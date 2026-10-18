#!/usr/bin/env python3
"""Can a Raspberry Pi 3 B+ running RF Eye detect emergency services? A drive, simulated.

    python scripts/simulator/field_test.py                  # Pi 3 B+ speed, 5 min
    python scripts/simulator/field_test.py --pi 20 --minutes 10
    python scripts/simulator/field_test.py --pi 0           # full PC speed

The real application -- rfeye/app.py with its UI on a dummy display, the real
backend, stream, detector and waveform tests -- runs in this process, slowed
to a Pi 3 B+ at its 900 MHz ceiling (pi_mode: --pi 15 matches the three
workloads the README times on the reference unit; 20 is pessimistic). On
Windows only, as the slowdown suspends and resumes the process.

The air runs in a process of its own at full speed (sim_air.serve), so making
it costs the emulated Pi nothing: two C2000 sites, and vehicles that reach the
receiver through multipath fading (channel.py, TETRA TU50/HT200 models or line
of sight), with the dongle's own impairments on top. The unit starts with its
site state as a running unit has it -- locked, main carriers known -- unless
--cold.

Every twenty seconds something else comes past:
  police    own cell, urban fading, 50 km/h, a status/registration every 3 s
  ambulance own cell, line of sight, 80 km/h, every 4 s
  police    neighbouring cell, urban fading, 50 km/h, every 3 s
  fire      own cell, parked by the road, a message every 8 s
  call      own cell, a 3 s push-to-talk, then quiet
  unknown   a cell whose base station this unit cannot hear (random channel)

and the report says, per vehicle, at which of its bursts strong enough to
recognise (>= 8 dB) RF Eye first verified it, and how many seconds after the
first such burst; plus every verified hit that belonged to no vehicle (a false
alarm), samples lost because the Pi did not keep up, and its CPU load.
"""
import argparse
import json
import os
import sys
import tempfile
import threading
import time

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.abspath(os.path.join(HERE, "..", ".."))

KINDS = [
    ("police", {"cell": "local", "model": "TU50", "speed": 50.0, "every": 3.0,
                "drive": (30.0, 30.0, -5.0)}),
    ("ambulance", {"cell": "local", "model": "static", "speed": 80.0, "k_db": 6.0,
                   "every": 4.0, "drive": (20.0, 34.0, -5.0)}),
    ("police-nb", {"cell": "neighbour", "model": "TU50", "speed": 50.0, "every": 3.0,
                   "drive": (30.0, 28.0, -5.0)}),
    ("fire", {"cell": "local", "model": "static", "speed": 0.0, "k_db": 3.0,
              "every": 8.0, "db": 16.0}),
    ("call", {"cell": "local", "model": "TU50", "speed": 30.0, "every": 60.0, "db": 18.0,
              "ptt": 3.0}),
    ("unknown", {"cell": "random", "model": "TU50", "speed": 50.0, "every": 3.0,
                 "drive": (30.0, 30.0, -5.0)}),
]


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--pi", type=float, default=15.0,
                    help="slow-down to emulate a Pi 3 B+ (15 = matched, 20 = pessimistic, 0 = off)")
    ap.add_argument("--cores", type=int, default=1, help="Pi cores to emulate (1 = pessimistic)")
    ap.add_argument("--minutes", type=float, default=5.0)
    ap.add_argument("--cold", action="store_true", help="start without the site state")
    ap.add_argument("--dwell", action="store_true", help="0.10.x dwells instead of the stream")
    ap.add_argument("--clean", action="store_true", help="no fading or receiver impairments")
    ap.add_argument("--seed", type=int, default=1)
    args = ap.parse_args()

    work = tempfile.mkdtemp(prefix="rfeye-field-")
    os.environ["HOME"] = work
    os.environ["RFEYE_CONFIG"] = os.path.join(work, "config.json")
    os.environ["RFEYE_SITE_STATE"] = os.path.join(work, "state", "c2000-sites.json")
    os.environ["RFEYE_DISPLAY_PROFILE"] = "cuqi35"
    os.environ["SDL_VIDEODRIVER"] = "dummy"
    sys.path[:0] = [HERE, os.path.join(REPO, "rfeye")]

    import random
    random.seed(args.seed)
    from sim_air import AirClient, SimDongle
    print("field test: air in its own process...", flush=True)
    air = AirClient(seed=args.seed, realistic=not args.clean)
    air.command("site", "neighbour", True, 12.0)
    dongle = SimDongle(air)

    import sdr_backend
    import buzzer

    class _Quiet:
        def __init__(self, *a, **k): pass
        def close(self): pass
        def off(self): pass
        def beep_pattern(self, *a, **k): pass

    sdr_backend._PersistentRTL = lambda index=0: dongle
    buzzer.GPIOBuzzer = _Quiet
    import app as appmod
    appmod.GPIOBuzzer = _Quiet
    from config import load_config
    cfg = load_config()
    cfg["stream_mode"] = not args.dwell

    if not args.cold:
        # What a running unit holds in c2000-sites.json: both sites, main
        # carriers known as continuous.
        from tetra_detector import STATE_SCHEMA
        now = time.time()
        sites = {}
        for f, cont, q in ((391_187_500, 0.9, 0.8), (391_462_500, 0.0, 0.7),
                           (392_012_500, 0.0, 0.7), (393_637_500, 0.9, 0.4),
                           (394_112_500, 0.0, 0.3)):
            sites[str(f)] = {"hits": 4, "misses": 0, "quality": q, "last_ok": now,
                             "first_seen": now - 3600, "ok_total": 40, "cont": cont}
        path = os.environ["RFEYE_SITE_STATE"]
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w") as fh:
            json.dump({"schema": STATE_SCHEMA,
                       "detector_profile_version": cfg["detector_profile_version"],
                       "saved_at": now, "sites": sites}, fh)

    if args.pi > 0:
        import pi_mode
        pi_mode.become_pi(args.pi, args.cores)
        time.sleep(0.3)

    a = appmod.App(cfg, fullscreen=False)
    b = a.backend
    b._sdr_present = lambda: True
    false_hits = []
    real_publish = b._publish

    def publish(now, watch_results, site_results, **kw):
        ok = [float(r.freq_hz) for r in watch_results if r.ok]
        if ok:
            air.command("verified", ok)
            mine = set()
            for v in air.command("snapshot")["vehicles"]:
                mine.add(int(round(v["mhz"] * 1e6)))
                if v["call_ch"]:
                    mine.add(int(round((300e6 + 12_500 + v["call_ch"] * 25_000 - 10e6))))
            false_hits.extend(f for f in ok if int(round(f)) not in mine)
        return real_publish(now, watch_results, site_results, **kw)

    b._publish = publish

    total = args.minutes * 60.0
    log = []

    def drive():
        t0 = time.monotonic()
        k = 0
        nxt = 15.0
        while a.running and time.monotonic() - t0 < total:
            t = time.monotonic() - t0
            if t >= nxt and t < total - 25.0:
                name, spec = KINDS[k % len(KINDS)]
                spec = dict(spec, name=name, first_after=random.uniform(0.5, 3.0))
                if "db" not in spec:
                    spec["db"] = -5.0
                vid = air.command("add", spec)
                if spec.get("ptt"):
                    air.command("ptt", vid, spec["ptt"])
                log.append((round(t, 1), name, vid))
                k += 1
                nxt += 20.0
            # Vehicles that have driven out of range are taken off the air.
            snap = air.command("snapshot")
            for v in snap["vehicles"]:
                if (not v["driving"] and v["first_tx"] is not None and v["mode"] != "call"
                        and v["db"] <= -5.0):
                    air.command("remove", v["id"])
                    log.append((round(t, 1), "gone", v["id"], v))
            time.sleep(0.5)
        a.running = False

    def finish_after():
        time.sleep(total + 5.0)
        a.running = False

    cpu0, wall0 = time.process_time(), time.monotonic()
    threading.Thread(target=drive, daemon=True).start()
    threading.Thread(target=finish_after, daemon=True).start()
    threading.Timer(total * 4 + 300, lambda: os._exit(3)).start()
    a.run()
    wall = time.monotonic() - wall0
    cpu = time.process_time() - cpu0

    # Everyone still on the air at the end, plus everyone who left.
    vehicles = {v["id"]: v for v in air.command("snapshot")["vehicles"]}
    for entry in log:
        if entry[1] == "gone":
            vehicles.setdefault(entry[2], entry[3])
    names = {e[2]: e[1] for e in log if e[1] != "gone"}
    print()
    print("RF Eye %s field test: %s, %.1f min, %s, %s" % (
        cfg.get("app_version"), ("Pi 3 B+ x%.0f, %d core" % (args.pi, args.cores))
        if args.pi else "full PC speed", wall / 60.0,
        "dwells" if args.dwell else "stream", "cold start" if args.cold else "locked"))
    print("%-10s %-9s %-7s %6s %8s %8s  %s" % ("vehicle", "cell", "channel", "sent",
                                                "audible", "seen at", "after first audible"))
    rows = []
    for vid in sorted(vehicles):
        v = vehicles[vid]
        got = v.get("seen_at")
        rows.append((names.get(vid, v["name"]), v, got))
        print("%-10s %-9s %-7s %6d %8d %8s  %s" % (
            names.get(vid, v["name"]), v["cell"], "CH %d" % v["ch"], v["sent"], v["audible"],
            ("#%d" % got[1]) if got else "never",
            ("%.1f s" % got[2]) if got else "-"))
    audible = [r for r in rows if r[1]["audible"] > 0]
    first = sum(1 for r in audible if r[2] and r[2][1] == 1)
    within2 = sum(1 for r in audible if r[2] and r[2][1] <= 2)
    seen = sum(1 for r in audible if r[2])
    print()
    print("vehicles with a recognisable burst: %d; seen on the first one: %d; within two: %d; "
          "at all: %d" % (len(audible), first, within2, seen))
    for cell in ("local", "neighbour", "random"):
        sub = [r for r in audible if r[1]["cell"] == cell]
        if sub:
            print("  %-9s first %d/%d  within two %d/%d  at all %d/%d" % (
                cell, sum(1 for r in sub if r[2] and r[2][1] == 1), len(sub),
                sum(1 for r in sub if r[2] and r[2][1] <= 2), len(sub),
                sum(1 for r in sub if r[2]), len(sub)))
    print("false alarms (verified hits on no vehicle's channel): %d %s" % (
        len(false_hits), sorted({round(f / 1e6, 4) for f in false_hits})[:8]))
    print("samples lost: USB %.2f s, behind %.2f s;  Pi core load %s" % (
        dongle.usb_lost / 2.016e6, b.stream_drops / 2.016e6,
        ("%.0f%%" % (100 * cpu / wall * args.pi)) if args.pi else "-"))
    print("logs:", os.path.join(work, "state"))
    sys.stdout.flush()
    air.close()
    os._exit(0)


if __name__ == "__main__":
    import multiprocessing as mp
    mp.freeze_support()
    main()
