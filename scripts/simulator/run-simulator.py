#!/usr/bin/env python3
"""RF Eye on a PC: the real app, a simulated dongle, and C2000 vehicles to drive past it.

    python scripts/simulator/run-simulator.py
    python scripts/simulator/run-simulator.py --scale 1.4 --dwell

Two windows open:

  * **RF Eye** -- the real application (rfeye/app.py and the real backend,
    detector and waveform tests), drawn as the portrait panel shows it. Click
    where you would tap. Its buzzer comes out of the PC speaker.
  * **Bedienpaneel** -- the simulated air: a C2000 base station, and vehicles
    you add, move closer or further away (signal level), let send short
    status/registration bursts, key up for a call (PTT), or drive past.

Only librtlsdr is replaced: the simulated dongle synthesises real pi/4-DQPSK
TETRA for whatever frequency the backend tunes, at 2.016 MS/s by the wall
clock, through the same calls (sync dwells and the async stream). Settings,
site state and logs go to a throw-away directory; the unit's own are never
touched. Nothing here is shipped in the OTA package.
"""
import argparse
import math
import multiprocessing as mp
import os
import queue
import random
import sys
import tempfile
import threading
import time

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.abspath(os.path.join(HERE, "..", ".."))


class PcBuzzer:
    """The TMB12A03 pattern player, on the sound card instead of GPIO26."""

    def __init__(self, *args, **kwargs):
        self.available = False
        self.generation = 0
        self.lock = threading.Lock()
        try:
            import numpy as np
            import pygame
            if not pygame.mixer.get_init():
                pygame.mixer.init(frequency=44100, size=-16, channels=1)
            rate, _, chans = pygame.mixer.get_init()
            t = np.arange(int(rate * 0.5)) / float(rate)
            wave = (np.sign(np.sin(2 * math.pi * 2700.0 * t)) * 0.18 * 32767).astype(np.int16)
            if chans > 1:
                wave = np.repeat(wave[:, None], chans, axis=1)
            self.tone = pygame.sndarray.make_sound(np.ascontiguousarray(wave))
            self.available = True
        except Exception as exc:
            print("simulator: no sound (%s)" % exc, flush=True)

    def beep(self, frequency=2700, duration_ms=80, duty=50):
        self.beep_pattern([(int(duration_ms), 0)])

    def beep_pattern(self, pattern, frequency=2700, duty=50):
        if not self.available:
            return
        with self.lock:
            self.generation += 1
            token = self.generation
        clean = [(max(1, int(on)), max(0, int(gap))) for on, gap in pattern]
        threading.Thread(target=self._play, args=(token, clean), daemon=True).start()

    def _play(self, token, pattern):
        for on, gap in pattern:
            if token != self.generation:
                return
            self.tone.play(maxtime=on)
            time.sleep((on + gap) / 1000.0)

    def off(self):
        with self.lock:
            self.generation += 1
        if self.available:
            self.tone.stop()

    def close(self):
        self.off()


def selftest(a, air, cmd_q, work, seconds):
    """Scripted run without windows: what a person at the panel would do.

    Both sites are on from the start, as they would be on a unit that has
    been running: the site state survives a restart, so a unit in the field
    is locked from boot. Vehicles come once the locks are there.
    """
    import pygame
    t0 = time.monotonic()
    plan = [(0.0, ("site", "neighbour", True, 12.0)),
            (45.0, ("scenario", "police_driveby")),
            (80.0, ("scenario", "police_neighbour")),
            (115.0, ("scenario", "ambulance_call")),
            (150.0, ("scenario", "fire_bursts"))]
    seen, shots = [], 0
    while a.running and time.monotonic() - t0 < seconds:
        t = time.monotonic() - t0
        while plan and t >= plan[0][0]:
            cmd_q.put(plan.pop(0)[1])
        snap = a.backend.snapshot()
        state = snap.get("detector_state", "")
        key = (state, snap.get("site_locked_count", 0))
        if not seen or seen[-1][1:] != key:
            seen.append((round(t, 1),) + key)
            print("selftest: %5.1f s  %-9s locked=%d  control=%s" % (
                t, state, key[1], sorted(a.backend.sites.control_partners())), flush=True)
            if state == "ALERT" and shots < 4:
                shots += 1
                pygame.image.save(a.ui, os.path.join(work, "shot-%d-%s.png" % (shots, state)))
        time.sleep(0.25)
    print("selftest: first seen, counted in the vehicle's own bursts:", flush=True)
    for v in air.command("snapshot")["vehicles"]:
        got = v["seen_at"]
        print("selftest:   %-10s %-9s CH %d  sent %3d  seen at burst %s" % (
            v["name"], v["cell"], v["ch"], v["sent"],
            ("%d (audible #%d)" % tuple(got[:2])) if got else "never"), flush=True)
    a.running = False


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--scale", type=float, default=1.6, help="window size (1.0 = 320x480)")
    ap.add_argument("--dwell", action="store_true", help="dwells instead of the stream (0.10.x)")
    ap.add_argument("--eco", action="store_true", help="start in ECO (low power) mode")
    ap.add_argument("--no-site", action="store_true", help="start without a base station")
    ap.add_argument("--clean", action="store_true",
                    help="no fading or receiver impairments (the unit tests' clean air)")
    ap.add_argument("--selftest", type=float, default=0.0, metavar="SECONDS",
                    help="no windows: play the scenarios, save screenshots, report")
    args = ap.parse_args()
    if args.selftest:
        os.environ["SDL_VIDEODRIVER"] = "dummy"

    work = tempfile.mkdtemp(prefix="rfeye-sim-")
    os.environ["HOME"] = work
    os.environ["RFEYE_CONFIG"] = os.path.join(work, "config.json")
    os.environ["RFEYE_SITE_STATE"] = os.path.join(work, "state", "c2000-sites.json")
    os.environ["RFEYE_DISPLAY_PROFILE"] = "cuqi35"
    sys.path.insert(0, os.path.join(REPO, "rfeye"))
    sys.path.insert(0, HERE)
    print("simulator: settings, site state and logs in", work, flush=True)

    from sim_air import Air, SimDongle
    from sim_panel import run_panel

    air = Air(realistic=not args.clean)
    air.command("site", "local", not args.no_site, 22.0)
    dongle = SimDongle(air)

    import sdr_backend
    import buzzer
    sdr_backend._PersistentRTL = lambda index=0: dongle
    buzzer.GPIOBuzzer = PcBuzzer
    import pygame
    import app as appmod
    appmod.GPIOBuzzer = PcBuzzer
    from config import load_config

    cfg = load_config()
    cfg["stream_mode"] = not args.dwell
    cfg["low_power_mode"] = bool(args.eco)

    cmd_q, status_q = mp.Queue(), mp.Queue(maxsize=4)
    panel = mp.Process(target=run_panel, args=(cmd_q, status_q), daemon=True)
    if not args.selftest:
        panel.start()

    a = appmod.App(cfg, fullscreen=False)
    b = a.backend
    b._sdr_present = lambda: True

    # The panel as it is seen: portrait, scaled, and a click is a tap.
    s = max(0.5, float(args.scale))
    size = (int(320 * s), int(480 * s))
    a.screen = pygame.display.set_mode(size)
    pygame.display.set_caption("RF Eye %s - simulator" % cfg.get("app_version", ""))
    pygame.mouse.set_visible(True)
    a.mouse_hide_delay = 1e9

    def present():
        a.screen.blit(pygame.transform.smoothscale(a.ui, size), (0, 0))

    def events():
        for e in pygame.event.get():
            a.ui_wake_at = time.monotonic()
            if e.type == pygame.QUIT:
                a.running = False
            elif e.type == pygame.MOUSEBUTTONDOWN and e.button == 1:
                a.ui_touch_at = a.ui_wake_at
                a._tap(int(e.pos[0] / s), int(e.pos[1] / s))
            elif e.type == pygame.KEYDOWN:
                a.ui_touch_at = a.ui_wake_at
                if e.key == pygame.K_ESCAPE:
                    a.page = "main"
                elif a.page == "wifi" and getattr(a, "wifi_selected", None):
                    if e.key == pygame.K_RETURN:
                        a._wifi_connect()
                    elif e.key == pygame.K_BACKSPACE:
                        a.wifi_password = a.wifi_password[:-1]
                    elif e.unicode and e.unicode.isprintable():
                        a.wifi_password += e.unicode

    a._present_rotated = present
    a._events = events

    # When RF Eye first verified each vehicle, counted in its own bursts --
    # the number that says whether the first message was heard.
    real_publish = b._publish

    def publish(now, watch_results, site_results, **kw):
        ok = [float(r.freq_hz) for r in watch_results if r.ok]
        if ok:
            air.command("verified", ok)
        return real_publish(now, watch_results, site_results, **kw)

    b._publish = publish

    def apply(cmd):
        if cmd[0] == "quit":
            a.running = False
        else:
            air.command(*cmd)

    def pump():
        last = 0.0
        while a.running:
            try:
                apply(cmd_q.get(timeout=0.1))
            except queue.Empty:
                pass
            except Exception as exc:
                print("simulator: command failed:", exc, flush=True)
            now = time.monotonic()
            if now - last >= 0.3:
                last = now
                snap = b.snapshot()
                if snap.get("stream_active"):
                    c = snap.get("stream_centre_hz", 0.0) / 1e6
                    window = "%.3f-%.3f MHz" % (c - 0.85, c + 0.85)
                    mode = "stream"
                else:
                    c = snap.get("dwell_centre_hz", 0.0) / 1e6
                    window = "%.3f MHz (%s)" % (c, snap.get("dwell_role", ""))
                    mode = "dwell" if snap.get("status") == "LIVE" else snap.get("status", "")
                st = {"air": air.command("snapshot"), "rfeye": {
                    "state": snap.get("detector_state", ""),
                    "provisional": bool(snap.get("alert_provisional")),
                    "mode": mode, "window": window,
                    "locked": int(snap.get("site_locked_count", 0)),
                    "triggers": int(snap.get("stream_triggers", 0)),
                    "peaks": [(int(p.get("channel", 0)), float(p.get("level", 0.0)))
                              for p in snap.get("peaks", [])]}}
                try:
                    status_q.put_nowait(st)
                except queue.Full:
                    pass

    threading.Thread(target=pump, name="sim-panel-pump", daemon=True).start()
    if args.selftest:
        threading.Thread(target=selftest, args=(a, air, cmd_q, work, args.selftest),
                         daemon=True).start()
    try:
        a.run()
    finally:
        try:
            status_q.put_nowait({"closed": True})
        except Exception:
            pass
        time.sleep(0.3)
        if panel.is_alive():
            panel.terminate()
        print("simulator: logs (hits.log, power.log) are in", os.path.join(work, "state"),
              flush=True)


if __name__ == "__main__":
    mp.freeze_support()
    main()
