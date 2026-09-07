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
        wide = sr > 500_000
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


def main():
    global VERBOSE
    ap = argparse.ArgumentParser()
    ap.add_argument("--verbose", action="store_true")
    args = ap.parse_args()
    VERBOSE = args.verbose

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

    # 4 -- a TETRA-shaped transmission with no base station behind it. Real
    #      C2000 handsets never exist without a site, so this must stay silent.
    log = scenario("4. uplink-shaped signal with no C2000 network",
                   FakeAir(uplinks=UPLINKS[:1]))
    check("never alerts without a locked network",
          not any(s["mobile_confirmed"] for s in log))

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
                   site_reverify_s=0.0, site_lost_rounds=3)
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
