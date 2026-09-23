#!/usr/bin/env python3
"""End-to-end RF Eye detector test against a simulated radio environment.

``tetra-phy-selftest.py`` proves the waveform tests in isolation.  This one
proves the whole product decision: the survey, the network lock, the duplex
partner maths, the dwell planner and the alarm hysteresis, driven by a fake
air interface instead of an RTL-SDR.

The scenarios below are the ones that matter in the field, and numbers 4 and 5
are the exact failure modes of detector profile v7:

  1. Empty band                 -> never locks, never alerts
  2. C2000 site, nobody talking -> locks the network, stays silent
  3. C2000 site, handset keyed  -> locks, then alerts
  4. TETRA-shaped burst, no net -> never alerts (no base station to belong to)
  5. Interference on the exact
     uplink partner channel     -> locked network, still silent

    python3 scripts/detector-selftest.py
    python3 scripts/detector-selftest.py --verbose
"""
import argparse
import os
import sys
import tempfile
import time

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "rfeye"))

import numpy as np                                              # noqa: E402

import tetra_sim as sim                                         # noqa: E402
from config import DEFAULTS                                     # noqa: E402
from sdr_backend import SDRBackend                              # noqa: E402
from tetra_detector import plan_dwell, raster_snap              # noqa: E402

DOWNLINKS = [391_212_500.0, 391_237_500.0, 391_262_500.0]
UPLINKS = [f - 10_000_000.0 for f in DOWNLINKS]

FAILURES = []
VERBOSE = False


def note(msg):
    if VERBOSE:
        print("    " + msg)


def check(name, ok, detail=""):
    if not ok:
        FAILURES.append(f"{name}: {detail}")
    print(f"  [{'ok ' if ok else 'FAIL'}] {name}" + (f"  {detail}" if detail else ""))


class FakeAir:
    """A synthetic radio environment standing in for the RTL-SDR.

    Wideband survey requests get cheap band-limited humps: the survey only has
    to notice that something is there.  Narrowband dwell requests get real
    generated TETRA, because that is what is actually under test.
    """

    def __init__(self, downlinks=(), uplinks=(), interferers=(), snr_db=24.0):
        self.downlinks = list(downlinks)
        self.uplinks = list(uplinks)
        self.interferers = list(interferers)     # (freq_hz, kind)
        self.snr_db = float(snr_db)
        self.seed = 0
        self.burst_every = 5

    def silence(self):
        """Everything off air, as if the antenna had been unscrewed."""
        self.downlinks = []
        self.uplinks = []
        self.interferers = []

    def _emitters(self):
        for f in self.downlinks:
            yield f, "downlink"
        for f in self.uplinks:
            yield f, "uplink"
        for f, kind in self.interferers:
            yield f, kind

    def samples(self, centre, sr, count):
        self.seed += 1
        n = int(count)
        dur = n / float(sr)
        # Survey captures are short and only have to notice that something is
        # there. Verification dwells run at a wide rate too since profile 10,
        # but they are long, and what they measure is what is under test, so
        # they get real generated TETRA.
        wide = sr > 500_000 and n < 400_000
        parts = []
        for freq, kind in self._emitters():
            off = float(freq) - float(centre)
            if abs(off) > sr * 0.45:
                continue
            if wide:
                # Survey resolution only. Spurs stay narrow and strong so the
                # shape-aware ranking has something real to discriminate.
                if kind == "spur":
                    parts.append((sim.cw_tone(dur, sr, off), 3.0))
                else:
                    parts.append((sim.band_noise(dur, sr, 20_000.0, off,
                                                 self.seed + int(freq) % 977), 1.0))
            elif kind == "downlink":
                parts.append((sim.tetra_carrier(dur, sr, role="DOWNLINK",
                                                seed=self.seed + 11,
                                                freq_offset_hz=off), 1.0))
            elif kind == "uplink":
                parts.append((sim.tetra_carrier(dur, sr, role="UPLINK",
                                                seed=self.seed + 23,
                                                freq_offset_hz=off), 1.0))
            elif kind == "traffic_downlink":
                # A secondary carrier: real TETRA, but discontinuous.
                parts.append((sim.tetra_carrier(dur, sr, role="UPLINK",
                                                seed=self.seed + 29,
                                                slot_pattern=(0, 2),
                                                freq_offset_hz=off), 1.0))
            elif kind == "control_burst":
                # A single short uplink transmission: a moving mobile keying up
                # a registration burst, not a held call. One slot of one frame,
                # which is what a terminal really sends -- this was two
                # contiguous slots until 0.9.33, a shape that cleared the
                # occupancy measurement a single slot could not.
                parts.append((sim.control_burst(dur, sr, seed=self.seed + 37,
                                                n_slots=1, freq_offset_hz=off), 1.0))
            elif kind == "periodic_burst":
                # A terminal that reports in periodically rather than talking:
                # a one-slot burst, present only in some captures, which is
                # what a passing vehicle mostly offers a receiver.
                # Drawn, not counted: a burst gated on the capture counter
                # aliases against the sweep period and can be present on
                # exactly the captures that never look here.
                import random as _random
                if _random.Random(self.seed * 2654435761).random() >= 1.0 / max(
                        1.0, float(self.burst_every)):
                    continue
                parts.append((sim.control_burst(dur, sr, seed=self.seed + 61,
                                                n_slots=1, freq_offset_hz=off), 1.0))
            elif kind == "access_burst":
                # Shorter still: the subslot of a random-access burst, the
                # first thing a terminal transmits when it registers and the
                # shortest transmission ETSI defines.
                parts.append((sim.control_burst(dur, sr, seed=self.seed + 43,
                                                n_slots=0.5, freq_offset_hz=off), 1.0))
            elif kind == "gated_noise":
                parts.append((sim.gated_noise(dur, sr, 22_000.0, 0.0142, 0.0567,
                                              off, self.seed + 31), 1.0))
            elif kind == "cw":
                parts.append((sim.cw_tone(dur, sr, off), 1.0))
            elif kind == "spur":
                # Narrow and strong: what an RTL-SDR comb tooth looks like.
                parts.append((sim.cw_tone(dur, sr, off), 3.0))
            elif kind == "wide":
                parts.append((sim.band_noise(dur, sr, 80_000.0, off,
                                             self.seed + 41), 1.0))
        return sim.make_capture(parts, dur, sr, snr_db=self.snr_db,
                                seed=self.seed + 500)[:n]


def make_backend(air, statefile, **overrides):
    cfg = dict(DEFAULTS)
    cfg.update(overrides)
    os.environ["RFEYE_SITE_STATE"] = statefile
    b = SDRBackend(cfg)
    b.running = True
    b._samples = lambda centre, sr, count: air.samples(centre, sr, count)
    b.sdr_path = "SIMULATED"
    return b


def run(backend, cycles, at=None):
    log = []
    for i in range(int(cycles)):
        if at and i in at:
            at[i]()
        ok = backend._scan_cycle()
        s = backend.snapshot()
        log.append(s)
        note(f"cycle {i:2d} state={s['detector_state']:<9} "
             f"locked={s['site_locked_count']} alert={s['mobile_confirmed']} "
             f"cand={s['site_candidate_count']} queue={s['site_queue_remaining']}"
             + (f" err={s['error'][:50]}" if s["error"] else ""))
        if not ok:
            note("  scan cycle reported failure")
    return log


def scenario(name, air, cycles=14, at=None, **overrides):
    print(f"\n{name}")
    with tempfile.TemporaryDirectory() as d:
        b = make_backend(air, os.path.join(d, "sites.json"), **overrides)
        try:
            return run(b, cycles, at=at)
        finally:
            b.running = False


def check_queue_order():
    """The verification queue has to be complete *and* usefully ordered.

    Order is the entire time-to-lock budget: one dwell covers four channels
    in about 1.1 s, so a 200-channel band is 68 s end to end and where a real
    carrier sits in the queue decides whether a lock takes 5 s or 70.
    Completeness is what keeps that safe -- on the reference unit the survey's
    top twelve were all spur-comb teeth, and queueing only those meant a
    genuine carrier was never verified at all.
    """
    print("\n11. verification queue")
    with tempfile.TemporaryDirectory() as d:
        air = FakeAir(downlinks=DOWNLINKS,
                      interferers=[(390_012_500.0 + k * 800_000.0, "spur")
                                   for k in range(6)])
        b = make_backend(air, os.path.join(d, "sites.json"))
        try:
            b._refill_site_queue(time.time())
            raster = [int(round(x)) for x in b._downlink_raster()]
            queue = [int(round(x)) for x in b._site_queue]
            check("queue still covers every raster channel",
                  sorted(queue) == sorted(raster),
                  "%d queued / %d raster" % (len(queue), len(raster)))
            hits = [queue.index(int(round(f))) for f in DOWNLINKS
                    if int(round(f)) in queue]
            check("a real carrier is reached early in the queue",
                  bool(hits) and min(hits) < 40,
                  "first real carrier at queue position %s" % (min(hits) if hits else None))
        finally:
            b.running = False


def check_rescan_while_locked():
    """A locked network must not stop the band being swept.

    A TETRA site runs several carriers and each one has its own uplink
    partner, so the watch list is exactly as wide as the number of locked
    downlinks. Until 0.9.11 the pass stopped for good once the first carrier
    locked and the queue drained: one lock meant one watched uplink channel
    for the rest of the session, and a handset on any other carrier of the
    same site was never looked at.
    """
    print("\n12. rediscovery while locked")
    with tempfile.TemporaryDirectory() as d:
        air = FakeAir(downlinks=DOWNLINKS)
        b = make_backend(air, os.path.join(d, "sites.json"), site_rescan_s=60.0)
        try:
            now = time.time()
            # A locked carrier, an exhausted queue, and the rescan timer due.
            b.sites.entries[int(round(DOWNLINKS[0]))] = {
                "hits": 9, "misses": 0, "quality": 0.7,
                "last_ok": now, "first_seen": now}
            assert b.sites.locked(now), "test setup: the carrier must read as locked"
            b._site_queue = []
            b._survey_at = now - 3600.0
            b._site_work(now)
            check("sweeps the band again once a network is locked",
                  len(b._site_queue) > 0,
                  "queue refilled to %d channels" % len(b._site_queue))

            # ...but not on every round, or it would starve the uplink watch.
            b._site_queue = []
            b._survey_at = time.time()
            b._site_work(time.time())
            check("and not on every round", len(b._site_queue) == 0,
                  "queue %d" % len(b._site_queue))
        finally:
            b.running = False


def check_one_quiet_carrier_costs_nothing():
    """A single quiet carrier may not cost the other carriers their locks.

    Until 0.9.24 one designated carrier decided whether the site existed, and
    three quiet re-tests of it set ``hits = 0`` on every entry. Reported from
    the field and visible in the log: a pass with 24 channels through the full
    waveform test, the best at 37.6 dB TETRA, coming out as locked=0 cand=7 --
    every proven carrier demoted at once and the whole site rebuilt from
    scratch.

    Silence is evidence of a deaf receiver only when it is everywhere. One
    carrier going quiet is a traffic carrier being a traffic carrier.
    """
    print(chr(10) + "13. one quiet carrier costs the others nothing")
    from tetra_detector import SiteRegistry
    with tempfile.TemporaryDirectory() as d:
        os.environ["RFEYE_SITE_STATE"] = os.path.join(d, "sites.json")
        try:
            cfg = dict(DEFAULTS)
            cfg["site_silence_window_s"] = 180.0
            reg = SiteRegistry(cfg)
            now = time.time()
            for f in (391_212_500.0, 391_237_500.0, 391_262_500.0):
                for _ in range(4):
                    reg.observe(f, True, 0.7, now)
            assert len(reg.locked(now)) == 3, "test setup: three locked"

            # One of them goes quiet for round after round. The others keep
            # verifying, so the receiver is demonstrably fine.
            quiet = 391_262_500.0
            for i in range(8):
                t = now + 10.0 * (i + 1)
                reg.observe(quiet, False, 0.0, t, silent=True)
                for f in (391_212_500.0, 391_237_500.0):
                    reg.observe(f, True, 0.7, t)
            end = now + 80.0
            check("the carriers that are still heard keep their locks",
                  len(reg.locked(end)) >= 2,
                  "%d of 3 still locked" % len(reg.locked(end)))
            check("and the quiet one is not held against itself either",
                  reg.entries[int(quiet)]["hits"] > 0,
                  "hits=%d" % reg.entries[int(quiet)]["hits"])

            # Now the receiver really does go deaf: nothing verifies anywhere.
            # Past the silence window the excuse lapses and misses count.
            deaf = end + float(cfg["site_silence_window_s"]) + 10.0
            assert not reg.heard_recently(deaf), "nothing may still count as heard"
            for i in range(int(cfg["site_unlock_misses"]) + 1):
                for f in (391_212_500.0, 391_237_500.0, 391_262_500.0):
                    reg.observe(f, False, 0.0, deaf + i, silent=True)
            check("but silence everywhere at once does drop the site",
                  len(reg.locked(deaf + 10.0)) == 0,
                  "%d still locked" % len(reg.locked(deaf + 10.0)))
        finally:
            os.environ.pop("RFEYE_SITE_STATE", None)


def check_ok_total_survives_a_restart():
    """save() wrote the lifetime score; _load() threw it away.

    Every carrier therefore came out of a restart with ok_total = 0, so
    anything ranking on it ranked on nothing at all.
    """
    print(chr(10) + "13b. the state file round-trips")
    from tetra_detector import SiteRegistry
    with tempfile.TemporaryDirectory() as d:
        os.environ["RFEYE_SITE_STATE"] = os.path.join(d, "sites.json")
        try:
            cfg = dict(DEFAULTS)
            reg = SiteRegistry(cfg)
            now = time.time()
            for _ in range(5):
                reg.observe(391_212_500.0, True, 0.8, now)
            reg.save()
            before = reg.entries[391_212_500]["ok_total"]
            assert before == 5, before
            again = SiteRegistry(cfg)
            check("ok_total survives a restart",
                  again.entries[391_212_500].get("ok_total") == before,
                  "%r after reload, %d before"
                  % (again.entries[391_212_500].get("ok_total"), before))
        finally:
            os.environ.pop("RFEYE_SITE_STATE", None)


def check_rescan_with_candidates():
    """One half-verified candidate must not stop the band being swept.

    0.9.11 took the "only refill when nothing is pending" gate off the locked
    path and left it standing on the unlocked one. It is the same trap either
    way: `candidates()` returns every carrier below the lock threshold with no
    age limit, so a single channel that passed the waveform test once keeps
    the priority list non-empty for ever and the refill never fires. Seen on a
    real unit -- 31 minutes without a single band pass while it chased two
    candidates that could not reach a lock by themselves, and could not find
    the carriers that would have corroborated them because it had stopped
    looking.
    """
    print(chr(10) + "14. rediscovery with a candidate but no lock")
    with tempfile.TemporaryDirectory() as d:
        air = FakeAir(downlinks=DOWNLINKS)
        b = make_backend(air, os.path.join(d, "sites.json"))
        try:
            now = time.time()
            # Exactly the state the field unit was stuck in: heard once,
            # nowhere near a lock, and stale.
            b.sites.entries[int(round(DOWNLINKS[0]))] = {
                "hits": 1, "misses": 0, "quality": 0.4,
                "last_ok": now - 1800.0, "first_seen": now - 1800.0}
            assert not b.sites.locked(now), "test setup: nothing may be locked"
            assert b.sites.candidates(now), "test setup: the candidate must survive"
            b._site_queue = []
            b._survey_at = now - 3600.0
            b._site_work(now)
            check("sweeps the band with a candidate pending",
                  len(b._site_queue) > 0,
                  "queue refilled to %d channels" % len(b._site_queue))

            # The candidate still goes first: the refill decides what is swept
            # up alongside it, it does not push it aside.
            check("and still serves the candidate first",
                  b.sites.candidates(now)[0]["freq_hz"] == DOWNLINKS[0],
                  "%.4f MHz" % (b.sites.candidates(now)[0]["freq_hz"] / 1e6))

            # Not on every round, or the idle interval would mean nothing.
            b._site_queue = []
            b._survey_at = time.time()
            b._site_work(time.time())
            check("but not on every round", len(b._site_queue) == 0,
                  "queue %d" % len(b._site_queue))
        finally:
            b.running = False


def check_candidate_does_not_starve_the_pass():
    """A candidate outside the current dwell must not hold every round.

    A round is one dwell, one ~100 kHz window, so whatever heads the priority
    list consumes it. Candidates were queued unconditionally on every round
    while locked carriers had a timer, so a candidate sitting far from the
    channels the queue was working through took every dwell for itself. The
    queue then never drained, never emptied, never refilled, and no band pass
    ever completed -- and `candidates()` has no age limit, so it never expired.

    Seen in the field at 0.9.18: two candidates 575 kHz apart and not one
    band pass in an hour, on a unit reporting SEARCHING throughout.
    """
    print(chr(10) + "15. a candidate must not monopolise the dwell")
    with tempfile.TemporaryDirectory() as d:
        air = FakeAir(downlinks=DOWNLINKS)
        b = make_backend(air, os.path.join(d, "sites.json"))
        try:
            now = time.time()
            # Two half-verified carriers far enough apart that neither can
            # share a dwell with the other, or with the head of the queue.
            for f in (391_187_500.0, 391_762_500.0):
                b.sites.entries[int(round(f))] = {
                    "hits": 1, "misses": 0, "quality": 0.4,
                    "last_ok": now - 60.0, "first_seen": now - 600.0}
            assert not b.sites.locked(now), "test setup: nothing may be locked"
            assert len(b.sites.candidates(now)) == 2

            b._survey_at = 0.0
            b._site_queue = []
            b._site_work(now)            # first round refills the queue
            start = len(b._site_queue)
            assert start > 0, "test setup: the queue must have been refilled"

            # The channels either candidate can drag into its own dwell are
            # not the interesting ones -- they come along for free and then
            # run out. What matters is whether the pass reaches the rest of
            # the band afterwards.
            # "Near" is whatever one dwell anchored on a candidate can reach,
            # which is the dwell's own half-width plus a channel of margin.
            reach = float(DEFAULTS["phy_max_offset_hz"]) + 50_000.0
            near = lambda f: min(abs(f - 391_187_500.0), abs(f - 391_762_500.0)) <= reach
            far_before = [f for f in b._site_queue if not near(f)]
            for i in range(40):
                b._site_work(now + 1.0 + i)
            far_after = [f for f in b._site_queue if not near(f)]
            far_done = len(far_before) - len(far_after)
            check("the pass reaches the band beyond the candidates",
                  far_done > 0,
                  "%d of %d distant channels covered in 40 rounds"
                  % (far_done, len(far_before)))

            # ...and they are not parked while that happens.
            served = sum(1 for f in (391_187_500.0, 391_762_500.0)
                         if int(round(f)) in b.sites.entries
                         and (b.sites.entries[int(round(f))]["last_ok"] > now - 60.0
                              or b.sites.entries[int(round(f))]["misses"] > 0))
            check("and the candidates are still being re-tested",
                  served == 2, "%d of 2 candidates revisited" % served)
        finally:
            b.running = False


def check_stuck_capture_is_refused():
    """A dongle handing back the same buffer must be caught, not believed.

    Recorded in the field on 11 September, next to an undercover police car:
    every channel of every dwell, uplink and downlink, anywhere in the band,
    returned identical numbers -- snr 34.3, bw 32344, boundary -13.4, centre
    10039 -- for six minutes, with zero downlink verifications, while the
    panel reported a live and locked network. The samples had stopped being
    new. A receiver in that state cannot detect anything, and must not look
    as though it can.
    """
    print(chr(10) + "16. a stuck capture is refused, not analysed")
    import numpy as np

    class FakeRTL:
        rate_changed = False
        def __init__(self, frozen):
            self.frozen = frozen
            self.rng = np.random.default_rng(3)
            self.buf = None
            self.closed = False
        def configure(self, *a): pass
        def tune(self, *a): pass
        def reset(self): pass
        def close(self): self.closed = True
        def read_complex(self, count, abort=None):
            if self.frozen and self.buf is not None and count == len(self.buf):
                return self.buf
            out = (self.rng.standard_normal(count)
                   + 1j * self.rng.standard_normal(count)).astype(np.complex64) * 12
            if count > 10000:
                self.buf = out
            return out

    with tempfile.TemporaryDirectory() as d:
        os.environ["RFEYE_SITE_STATE"] = os.path.join(d, "sites.json")
        b = SDRBackend(dict(DEFAULTS))
        b.running = True
        try:
            # Live air: two dwells never agree, so both are accepted.
            b.sdr = FakeRTL(frozen=False)
            b._samples(391_000_000.0, 288_000, 262_144)
            b._samples(391_000_000.0, 288_000, 262_144)
            check("live captures pass, even on the same frequency",
                  b.stuck_captures == 0, "stuck=%d" % b.stuck_captures)

            # A frozen dongle: the second dwell is a byte-for-byte repeat.
            fake = FakeRTL(frozen=True)
            b.sdr = fake
            b._last_capture_sig = None
            b._samples(390_500_000.0, 288_000, 262_144)
            refused = False
            try:
                b._samples(392_000_000.0, 288_000, 262_144)
            except RuntimeError as e:
                refused = "stuck" in str(e)
            check("an identical repeat is refused as a stuck capture", refused,
                  "stuck=%d" % b.stuck_captures)
            check("and the handle is dropped so the next dwell reopens it",
                  b.sdr is None and fake.closed,
                  "sdr=%r closed=%s" % (b.sdr, fake.closed))

            # The scan cycle treats it as a wedged dongle worth a USB reset,
            # the same as a read timeout.
            calls = []
            b._recover_sdr_usb = lambda: calls.append(1) or False
            def frozen_samples(centre, sr, count):
                raise RuntimeError("librtlsdr read failed: capture stuck: "
                                   "identical to the previous dwell")
            b._samples = frozen_samples
            b._scan_cycle()
            check("the scan cycle asks for a USB recovery", bool(calls),
                  "%d recovery call(s)" % len(calls))
            check("and reports the fault instead of a live network",
                  "stuck" in str(b.error) and b.status != "LIVE",
                  "status=%s error=%s" % (b.status, str(b.error)[:40]))
        finally:
            b.running = False
            os.environ.pop("RFEYE_SITE_STATE", None)


SHORT_PTT_SITE = [390_737_500.0, 390_912_500.0, 391_187_500.0,
                  391_512_500.0, 391_762_500.0]


def check_short_transmission_alerts(**overrides):
    """A push-to-talk of a few seconds, on a site with several carriers.

    Every alert scenario above keeps its handset keyed for the whole test, so
    all of them passed while the field units never once alerted. Measured on
    rfeye at 0.9.25 with five locked carriers: each uplink channel came round
    every 9 s, one channel per dwell, and confirmation needs a second hit on
    the same channel -- so a transmission had to outlast two visits, 18 s, to
    be reported at all. Real traffic is a few seconds of speech.

    Here the handset is keyed for three scan cycles, three to five seconds on
    a Pi, and the alert has to come while it is still on air. Returns True if
    it did, so the old dwell can be run through it too.
    """
    print(chr(10) + "17. a short push-to-talk on a busy site")
    with tempfile.TemporaryDirectory() as d:
        air = FakeAir(downlinks=SHORT_PTT_SITE)
        b = make_backend(air, os.path.join(d, "sites.json"), **overrides)
        try:
            now = time.time()
            for f in SHORT_PTT_SITE:
                b.sites.entries[int(round(f))] = {
                    "hits": 9, "misses": 0, "quality": 0.7,
                    "last_ok": now, "first_seen": now, "ok_total": 9}
            assert len(b.sites.locked(now)) == len(SHORT_PTT_SITE)
            b._site_queue = []
            b._survey_at = now
            handset = SHORT_PTT_SITE[2] - 10_000_000.0
            log = run(b, 9, at={3: lambda: air.uplinks.append(handset),
                                6: lambda: air.uplinks.remove(handset)})
            keyed = [i for i, s in enumerate(log[3:6], start=3) if s["mobile_confirmed"]]
            check("alerts while the handset is still keyed up", bool(keyed),
                  "alert on cycles %s" % [i for i, s in enumerate(log)
                                          if s["mobile_confirmed"]])
            hit = next((s for s in log if s["mobile_confirmed"] and s["peaks"]), None)
            check("and names the handset's uplink channel",
                  bool(hit) and abs(hit["peaks"][0]["freq_hz"] - handset) < 1000.0,
                  ("%.4f MHz" % (hit["peaks"][0]["freq_hz"] / 1e6)) if hit else "no alert")
            return bool(keyed)
        finally:
            b.running = False


def check_follow_up_is_bounded():
    """Following a hit may not leave the rest of the site unwatched.

    A long transmission keeps verifying, and a follow-up re-armed by its own
    hits would park the receiver on that one window for as long as the handset
    talks, while a second handset on another carrier went unseen.
    """
    print(chr(10) + "18. a long transmission does not blind the other carriers")
    site = [390_037_500.0, 391_962_500.0]     # too far apart for one dwell
    with tempfile.TemporaryDirectory() as d:
        air = FakeAir(downlinks=site, uplinks=[site[0] - 10_000_000.0])
        b = make_backend(air, os.path.join(d, "sites.json"))
        try:
            now = time.time()
            for f in site:
                b.sites.entries[int(round(f))] = {
                    "hits": 9, "misses": 0, "quality": 0.7,
                    "last_ok": now, "first_seen": now, "ok_total": 9}
            b._site_queue = []
            b._survey_at = now
            seen = []
            real = b._verify
            def spy(freqs, role):
                out = real(freqs, role)
                if role == "UPLINK":
                    seen.append([int(round(f)) for f in b.dwell_channels])
                return out
            b._verify = spy
            run(b, 10)
            other = int(round(site[1] - 10_000_000.0))
            visits = sum(1 for chans in seen if other in chans)
            check("the other carrier's uplink is still visited",
                  visits >= 2, "%d visits in %d uplink dwells" % (visits, len(seen)))
        finally:
            b.running = False


def check_sensitive_control_burst():
    """Sensitive mode alerts on a short control burst; strict does not.

    This is the failure the field kept hitting: eight drives past police, no
    held voice call on air, no alert. A moving mobile still keys up short
    registration bursts, and sensitive mode (the 0.9.29 default) confirms on
    one. Strict mode wants a held call and stays silent.
    """
    print(chr(10) + "19. a short control burst -- sensitive alerts, strict does not")
    site = [391_187_500.0, 391_512_500.0, 391_762_500.0]
    handset = site[0] - 10_000_000.0
    # Both real shapes: one 14 ms slot, and the 7 ms subslot of a random-access
    # burst. Until 0.9.33 this scenario only ran a contiguous two-slot block,
    # and neither of these two would have passed it -- which is why the suite
    # was green through eight drives past police that raised no alert.
    for kind in ("control_burst", "access_burst"):
        for mode, want in (("sensitive", True), ("strict", False)):
            with tempfile.TemporaryDirectory() as d:
                air = FakeAir(downlinks=site)
                b = make_backend(air, os.path.join(d, "sites.json"),
                                 uplink_sensitive=(mode == "sensitive"))
                try:
                    now = time.time()
                    for f in site:
                        b.sites.entries[int(round(f))] = {
                            "hits": 9, "misses": 0, "quality": 0.7,
                            "last_ok": now, "first_seen": now, "ok_total": 9}
                    b._site_queue = []
                    b._survey_at = now
                    log = run(b, 9, at={3: lambda: air.interferers.append((handset, kind)),
                                        6: lambda: air.interferers.clear()})
                    alerted = any(s["mobile_confirmed"] for s in log)
                    check("%s: %s on a lone %s" % (
                            mode, "alerts" if want else "stays silent", kind),
                          alerted == want,
                          "alerted=%s on cycles %s" % (alerted,
                              [i for i, s in enumerate(log) if s["mobile_confirmed"]]))
                    if want and alerted:
                        hit = next(s for s in log if s["mobile_confirmed"] and s["peaks"])
                        check("sensitive: names the %s uplink channel" % kind,
                              abs(hit["peaks"][0]["freq_hz"] - handset) < 1000.0,
                              "%.4f MHz" % (hit["peaks"][0]["freq_hz"] / 1e6))
                finally:
                    b.running = False


def check_vehicle_on_an_unlocked_carrier():
    """A vehicle keyed up on a carrier this unit never locked.

    This is the ambulance. Until 0.9.35 stage 2 watched exactly the duplex
    partners of the locked downlinks -- on a real unit two channels of the two
    hundred in 380-385 MHz -- so a terminal registered on any other carrier,
    or on a neighbouring site, transmitted into a receiver that was not
    listening there. Not a sensitivity problem: a coverage one.
    """
    print(chr(10) + "20. a vehicle on a carrier this unit never locked")
    site = [391_187_500.0, 391_512_500.0]
    stranger = 382_437_500.0          # an uplink with no locked partner here
    with tempfile.TemporaryDirectory() as d:
        air = FakeAir(downlinks=site)
        b = make_backend(air, os.path.join(d, "sites.json"))
        try:
            now = time.time()
            for f in site:
                b.sites.entries[int(round(f))] = {
                    "hits": 9, "misses": 0, "quality": 0.7,
                    "last_ok": now, "first_seen": now, "ok_total": 9}
            b._site_queue = []
            b._survey_at = now
            watched = set()
            log = run(b, 26, at={2: lambda: air.interferers.append((stranger, "uplink"))})
            # watch_freqs is whatever dwell ran last in the cycle, and a
            # downlink pass shares the cycle, so count only the uplink band.
            for s in log:
                watched |= {int(round(f)) for f in (s.get("watch_freqs") or [])
                            if 380e6 <= f < 385e6}
            check("the sweep reaches beyond the locked partners",
                  len(watched) > 8, "%d distinct uplink channels examined" % len(watched))
            alerted = [i for i, s in enumerate(log) if s["mobile_confirmed"]]
            check("and alerts on a vehicle with no locked partner",
                  bool(alerted), "alert on cycles %s" % alerted)
            if alerted:
                hit = next(s for s in log if s["mobile_confirmed"] and s["peaks"])
                check("naming the right channel",
                      abs(hit["peaks"][0]["freq_hz"] - stranger) < 1000.0,
                      "%.4f MHz" % (hit["peaks"][0]["freq_hz"] / 1e6))
        finally:
            b.running = False


def check_unlocked_receiver_still_hears():
    """With nothing locked at all, the uplink is still watched.

    Driving, that is most of the time in every new cell: the lock the unit
    holds belongs to a site it left behind, or there is none yet. Stage 2 used
    to run only behind a lock, so the receiver was deaf exactly then.
    """
    print(chr(10) + "21. nothing locked -- the uplink is watched anyway")
    stranger = 381_937_500.0
    with tempfile.TemporaryDirectory() as d:
        air = FakeAir(downlinks=[])
        b = make_backend(air, os.path.join(d, "sites.json"))
        try:
            watched = set()
            log = run(b, 14, at={1: lambda: air.interferers.append((stranger, "uplink"))})
            for s in log:
                watched |= {int(round(f)) for f in (s.get("watch_freqs") or [])
                            if 380e6 <= f < 385e6}
            check("uplink channels examined with no lock at all",
                  len(watched) > 8, "%d channels" % len(watched))
            check("and it can still raise an alert",
                  any(s["mobile_confirmed"] for s in log),
                  "alert on cycles %s" % [i for i, s in enumerate(log) if s["mobile_confirmed"]])
        finally:
            b.running = False


def check_unknown_channel_needs_two_hits():
    """One hit confirms on a partner channel; two are needed elsewhere.

    Sweeping the whole band means two orders of magnitude more channels for a
    freak accept to land on, so the single-hit rule that sensitive mode was
    priced for is kept only where the site lock corroborates it. The follow-up
    is what keeps the second hit cheap: it parks the next dwells back on the
    window that just produced one.
    """
    print(chr(10) + "22. one hit on a partner, two on an unknown channel")
    from tetra_detector import UplinkAlarm
    from tetra_phy import PhyResult
    cfg = dict(DEFAULTS)
    partner, stranger = 381_187_500.0, 382_437_500.0

    def hit(f):
        r = PhyResult(freq_hz=f, role="UPLINK"); r.ok = True; r.quality = 0.6
        return r

    a = UplinkAlarm(cfg)
    ok, _, _ = a.update([hit(partner)], [partner, stranger], 1000.0, trusted=[partner])
    check("one hit on a locked partner confirms", ok)

    a = UplinkAlarm(cfg)
    ok, _, _ = a.update([hit(stranger)], [partner, stranger], 1000.0, trusted=[partner])
    check("one hit on an unknown channel does not", not ok)
    ok, _, _ = a.update([hit(stranger)], [partner, stranger], 1001.0, trusted=[partner])
    check("the second hit on it does", ok)


def check_capture_pipeline():
    """The capture thread must change the timing and nothing else.

    Until 0.9.37 the radio and the maths strictly alternated: 0.70 s capturing
    then 0.93 s analysing, so the receiver listened to 380-385 MHz 11% of the
    time. The pump overlaps them. What it must not do is change a single
    verdict, so the same air is driven through both paths and compared.
    """
    print(chr(10) + "23. the capture pipeline decides exactly what one thread decided")
    from sdr_backend import _CapturePump
    site = [391_187_500.0, 391_512_500.0]
    stranger = site[0] - 10_000_000.0

    def drive(pipelined):
        with tempfile.TemporaryDirectory() as d:
            air = FakeAir(downlinks=site)
            # At the power ladder's recovery steps the capture deliberately
            # does not overlap; this compares the overlap itself.
            b = make_backend(air, os.path.join(d, "sites.json"),
                             scan_pipeline_min_duty=0.0)
            if pipelined:
                b._pump = _CapturePump(lambda c, s, n: b._samples(c, s, n))
            try:
                now = time.time()
                for f in site:
                    b.sites.entries[int(round(f))] = {
                        "hits": 9, "misses": 0, "quality": 0.7,
                        "last_ok": now, "first_seen": now, "ok_total": 9}
                b._site_queue = []
                b._survey_at = now
                log = run(b, 16, at={3: lambda: air.interferers.append((stranger, "uplink"))})
                seen = set()
                for s in log:
                    seen |= {int(round(f)) for f in (s.get("watch_freqs") or [])
                             if 380e6 <= f < 385e6}
                return ([s["mobile_confirmed"] for s in log], sorted(seen),
                        b._prefetch_hits, b._prefetch_misses)
            finally:
                b.running = False
                job, b._prefetch = b._prefetch, None
                if job is not None and b._pump is not None:
                    try: b._pump.wait(job, 5.0)
                    except Exception: pass
                if b._pump is not None:
                    b._pump.stop()

    plain = drive(False)
    piped = drive(True)
    check("the same alert pattern either way", plain[0] == piped[0],
          "%d cycles, %d alerts both ways" % (len(plain[0]), sum(plain[0])))
    check("the same channels examined either way", plain[1] == piped[1],
          "%d channels" % len(piped[1]))
    check("and the prefetched capture is actually being used",
          piped[2] > 0, "%d used, %d discarded" % (piped[2], piped[3]))
    check("the single-threaded path prefetches nothing", plain[2] == 0)


def check_radio_duty_limit():
    """The radio's share of the time is held to what the supply can carry.

    0.9.37 moved it without naming it: capture on its own thread and a 0.10 s
    pause had the dongle streaming about 88% of the time against 37% before,
    and a field unit threw it off the USB bus 169 s after boot with the
    under-voltage flag set. The limit is a measured fraction now, ECO and Max
    power really differ, and losing the dongle backs it off further.
    """
    print(chr(10) + "24. the radio is held to its share of the time")
    with tempfile.TemporaryDirectory() as d:
        air = FakeAir(downlinks=[391_187_500.0])
        b = make_backend(air, os.path.join(d, "sites.json"))
        try:
            now = time.time()
            eco = b._duty_limit(now)
            b.cfg["low_power_mode"] = False
            full = b._duty_limit(now)
            b.cfg["low_power_mode"] = True
            check("ECO limits the radio to a fraction of the time",
                  0.2 <= eco <= 0.8, "%.2f" % eco)
            check("Max power lifts the limit", full > eco, "%.2f vs %.2f" % (full, eco))
            b.ladder.event(now, "SDR lost", in_use=eco)
            backed = b._duty_limit(now)
            check("and losing the dongle backs it off further", backed < eco,
                  "%.2f" % backed)
            b.ladder.index = len(b.ladder.rungs) - 1

            # The pause really is computed from measured streaming time.
            b._radio_epoch = now - 10.0
            b._radio_s = 9.0                      # 90% of the window, way over
            t0 = time.perf_counter()
            b.running = True
            b._low_power_pause()
            waited = time.perf_counter() - t0
            check("an over-budget radio is made to wait", waited > 0.3,
                  "%.2f s" % waited)
            b._radio_s = 0.2                      # 2% of the window, under
            t0 = time.perf_counter()
            b._low_power_pause()
            check("an under-budget one is not", (time.perf_counter() - t0) < 0.5)
        finally:
            b.running = False


def check_follow_up_outlasts_a_repeat():
    """A follow-up has to outlive the gap between two transmissions.

    The second hit an unknown channel needs comes from the next transmission
    on it. Counted only in dwells the window was about 2.4 s, which can be
    shorter than the gap, so the look that exists to catch the repeat ended
    before it arrived.
    """
    print(chr(10) + "25. the follow-up outlasts one repeat")
    from config import DEFAULTS as D
    dwells = int(D["uplink_follow_dwells"])
    secs = float(D["uplink_follow_s"])
    check("the follow-up is bounded in seconds as well as dwells", secs > 0,
          "%d dwells or %.1f s" % (dwells, secs))
    check("and the window outlasts a several-second repeat", secs >= 5.0,
          "%.1f s" % secs)
    with tempfile.TemporaryDirectory() as d:
        air = FakeAir(downlinks=[391_187_500.0])
        b = make_backend(air, os.path.join(d, "sites.json"))
        try:
            b._follow = [381_187_500.0]
            b._follow_left = dwells
            b._follow_until = time.time() + secs
            plan = b._uplink_plan(time.time())
            check("a fresh follow-up is honoured",
                  bool(plan) and abs(plan['targets'][0] - 381_187_500.0) < 1.0)
            b._follow_until = time.time() - 0.1
            plan = b._uplink_plan(time.time())
            check("an expired one is not, dwells left or no",
                  bool(plan) and abs(plan['targets'][0] - 381_187_500.0) > 1.0)
        finally:
            b.running = False


def check_periodic_reporter_is_caught():
    """A vehicle that reports in periodically rather than holding a call.

    This is what a passing emergency vehicle mostly offers a receiver: short
    transmissions every few seconds, not speech. Measured both ways, because
    the two cases behave very differently and only one of them is solved:

        carrier of a locked site   24 visits, alert three cycles in
        carrier with no lock       13 visits, alert once the channel is hot

    A channel that produces verified TETRA joins the fast rotation for
    uplink_hot_s, which is what makes the second case possible at all -- before
    it, one hit in seven visits and no alert, because the sweep only came back
    about once every fifteen dwells.
    """
    print(chr(10) + "26. a vehicle that reports in periodically while passing")
    site = [391_187_500.0, 391_512_500.0]
    for label, chan, budget in (("on a locked site's carrier",
                                 site[0] - 10_000_000.0, 12),
                                ("on a carrier with no lock",
                                 383_712_500.0, 58)):
        with tempfile.TemporaryDirectory() as d:
            air = FakeAir(downlinks=site)
            air.burst_every = 4
            # The revisit cadence is in wall-clock seconds, and this suite
            # runs a cycle in a fraction of what one costs on a Pi. Left
            # alone, a track would come round every eight simulated cycles
            # instead of every one or two, which measures the clock rather
            # than the mechanism. Zero here means "as often as the rotation
            # allows", which is what ~0.8 s means on the real thing.
            b = make_backend(air, os.path.join(d, "sites.json"),
                             track_revisit_fast_s=0.0,
                             track_revisit_s=0.0,
                             track_revisit_cool_s=0.0)
            try:
                now = time.time()
                for f in site:
                    b.sites.entries[int(round(f))] = {
                        "hits": 9, "misses": 0, "quality": 0.7,
                        "last_ok": now, "first_seen": now, "ok_total": 9}
                b._site_queue = []
                b._survey_at = now
                log = run(b, 70, at={2: lambda c=chan: air.interferers.append(
                    (c, "periodic_burst"))})
                hit = next((i for i, s in enumerate(log) if s["mobile_confirmed"]), None)
                check("recognised %s" % label, hit is not None and hit <= budget,
                      "alert on cycle %s (budget %d)" % (hit, budget))
                if hit is not None:
                    s = next(s for s in log if s["mobile_confirmed"] and s["peaks"])
                    check("  and on the right channel",
                          abs(s["peaks"][0]["freq_hz"] - chan) < 1000.0,
                          "%.4f MHz" % (s["peaks"][0]["freq_hz"] / 1e6))
            finally:
                b.running = False


def check_hot_channel_rotation():
    """One verified hit keeps its channel in the fast rotation for a while."""
    print(chr(10) + "27. a channel that produced TETRA stays hot")
    with tempfile.TemporaryDirectory() as d:
        b = make_backend(FakeAir(downlinks=[391_187_500.0]),
                         os.path.join(d, "sites.json"))
        try:
            now = time.time()
            stranger = 383_712_500.0
            tr = b.alarm.tracks.get(stranger, now)
            tr.note_hit(now, 20.0, 0.6, 4)
            plan = b._uplink_plan(now + 1.0)
            check("a channel that produced TETRA joins the fast rotation",
                  bool(plan) and stranger in plan["fast"],
                  "%d fast channels" % len(plan["fast"]))
            check("  and takes the first lane, ahead of the partners",
                  plan["lane"] == "track", plan["lane"])
            tr.last_hit = now - 10_000.0
            plan = b._uplink_plan(now)
            check("and drops out when it goes cold",
                  bool(plan) and stranger not in plan["fast"])
            check("but it is still not trusted for a single hit",
                  stranger not in set(b.sites.uplink_partners(now)))
        finally:
            b.running = False


def check_tracks_and_screen():
    """The two pieces 0.10.0 rests on, asserted directly.

    A track is what lets the scan spend its time where something is, and the
    screen is what stops it paying full price for the 199 channels where
    nothing is. Both are cheap to get subtly wrong in ways the end-to-end
    scenarios would not notice for a while.
    """
    print(chr(10) + "28. tracks remember, and the screen only ever drops")
    import numpy as np
    import tetra_phy as phy
    import tetra_sim as sim2
    from tracker import RadioTrack, TrackRegistry, ACTIVE, FADING, LOST

    now = 1000.0
    tr = RadioTrack(381_187_500.0, now)
    for k in range(4):
        tr.note_visit(now + 4.0 * k)
        tr.note_hit(now + 4.0 * k, 18.0 + k, 0.5, 10)
    check("a repeating transmission shows its period",
          abs(tr.period_s() - 4.0) < 0.01, "%.2f s" % tr.period_s())
    check("the bar follows the RF level, not the waveform score",
          tr.level(8.0, 26.0) > tr.level(8.0, 60.0),
          "%.2f at 26 dB full scale" % tr.level(8.0, 26.0))
    last = now + 12.0                      # the fourth and final hit
    check("it is here while it is still talking",
          tr.state(last + 1.0, 2.0, 5.0) == ACTIVE, tr.state(last + 1.0, 2.0, 5.0))
    check("  then fading", tr.state(last + 3.0, 2.0, 5.0) == FADING,
          tr.state(last + 3.0, 2.0, 5.0))
    check("  then gone", tr.state(last + 6.0, 2.0, 5.0) == LOST,
          tr.state(last + 6.0, 2.0, 5.0))

    # The registry floors these at 5 s, so prune past that, not past the
    # configured value -- which is the kind of thing this check is for.
    reg = TrackRegistry({"track_forget_quiet_s": 1.0, "track_forget_s": 90.0})
    reg.note_visits([380_012_500.0], now)
    reg.prune(now + 4.0)
    check("a channel that said nothing is kept for the floor", bool(reg.tracks))
    reg.prune(now + 7.0)
    check("and forgotten after it", not reg.tracks)

    # The screen may drop what the real test would drop, never the reverse.
    SR = 2_016_000.0
    dur = (1 << 20) / SR
    parts = [(sim2.tetra_carrier(dur, SR, role="DOWNLINK", seed=11,
                                 freq_offset_hz=o), 1.0)
             for o in (-400_000.0, 0.0, 350_000.0)]
    worst = 0.0
    for iq in (sim2.make_capture(parts, dur, SR, snr_db=12, seed=5),
               sim2.make_capture([], dur, SR, seed=5)):
        ch = phy.Channelizer(iq, SR)
        prep = ch.shape_prepared(percentile=100.0)
        offs = [-600_000.0 + 25_000.0 * k for k in range(48)]
        levels = phy.screen_levels(prep, offs)
        for o in offs:
            try:
                real = phy.channel_shape(prep["freqs"], prep["p_db"],
                                         centre_hz=o, prepared=prep)["snr_db"]
            except ValueError:
                continue
            worst = min(worst, levels[o] - real)
    check("the screen never reads below the real level by more than its margin",
          worst > -2.0, "worst %.2f dB against a 2.0 dB margin" % worst)


def check_priority_lanes():
    """A track that is due may not wait behind the partner list.

    Merged into the partners it shared their rotation, so an active unknown
    channel could be made to wait behind however many partners a locked site
    happened to name. The run cap is the other half: without it a channel that
    never stops transmitting is due every round and nothing else is measured.
    """
    print(chr(10) + "29. the three lanes are really three")
    site = [391_187_500.0, 391_512_500.0, 391_762_500.0]
    with tempfile.TemporaryDirectory() as d:
        b = make_backend(FakeAir(downlinks=site), os.path.join(d, "sites.json"))
        try:
            now = time.time()
            for f in site:
                b.sites.entries[int(round(f))] = {
                    "hits": 9, "misses": 0, "quality": 0.7,
                    "last_ok": now, "first_seen": now, "ok_total": 9}
            stranger = 383_712_500.0
            b.alarm.tracks.get(stranger, now).note_hit(now, 20.0, 0.6, 10)
            lanes = []
            for _ in range(6):
                plan = b._uplink_plan(now + 1.0)
                lanes.append(plan["lane"])
                b._track_run = b._track_run + 1 if plan["lane"] == "track" else 0
                b._watch_turn = plan["turn"]
            check("a due track goes first", lanes[0] == "track", lanes[0])
            check("  and does not hold every lane", set(lanes) != {"track"},
                  " ".join(lanes))
            check("  the other lanes still get their turn",
                  {"partner", "band"} & set(lanes) != set(), " ".join(lanes))
        finally:
            b.running = False


def check_power_ladder():
    """The radio backs off when the supply sags, and earns its share back.

    Until 0.10.2 the rail was only read after the dongle had already gone, and
    what was read -- the since-boot under-voltage bit -- never clears. One dip
    pinned the radio at 30% until a reboot, while a unit that had not dipped
    yet ran at its full share right up to the moment it lost the dongle,
    which a field unit then did about ten minutes into every drive.
    """
    print(chr(10) + "30. the power ladder steps down on trouble and climbs back on calm")
    from power import PowerLadder, supply_text
    import sdr_backend as sb
    from sdr_backend import _CapturePump
    with tempfile.TemporaryDirectory() as d:
        cfg = dict(DEFAULTS)
        path = os.path.join(d, "ladder.json")
        lad = PowerLadder(cfg, path, boot_id="boot-a")
        t = 1_000_000.0
        check("a fresh unit is not held back", lad.cap == 1.0)
        check("trouble drops straight to the safe step, not one at a time",
              lad.event(t, "SDR lost", in_use=0.45) and abs(lad.cap - 0.30) < 1e-9,
              "%.2f" % lad.cap)
        check("the dip and the dongle going a second later are one incident",
              not lad.event(t + 1.0, "under-voltage", in_use=0.30)
              and abs(lad.cap - 0.30) < 1e-9 and lad.incidents == 1)

        def calm(t0, seconds, live=True):
            tt, end = t0, t0 + seconds
            while tt < end:
                tt += 1.0
                lad.tick(tt, live)
            return tt

        t2 = calm(t + 1.0, 100.0, live=False)
        check("time without a working dongle proves nothing",
              abs(lad.cap - 0.30) < 1e-9)
        t2 = calm(t2, 301.0)
        check("five calm minutes buy one step", abs(lad.cap - 0.35) < 1e-9,
              "%.2f" % lad.cap)
        t2 = calm(t2, 301.0)
        check("and five more the next", abs(lad.cap - 0.40) < 1e-9, "%.2f" % lad.cap)
        t2 = calm(t2, 600.0)
        check("the step it failed on stays off limits for half an hour",
              abs(lad.cap - 0.40) < 1e-9 and lad.next_step_s(t2) > 0.0,
              "%.2f, next in %.0f s" % (lad.cap, lad.next_step_s(t2)))
        t2 = calm(t2, (t + 1800.0 - t2) + 2.0)
        check("and is tried again after that", abs(lad.cap - 0.45) < 1e-9,
              "%.2f" % lad.cap)
        t3 = t2 + 60.0
        lad.event(t3, "SDR lost", in_use=0.45)
        check("failing there again keeps it away twice as long",
              abs(lad.cap - 0.30) < 1e-9
              and abs((lad.ceiling_until - t3) - 3600.0) < 1.0,
              "%.0f s" % (lad.ceiling_until - t3))
        t4 = calm(t3, 3600.0 + 302.0)
        t4 = calm(t4, 302.0)
        check("holding the old ceiling for a full step clears it",
              lad.cap > 0.45 and lad.ceiling == 0.0, "%.2f" % lad.cap)
        again = PowerLadder(cfg, path, boot_id="boot-b")
        check("where it stands survives a reboot", abs(again.cap - lad.cap) < 1e-9,
              "%.2f vs %.2f" % (again.cap, lad.cap))

        boot = os.path.join(d, "boot.json")
        x = PowerLadder(cfg, boot, boot_id="boot-c")
        check("a dip at power-up drops to the safe step",
              x.boot_dip(t) and abs(x.cap - 0.30) < 1e-9 and x.ceiling == 0.0)
        x2 = PowerLadder(cfg, boot, boot_id="boot-c")
        check("an app restart in the same boot does not take it twice",
              not x2.boot_dip(t + 100.0) and x2.incidents == 1)
        check("a dip the radio cannot have caused never goes under the safe step",
              x2.event(t + 200.0, "under-voltage before start")
              and abs(x2.cap - 0.30) < 1e-9)
        check("the radio failing at the safe step goes one further down",
              x2.event(t + 300.0, "SDR lost", in_use=0.30)
              and abs(x2.cap - 0.22) < 1e-9, "%.2f" % x2.cap)
        check("and not below the floor",
              x2.event(t + 400.0, "SDR lost", in_use=0.22)
              and abs(x2.cap - 0.22) < 1e-9)

        # The backend: the limit, the pipeline, and the dongle going.
        air = FakeAir(downlinks=[391_187_500.0])
        os.makedirs(os.path.join(d, "a"))
        b = make_backend(air, os.path.join(d, "a", "sites.json"))
        try:
            now = time.time()
            eco = b._duty_limit(now)
            b.ladder.event(now, "test", in_use=eco)
            check("the radio is held to the ladder",
                  abs(b._duty_limit(now) - 0.30) < 1e-9, "%.2f" % b._duty_limit(now))
            b.cfg["low_power_mode"] = False
            check("Max power too", abs(b._duty_limit(now) - 0.30) < 1e-9)
            b.cfg["low_power_mode"] = True
            b._pump = _CapturePump(lambda c, s_, n: b._samples(c, s_, n))
            check("at the recovery steps capture and analysis do not overlap",
                  not b._pipeline_on(now))
            b._prefetch_uplink(now)
            check("so nothing is prefetched", b._prefetch is None)
            b.ladder.index = len(b.ladder.rungs) - 1
            check("at the normal share they do", b._pipeline_on(now))
            b._pump.stop()
            b._pump = None

            # A clean ladder: the one above has just taken an incident, and
            # anything within its debounce is rightly the same one.
            b.ladder = PowerLadder(b.cfg, None, boot_id="t")
            ok = b._scan_cycle()
            check("a working dongle scans", ok and bool(b.last_good_scan))
            good = b._samples

            def gone(centre, sr, count):
                raise RuntimeError("librtlsdr read failed: boom")
            b._samples = gone
            b._scan_cycle()
            check("losing a working dongle steps the ladder down",
                  abs(b.ladder.cap - 0.30) < 1e-9, "%.2f" % b.ladder.cap)
            b._scan_cycle()
            b._scan_cycle()
            check("while it stays gone nothing more is taken",
                  b.ladder.incidents == 1, "%d" % b.ladder.incidents)
            b._samples = good
            b._scan_cycle()
            log = open(os.path.join(d, "a", "power.log")).read()
            check("power.log says what happened, and when it came back",
                  "SDR lost" in log and "DROP" in log and "SDR back" in log, log[-200:])
        finally:
            b.running = False

        # The supply itself, read before anything has gone wrong.
        os.makedirs(os.path.join(d, "b"))
        b2 = make_backend(air, os.path.join(d, "b", "sites.json"))
        word = ["0x50000"]

        class _CP:
            def __init__(self, out):
                self.stdout = out

        def fake_run(cmd, **kw):
            if cmd[1] == "get_throttled":
                return _CP("throttled=%s\n" % word[0])
            return _CP("volt=1.2563V\n")
        real_which, real_run = sb.shutil.which, sb.subprocess.run
        sb.shutil.which = lambda name: "/usr/bin/vcgencmd"
        sb.subprocess.run = fake_run
        try:
            t0 = time.time()
            b2._power_checked = 0.0
            b2._power_tick(t0)
            check("the supply is read before the dongle has had a chance to fail",
                  b2._power_bits == 0x50000)
            check("a dip before the radio started drops to the safe step, once",
                  abs(b2.ladder.cap - 0.30) < 1e-9 and b2.ladder.ceiling == 0.0,
                  "%.2f" % b2.ladder.cap)
            b2._power_checked = 0.0
            b2._power_tick(t0 + 40.0)
            check("the since-boot bit alone does not keep taking it down",
                  b2.ladder.incidents == 1, "%d" % b2.ladder.incidents)
            word[0] = "0x50005"
            b2._power_checked = 0.0
            b2._power_tick(t0 + 80.0)
            check("the rail low right now is a new incident",
                  b2.ladder.incidents == 2 and b2.ladder.cap < 0.30,
                  "%d, %.2f" % (b2.ladder.incidents, b2.ladder.cap))
            text = supply_text(b2.snapshot())
            check("and the debug page says so", "LOW NOW" in text and "cap 22%" in text,
                  text)
            check("in 22 characters", len(text) <= 22, "%d" % len(text))
        finally:
            sb.shutil.which, sb.subprocess.run = real_which, real_run
            b2.running = False


def main():
    global VERBOSE
    ap = argparse.ArgumentParser()
    ap.add_argument("--verbose", action="store_true")
    ap.add_argument("--only", default="",
                    help="comma-separated check_* functions to run on their own")
    args = ap.parse_args()
    VERBOSE = args.verbose
    if args.only:
        for name in args.only.split(","):
            globals()[name.strip()]()
        print()
        for f in FAILURES:
            print("  FAIL " + f)
        print("%d failures" % len(FAILURES) if FAILURES else "detector-selftest OK")
        return 1 if FAILURES else 0

    # 1 -- nothing on air but noise and clutter.
    log = scenario("1. empty band with clutter",
                   FakeAir(interferers=[(381_237_500.0, "wide"),
                                        (382_512_500.0, "cw")]))
    check("never locks a network", not any(s["site_locked_count"] for s in log),
          f"max locked = {max(s['site_locked_count'] for s in log)}")
    check("never alerts", not any(s["mobile_confirmed"] for s in log))

    # 2 -- a real C2000 site, but no handset transmitting nearby.
    log = scenario("2. C2000 site present, uplink silent",
                   FakeAir(downlinks=DOWNLINKS))
    check("locks the C2000 network", any(s["site_locked_count"] for s in log),
          f"max locked = {max(s['site_locked_count'] for s in log)}")
    check("stays silent with no uplink traffic",
          not any(s["mobile_confirmed"] for s in log))

    # 3 -- the case the product exists for.
    log = scenario("3. C2000 site with a handset transmitting",
                   FakeAir(downlinks=DOWNLINKS, uplinks=UPLINKS[:1]), cycles=18)
    check("locks the C2000 network", any(s["site_locked_count"] for s in log))
    check("alerts on the verified uplink", any(s["mobile_confirmed"] for s in log))
    hits = [s for s in log if s["mobile_confirmed"] and s["peaks"]]
    if hits:
        f = hits[0]["peaks"][0]["freq_hz"]
        check("alert names the right uplink channel", abs(f - UPLINKS[0]) < 1000.0,
              f"{f/1e6:.4f} MHz vs expected {UPLINKS[0]/1e6:.4f} MHz")
        note(f"first alert: {hits[0]['peaks'][0]}")
    else:
        check("alert names the right uplink channel", False, "no alert produced")

    # 4 -- a real TETRA uplink transmission with no base station locked.
    #      Until 0.9.35 this had to stay silent: no verified site meant
    #      nothing to be near, so any alert would have been a guess. That
    #      rule cost more than it bought. Driving, the unit is between locks
    #      most of the time, and 380-385 MHz carries nothing in this country
    #      but emergency-services terminals -- so a transmission that passes
    #      the full waveform test there is evidence on its own, and waiting
    #      for a downlink to corroborate it is waiting through the event.
    #      What replaces the rule is the confirmation count: a channel with
    #      no locked partner has to be heard twice (scenario 22), where a
    #      partner channel still confirms on one.
    log = scenario("4. a real uplink transmission with nothing locked",
                   FakeAir(uplinks=UPLINKS[:1]))
    check("alerts on verified TETRA even with no locked network",
          any(s["mobile_confirmed"] for s in log),
          "alert on cycles %s" % [i for i, s in enumerate(log) if s["mobile_confirmed"]])

    # 5 -- the profile v7 killer: interference sitting on exactly the channel
    #      being watched, with a TETRA-like bandwidth and duty cycle.
    log = scenario("5. interference on the exact uplink partner channel",
                   FakeAir(downlinks=DOWNLINKS,
                           interferers=[(UPLINKS[0], "gated_noise"),
                                        (UPLINKS[1], "wide")]), cycles=18)
    check("still locks the C2000 network", any(s["site_locked_count"] for s in log))
    check("does not alert on interference",
          not any(s["mobile_confirmed"] for s in log))

    # 6 -- the reference unit's real situation: an RTL-SDR spur comb on an
    #      800 kHz grid, stronger than the base station, so the survey's
    #      shortlist fills up with artefacts. The band pass must still reach
    #      the genuine carrier and lock it.
    comb = [(390_012_500.0 + k * 800_000.0, "spur") for k in range(6)]
    comb += [(390_012_500.0 + k * 800_000.0 + 25_000.0, "spur") for k in range(6)]
    log = scenario("6. C2000 site buried under an RTL-SDR spur comb",
                   FakeAir(downlinks=DOWNLINKS, interferers=comb), cycles=40)
    check("locks the real carrier despite the comb",
          any(s["site_locked_count"] for s in log),
          "max locked = %d" % max(s["site_locked_count"] for s in log))
    first = next((i for i, s in enumerate(log) if s["site_locked_count"]), None)
    check("locks inside one band pass rather than after it",
          first is not None and first <= 12,
          "first locked on cycle %s" % first)
    check("never locks a comb tooth",
          all(any(abs(x["freq_hz"] - d) < 1.0 for d in DOWNLINKS)
              for s in log for x in s["site_peaks"]),
          "locked: " + ", ".join(sorted({"%.4f" % (x["freq_hz"] / 1e6)
                                         for s in log for x in s["site_peaks"]})))

    # 7 -- a full band pass must be bounded, not endless.
    log = scenario("7. band pass completes without a network present",
                   FakeAir(interferers=[(391_012_500.0, "spur")]), cycles=30)
    check("never alerts while sweeping an empty band",
          not any(s["mobile_confirmed"] for s in log))
    check("band pass makes progress every cycle",
          log[-1]["site_queue_remaining"] < log[2]["site_queue_remaining"],
          "queue %d -> %d" % (log[2]["site_queue_remaining"],
                              log[-1]["site_queue_remaining"]))

    # 8 -- ETSI only requires the *main* carrier to be continuous. A site's
    #      secondary traffic carriers are discontinuous, and once a call moves
    #      to one, the handset transmits on that carrier's uplink partner. If
    #      only continuous downlinks could lock, those calls would never be
    #      watched at all.
    MAIN = 391_212_500.0
    TRAFFIC = 391_437_500.0
    log = scenario("8. call moved to a discontinuous traffic carrier",
                   FakeAir(downlinks=[MAIN],
                           uplinks=[TRAFFIC - 10_000_000.0],
                           interferers=[(TRAFFIC, "traffic_downlink")]),
                   cycles=26)
    locked_freqs = sorted({round(x["freq_hz"]) for s in log for x in s["site_peaks"]})
    check("locks the discontinuous traffic carrier too",
          round(TRAFFIC) in locked_freqs,
          "locked: " + ", ".join("%.4f" % (f / 1e6) for f in locked_freqs))
    check("alerts on the traffic carrier's uplink",
          any(s["mobile_confirmed"] for s in log))
    hit = next((s for s in log if s["mobile_confirmed"] and s["peaks"]), None)
    check("alert names the traffic uplink channel",
          bool(hit) and abs(hit["peaks"][0]["freq_hz"]
                            - (TRAFFIC - 10_000_000.0)) < 1000.0,
          ("%.4f MHz" % (hit["peaks"][0]["freq_hz"] / 1e6)) if hit else "no alert")

    # 9 -- the antenna comes off. A locked site whose carriers all stop
    #      answering is not a quiet site, it is a receiver that has lost it.
    #      Reporting a locked network in that state tells the user the device
    #      is watching when it is deaf, which is worse than saying nothing.
    air = FakeAir(downlinks=DOWNLINKS)
    log = scenario("9. antenna removed after the network is locked",
                   air, cycles=34, at={12: air.silence},
                   # The lock is now lost by ageing rather than by one
                   # carrier's verdict, so the windows have to be short enough
                   # to expire inside a test that runs in wall-clock seconds.
                   site_reverify_s=0.0, site_silence_window_s=30.0,
                   site_lock_stale_s=60.0)
    before = [s for s in log[:12] if s["site_locked_count"]]
    after = log[12:]
    check("was locked before the antenna came off", bool(before),
          "max locked = %d" % max((s["site_locked_count"] for s in log[:12]),
                                  default=0))
    cleared = next((i for i, s in enumerate(after)
                    if s["site_locked_count"] == 0), None)
    check("drops the lock once nothing answers", cleared is not None,
          "still locked after %d cycles" % len(after) if cleared is None
          else "cleared %d cycles after going silent" % (cleared + 1))
    check("goes back to SEARCHING, not a stale LOCKED",
          after[-1]["detector_state"] == "SEARCHING",
          after[-1]["detector_state"])
    check("never alerts while deaf",
          not any(s["mobile_confirmed"] for s in after))

    # Dwell planning and raster maths, independent of any capture.
    print("\n10. planner and raster")
    centre, members = plan_dwell(UPLINKS, 288_000.0)
    check("one dwell covers a whole site's uplink list", len(members) == len(UPLINKS),
          f"{len(members)}/{len(UPLINKS)} channels")
    check("tuner centre avoids every watched channel",
          all(abs(f - centre) >= 20_000.0 for f, _ in members),
          f"centre {centre/1e6:.4f} MHz")
    check("offsets stay inside the usable window",
          all(abs(o) <= 100_000.0 for _, o in members))
    snapped = raster_snap(381_240_000.0, 380_000_000.0)
    check("raster snaps to the +12.5 kHz TETRA grid",
          abs(snapped - 381_237_500.0) < 1.0, f"{snapped:.0f} Hz")

    check_queue_order()
    check_rescan_while_locked()
    check_one_quiet_carrier_costs_nothing()
    check_ok_total_survives_a_restart()
    check_rescan_with_candidates()
    check_candidate_does_not_starve_the_pass()
    check_stuck_capture_is_refused()
    check_short_transmission_alerts()
    check_follow_up_is_bounded()
    check_sensitive_control_burst()
    check_vehicle_on_an_unlocked_carrier()
    check_unlocked_receiver_still_hears()
    check_unknown_channel_needs_two_hits()
    check_capture_pipeline()
    check_radio_duty_limit()
    check_follow_up_outlasts_a_repeat()
    check_periodic_reporter_is_caught()
    check_hot_channel_rotation()
    check_tracks_and_screen()
    check_priority_lanes()
    check_power_ladder()

    print()
    if FAILURES:
        print(f"{len(FAILURES)} failures")
        for f in FAILURES:
            print("  FAIL " + f)
        return 1
    print("detector-selftest OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
