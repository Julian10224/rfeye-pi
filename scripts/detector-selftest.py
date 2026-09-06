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
                # Survey resolution only: a 20 kHz hump in the right place.
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
            elif kind == "gated_noise":
                parts.append((sim.gated_noise(dur, sr, 22_000.0, 0.0142, 0.0567,
                                              off, self.seed + 31), 1.0))
            elif kind == "cw":
                parts.append((sim.cw_tone(dur, sr, off), 1.0))
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


def run(backend, cycles):
    log = []
    for i in range(int(cycles)):
        ok = backend._scan_cycle()
        s = backend.snapshot()
        log.append(s)
        note(f"cycle {i:2d} state={s['detector_state']:<9} "
             f"locked={s['site_locked_count']} alert={s['mobile_confirmed']} "
             f"cand={s['site_candidate_count']}"
             + (f" err={s['error'][:50]}" if s["error"] else ""))
        if not ok:
            note("  scan cycle reported failure")
    return log


def scenario(name, air, cycles=14, **overrides):
    print(f"\n{name}")
    with tempfile.TemporaryDirectory() as d:
        b = make_backend(air, os.path.join(d, "sites.json"), **overrides)
        try:
            return run(b, cycles)
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

    # Dwell planning and raster maths, independent of any capture.
    print("\n6. planner and raster")
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
