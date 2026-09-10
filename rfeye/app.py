#!/usr/bin/env python3
import argparse
import math
import os
import time
import threading
import subprocess
import traceback
from pathlib import Path

os.environ.setdefault("PYGAME_HIDE_SUPPORT_PROMPT", "1")

print(f"RFEYE_BOOT python-entry {time.monotonic():.3f}", flush=True)
import pygame
print(f"RFEYE_BOOT pygame-imported {time.monotonic():.3f}", flush=True)

from config import load_config, save_config
from buzzer import GPIOBuzzer
from updater import fetch_manifest, download_update, install_zip_bytes, version_tuple

BG = (2, 3, 5)
PANEL = (8, 9, 12)
BLUE = (0, 152, 222)
BLUE_BRIGHT = (28, 190, 255)
WHITE = (224, 229, 236)
DIM = (82, 90, 100)
SEG_OFF = (34, 35, 31)
GREEN = (57, 205, 91)
YELLOW = (243, 192, 56)
ORANGE = (243, 128, 32)
RED = (230, 54, 54)

# How long a touch or a visible change keeps the UI at the active frame rate.
UI_WAKE_S = 4.0

# The supply notice closes itself. It reports something the user cannot do
# anything about from the passenger seat, and a modal left standing over a
# detector in a moving car is worse than the warning is useful.
POWER_NOTICE_TIMEOUT_S = 30.0

# A frame that keeps failing is handed back to systemd rather than sat in;
# see App._guarded_frame.
FRAME_ERROR_RESTART_FRAMES = 60


def boot_note(text):
    """Append one line to the boot log, and never fail doing it.

    A unit that goes dark after a power cut leaves nothing behind to look at:
    the panel says nothing by definition, and the operator has already pulled
    the plug on whatever was on screen. This file is the smallest thing that
    survives that, and it is what separates "the app never started" from "the
    app started and then something went wrong".
    """
    try:
        path = Path.home() / ".local" / "state" / "rfeye" / "boot.log"
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.exists() and path.stat().st_size > 131072:
            keep = path.read_text().splitlines()[-300:]
            path.write_text(chr(10).join(keep) + chr(10))
        stamp = time.strftime("%Y-%m-%dT%H:%M:%S")
        with path.open("a") as fh:
            fh.write("%s pid=%d %s%s" % (stamp, os.getpid(), text, chr(10)))
    except Exception:
        pass


def clamp(v, lo=0.0, hi=1.0):
    return max(lo, min(hi, v))


def _split_nmcli_terse(line, expected_fields):
    """Split nmcli terse output while honoring its backslash escaping."""
    fields = []
    current = []
    escaped = False
    for ch in str(line):
        if escaped:
            current.append(ch)
            escaped = False
        elif ch == "\\":
            escaped = True
        elif ch == ":" and len(fields) < int(expected_fields) - 1:
            fields.append("".join(current))
            current = []
        else:
            current.append(ch)
    if escaped:
        current.append("\\")
    fields.append("".join(current))
    return fields


class App:
    def __init__(self, cfg, fullscreen=True):
        self.cfg = cfg
        if os.getenv("WAYLAND_DISPLAY"):
            sock=Path(os.getenv("XDG_RUNTIME_DIR", f"/run/user/{os.getuid()}")) / os.getenv("WAYLAND_DISPLAY")
            while not sock.exists():
                time.sleep(0.05)
        print(f"RFEYE_BOOT wayland-ready {time.monotonic():.3f}", flush=True)
        pygame.display.init()
        pygame.font.init()

        self.uw = int(cfg["ui_width"])
        self.uh = int(cfg["ui_height"])
        self.pw = int(cfg["physical_width"])
        self.ph = int(cfg["physical_height"])

        flags = pygame.NOFRAME
        self.screen = pygame.display.set_mode((self.pw, self.ph), flags)
        print(f"RFEYE_BOOT display-ready {time.monotonic():.3f}", flush=True)
        pygame.display.set_caption(cfg.get("title", "RF EYE"))

        self.ui = pygame.Surface((self.uw, self.uh))
        self.mouse_hide_delay = 2.5
        self.last_mouse_motion = 0.0
        pygame.mouse.set_visible(False if fullscreen else True)

        self.font_s = pygame.font.Font(None, 23)
        self.font_m = pygame.font.Font(None, 30)
        self.font_l = pygame.font.Font(None, 40)
        self.font_xl = pygame.font.Font(None, 56)
        self.font_boot = pygame.font.Font(None, 54 if self.uw <= 320 else 82)

        self._startup_splash(0.18, "STARTING")

        self.settings_icon = None
        if os.getenv("RFEYE_DISPLAY_PROFILE", "").lower() != "cuqi35":
            try:
                icon_path = Path(__file__).resolve().parent / "assets" / "settings_icon.png"
                icon = pygame.image.load(str(icon_path)).convert_alpha()
                self.settings_icon = pygame.transform.smoothscale(icon, (48, 48))
            except Exception:
                self.settings_icon = None

        from sdr_backend import SDRBackend
        self.backend = SDRBackend(cfg)
        self.backend.start()
        print(f"RFEYE_BOOT backend-started {time.monotonic():.3f}", flush=True)

        self.page = "main"
        self.running = True
        # Idle pacing. The appliance spends almost all of its life on the main
        # page with nobody touching it, and drawing that page eight times a
        # second is the largest single load on the Pi. ui_wake lets the touch
        # thread cut the wait short, so a slower idle rate does not become a
        # slower response.
        self.ui_wake = threading.Event()
        self.ui_wake_at = 0.0
        self.ui_signature = None
        self._dim_overlay = None
        self._dim_alpha = -1
        self.last_beep = 0.0
        # One supply warning per session, shown over whatever page is up and
        # dismissed with a button. Scanning runs in its own thread and is not
        # touched by any of this -- the notice reports the problem, it does
        # not stop the search.
        self.power_notice_open = False
        self.power_notice_done = False
        self.power_notice_lines = []
        self.power_notice_at = 0.0
        # A frame that raises must not be able to take the appliance down.
        self.frame_error = None
        self.frame_error_logged = ""
        self.frame_error_count = 0
        self.frame_error_streak = 0
        self.first_frame_done = False
        self.debug_frame_ms = 0.0
        self.debug_last_frame = time.perf_counter()
        self.ready_chime_done = False
        self.update_message = "CHECK"
        self.update_busy = False
        self.update_manifest = None
        self.wifi_networks = []
        self.wifi_selected = None
        self.wifi_password = ""
        self.wifi_message = ""
        self.wifi_details = None
        self.wifi_scan_busy = False
        self.wifi_last_scan = 0.0
        # TMB12A03 active buzzer on BCM GPIO26 / physical pin 37.
        self.cfg["buzzer_gpio"] = 26
        self.cfg["buzzer_passive"] = False
        self.cfg["buzzer_model"] = "TMB12A03"
        self.buzzer = GPIOBuzzer(
            pin=self.cfg.get("buzzer_gpio", 18),
            passive=self.cfg.get("buzzer_passive", True),
            active_high=self.cfg.get("buzzer_active_high", True),
        )

    def _startup_splash(self, progress, status):
        self.ui.fill((0, 0, 0))
        cx = self.uw // 2
        title_y = int(self.uh * 0.394)
        status_y = int(self.uh * 0.478)
        track_w = max(180, int(self.uw * 0.77))
        track_h = max(10, int(self.uh * 0.0175))
        track_x = (self.uw - track_w) // 2
        track_y = int(self.uh * 0.556)
        self._text("RF EYE", cx, title_y, self.font_boot, BLUE_BRIGHT, center=True)
        self._text(status, cx, status_y, self.font_s, (86, 126, 146), center=True)
        self._text("Made by: Julian", cx, int(self.uh * 0.92), self.font_s, (70, 95, 108), center=True)
        track = pygame.Rect(track_x, track_y, track_w, track_h)
        radius = max(4, track_h // 2)
        pygame.draw.rect(self.ui, (18, 27, 34), track, border_radius=radius)
        pygame.draw.rect(self.ui, (43, 67, 80), track, 1, border_radius=radius)
        fill_w = max(6, int((track.width - 4) * max(0.0, min(1.0, progress))))
        pygame.draw.rect(
            self.ui,
            BLUE_BRIGHT,
            (track.x + 2, track.y + 2, fill_w, max(4, track.height - 4)),
            border_radius=max(2, radius - 2),
        )
        self._present_rotated()
        pygame.display.flip()
        pygame.event.pump()

    def _make_beep(self, freq, ms, volume):
        try:
            sr = 22050
            import numpy as np
            t = np.linspace(0, ms / 1000.0, int(sr * ms / 1000.0), False)
            wave = (np.sin(2 * np.pi * freq * t) * 32767 * volume).astype(np.int16)
            return pygame.sndarray.make_sound(wave)
        except Exception:
            return None

    def _ui_busy(self):
        """Is anything happening that a slower frame rate would spoil?

        Everything here is a reason a person is either looking at the screen
        or about to: a recent touch, a page that is not the passive one, a
        notice waiting to be dismissed, a recording counting down, or the
        detector changing its mind. Outside those the panel shows the same
        picture frame after frame.
        """
        if time.monotonic() - float(getattr(self, "ui_wake_at", 0.0)) < UI_WAKE_S:
            return True
        if self.page != "main":
            return True
        if getattr(self, "power_notice_open", False):
            return True
        if bool(getattr(self, "rf_recording", False)):
            return True
        return False

    def _fps(self):
        """Frame rate for this frame, so the low power setting applies at once."""
        if not bool(self.cfg.get("low_power_mode", False)):
            return max(1, int(self.cfg.get("ui_fps", 20)))
        active = max(1, int(self.cfg.get("low_power_ui_fps", 8)))
        idle = max(1, int(self.cfg.get("low_power_idle_ui_fps", 3)))
        if idle >= active or self._ui_busy():
            return active
        return idle

    def run(self):
        boot_note("ui-loop v%s profile=%s %dx%d" % (
            self.cfg.get("app_version", "?"),
            getattr(self, "display_profile", "default"), self.uw, self.uh))

        while self.running:
            started = time.monotonic()
            self._guarded_frame()
            # Waiting on an event rather than sleeping a fixed slice is what
            # makes a low idle rate usable: a touch wakes the loop at once
            # instead of up to a third of a second later. The floor is the
            # fastest rate the appliance ever runs at, so a drag cannot spin
            # this into a busy loop.
            floor = 1.0 / float(max(1, int(self.cfg.get("ui_fps", 20))))
            target = 1.0 / float(max(1, self._fps()))
            while self.running:
                left = target - (time.monotonic() - started)
                if left <= 0:
                    break
                if self.ui_wake.wait(left):
                    self.ui_wake.clear()
                    target = floor

        boot_note("exit faults=%d streak=%d last=%s" % (
            self.frame_error_count, self.frame_error_streak,
            self.frame_error_logged or "none"))
        self.backend.stop()
        self.buzzer.close()
        pygame.quit()

    def _guarded_frame(self):
        """Draw one frame; report a failure instead of dying of it.

        An exception used to leave ``run`` and end the process. systemd
        restarts half a second later, so a fault that repeats every frame
        became a restart loop, and all the panel showed was the compositor's
        background: a black screen carrying no information and offering no
        way back in from the touchscreen. Whatever else is broken, the
        appliance has to stay up and say what happened.
        """
        try:
            self._frame()
            self.frame_error_streak = 0
            if not self.first_frame_done:
                self.first_frame_done = True
                boot_note("first-frame")
        except Exception as exc:
            self._note_frame_error(exc)
            self.frame_error_streak += 1
            try:
                self._draw_frame_error()
                self._present_rotated()
                pygame.display.flip()
            except Exception:
                pass
            # A fault that never clears is not something to sit in. Until
            # 0.9.7 an exception ended the process and systemd started a fresh
            # one half a second later, and that restart is what carried a unit
            # through a transient failure at boot -- a display that was not
            # ready yet, a device that had not enumerated yet. Catching
            # everything took that recovery away and could leave a unit stuck
            # in a fault for ever. Show the fault long enough to read, then
            # hand the recovery back.
            limit = int(self.cfg.get("frame_error_restart_frames",
                                     FRAME_ERROR_RESTART_FRAMES))
            if self.frame_error_streak >= max(1, limit):
                boot_note("restarting after %d consecutive failed frames: %s"
                          % (self.frame_error_streak, self.frame_error))
                self.running = False

    def _frame(self):
        """One UI frame. Anything raised here is caught by ``_guarded_frame``."""
        self.frame_error = None
        now_frame = time.perf_counter()
        self.debug_frame_ms = (now_frame - self.debug_last_frame) * 1000.0
        self.debug_last_frame = now_frame
        self._events()
        if self.last_mouse_motion and time.time() - self.last_mouse_motion > self.mouse_hide_delay:
            pygame.mouse.set_visible(False)
            self.last_mouse_motion = 0.0
        snap = self.backend.snapshot()
        # A change the screen would show is a reason to be quick again --
        # an alert above all, which must not wait out an idle frame.
        signature = (snap.get("status"), snap.get("detector_state"),
                     int(snap.get("site_locked_count", 0) or 0),
                     bool(snap.get("mobile_confirmed")),
                     bool(snap.get("power_warning")))
        if signature != self.ui_signature:
            self.ui_signature = signature
            self.ui_wake_at = time.monotonic()
        sound_snap = snap
        if self.page == "recording_replay":
            replay_snap = getattr(self, "recording_replay_snapshot", None)
            if replay_snap and bool(getattr(self, "recording_replay_running", False)):
                sound_snap = replay_snap
            elif replay_snap:
                sound_snap = dict(replay_snap)
                sound_snap["peaks"] = []
        self._sound_logic(sound_snap)

        if self.page == "main":
            self._draw_main(snap)
        elif self.page == "settings":
            self._draw_settings()
        elif self.page == "wifi":
            self._draw_wifi()
        elif self.page == "debug":
            self._draw_debug(snap)
        elif self.page == "calibration":
            self._draw_calibration()
        elif self.page == "record_confirm":
            self._draw_record_confirm()
        elif self.page == "demo_confirm":
            self._draw_demo_confirm()
        elif self.page == "recordings":
            self._draw_recordings()
        elif self.page == "recording_detail":
            self._draw_recording_detail()
        elif self.page == "recording_delete_confirm":
            self._draw_recording_delete_confirm()
        elif self.page == "recording_replay":
            self._draw_recording_replay()
        else:
            # There is no spectrum page any more; an unknown page falls back
            # to the one the appliance exists to show.
            self.page = "main"
            self._draw_main(snap)

        self._power_notice_update(snap)
        if self.power_notice_open:
            self._draw_power_notice()

        self._apply_brightness()
        self._present_rotated()
        pygame.display.flip()

    # -- fault reporting ---------------------------------------------------
    def _note_frame_error(self, exc):
        """Record a failing frame once per distinct fault.

        Written to disk as well as the screen: the unit that needs this most
        is one in a car with nobody watching it, and a fault that has already
        scrolled past is exactly the one worth having afterwards.
        """
        detail = traceback.format_exc()
        self.frame_error = "%s: %s" % (type(exc).__name__, exc)
        self.frame_error_count += 1
        if self.frame_error_logged != self.frame_error:
            self.frame_error_logged = self.frame_error
            print(detail, flush=True)
            try:
                path = Path.home() / ".local" / "state" / "rfeye" / "crash.log"
                path.parent.mkdir(parents=True, exist_ok=True)
                if path.exists() and path.stat().st_size > 262144:
                    keep = path.read_text().splitlines()[-400:]
                    path.write_text(chr(10).join(keep) + chr(10))
                header = "%s v%s page=%s" % (
                    time.strftime("%Y-%m-%dT%H:%M:%S"),
                    self.cfg.get("app_version", "?"), self.page)
                with path.open("a") as fh:
                    fh.write(header + chr(10) + detail + chr(10))
            except Exception:
                pass

    def _draw_frame_error(self):
        """Say what went wrong, on the panel, in the space available."""
        self.ui.fill((10, 3, 3))
        pygame.draw.rect(self.ui, RED, (2, 2, self.uw - 4, self.uh - 4), 2)
        cx = self.uw // 2
        self._text("RF EYE FAULT", cx, int(self.uh * 0.14), self.font_m, RED,
                   center=True)
        self._text("v%s  page %s  x%d" % (self.cfg.get("app_version", "?"),
                                          self.page, self.frame_error_count),
                   cx, int(self.uh * 0.21), self.font_s, DIM, center=True)
        y = int(self.uh * 0.32)
        limit = max(18, int(self.uw / 7))
        text = str(self.frame_error or "unknown")
        while text and y < self.uh - 60:
            self._text(text[:limit], cx, y, self.font_s, WHITE, center=True)
            text = text[limit:]
            y += 18
        self._text("details in crash.log", cx, self.uh - 34, self.font_s, DIM,
                   center=True)

    def _present_rotated(self):
        rot = self.cfg.get("rotation", "cw")
        out = pygame.transform.rotate(self.ui, 90 if rot == "ccw" else -90)
        self.screen.blit(out, (0, 0))

    def _physical_to_ui(self, px, py):
        rot = self.cfg.get("rotation", "cw")
        if rot == "ccw":
            ux = self.uw - 1 - py
            uy = px
        else:
            ux = py
            uy = self.uh - 1 - px

        ux = max(0, min(self.uw - 1, int(ux)))
        uy = max(0, min(self.uh - 1, int(uy)))

        if self.cfg.get("touch_invert_x", False):
            ux = self.uw - 1 - ux
        if self.cfg.get("touch_invert_y", False):
            uy = self.uh - 1 - uy
        return ux, uy

    def _events(self):
        for e in pygame.event.get():
            self.ui_wake_at = time.monotonic()
            if e.type == pygame.QUIT:
                self.running = False
            elif e.type == pygame.KEYDOWN:
                if self.page == "wifi" and self.wifi_selected:
                    if e.key == pygame.K_RETURN:
                        self._wifi_connect()
                    elif e.key == pygame.K_BACKSPACE:
                        self.wifi_password = self.wifi_password[:-1]
                    elif e.key == pygame.K_ESCAPE:
                        self.wifi_selected = None
                        self.wifi_password = ""
                    elif e.unicode and e.unicode.isprintable() and len(self.wifi_password) < 63:
                        self.wifi_password += e.unicode
                elif e.key == pygame.K_ESCAPE:
                    if self.page == "main":
                        self.running = False
                    else:
                        self.page = "main"
                elif e.key == pygame.K_s:
                    self.page = "settings"
                elif e.key == pygame.K_d:
                    self._toggle_demo()
                elif e.key == pygame.K_m:
                    self._toggle_mute()
            elif e.type == pygame.MOUSEMOTION:
                self.last_mouse_motion = time.time()
                pygame.mouse.set_visible(True)
            elif e.type == pygame.MOUSEBUTTONDOWN:
                self.last_mouse_motion = time.time()
                pygame.mouse.set_visible(True)
                ux, uy = self._physical_to_ui(*e.pos)
                self._tap(ux, uy)
            elif e.type == pygame.FINGERDOWN:
                px = int(e.x * self.pw)
                py = int(e.y * self.ph)
                ux, uy = self._physical_to_ui(px, py)
                self._tap(ux, uy)

    # -- supply notice -----------------------------------------------------
    def _power_notice_update(self, snap):
        """Raise the notice when the 5 V rail sags, and back off the load.

        Max power is the setting most likely to have caused the sag, so a live
        under-voltage drops it before anything else: complaining about a
        supply while continuing to load it as hard as possible would be an odd
        way to help. That is a change the user asked for and did not make, so
        the notice comes back to say so even if it has already been dismissed
        once this session.
        """
        if self.power_notice_open and self._power_notice_left() <= 0.0:
            self.power_notice_open = False
            self.power_notice_done = True
        if not snap.get("power_warning"):
            return
        reverted = False
        if not bool(self.cfg.get("low_power_mode", True)):
            self.cfg["low_power_mode"] = True
            try:
                save_config(self.cfg)
            except Exception:
                pass
            reverted = True
            self.power_notice_done = False
        if self.power_notice_done or self.power_notice_open:
            return
        detail = str(snap.get("power_detail") or "").strip()
        self.power_notice_lines = [
            "5V rail below 4.63 V",
            detail or "measured by the Pi firmware",
            "Max power switched off" if reverted
            else "Scanning continues in the background",
        ]
        self.power_notice_open = True
        self.power_notice_at = time.monotonic()

    def _power_notice_left(self):
        """Seconds still on the clock, never below zero."""
        limit = max(1.0, float(self.cfg.get("power_notice_timeout_s",
                                            POWER_NOTICE_TIMEOUT_S)))
        return max(0.0, limit - (time.monotonic() - float(self.power_notice_at)))

    def _power_notice_rect(self):
        w = int(self.uw * 0.88)
        h = max(150, int(self.uh * 0.30))
        return pygame.Rect((self.uw - w) // 2, (self.uh - h) // 2, w, h)

    def _power_notice_button(self):
        box = self._power_notice_rect()
        bw = int(box.width * 0.52)
        bh = max(34, int(box.height * 0.24))
        return pygame.Rect(box.centerx - bw // 2,
                           box.bottom - bh - max(10, int(box.height * 0.09)),
                           bw, bh)

    def _draw_power_notice(self):
        shade = pygame.Surface((self.uw, self.uh), pygame.SRCALPHA)
        shade.fill((0, 0, 0, 170))
        self.ui.blit(shade, (0, 0))
        box = self._power_notice_rect()
        pygame.draw.rect(self.ui, (16, 18, 23), box, border_radius=12)
        pygame.draw.rect(self.ui, RED, box, 2, border_radius=12)
        self._text("USB POWER TOO LOW", box.centerx,
                   box.top + max(18, int(box.height * 0.14)),
                   self.font_m, RED, center=True)
        y = box.top + max(44, int(box.height * 0.33))
        for line in self.power_notice_lines[:3]:
            self._text(line, box.centerx, y, self.font_s, WHITE, center=True)
            y += 19
        btn = self._power_notice_button()
        pygame.draw.rect(self.ui, (0, 96, 142), btn, border_radius=9)
        pygame.draw.rect(self.ui, BLUE_BRIGHT, btn, 1, border_radius=9)
        # The count is on the button, not beside it: it is the same promise --
        # this goes away, either because you said so or because it ran out.
        # Rounded up, so it opens on the full 30 rather than on 29; reaching
        # zero and the notice closing are the same moment.
        left = self._power_notice_left()
        self._text("BEGREPEN (%d)" % math.ceil(left),
                   btn.centerx, btn.centery, self.font_m, WHITE, center=True)

    def _power_notice_tap(self, x, y):
        """Swallow the tap while the notice is up; return True if handled."""
        if not self.power_notice_open:
            return False
        if self._power_notice_button().collidepoint(int(x), int(y)):
            self.power_notice_open = False
            self.power_notice_done = True
        return True

    def _tap(self, x, y):
        if self._power_notice_tap(x, y):
            return
        if self.page == "main":
            if x <= 82 and y <= 82:
                self.page = "settings"
                return
            if x < 120 and y > 655:
                self._toggle_mute()
                return

        elif self.page == "settings":
            if y < 90:
                self.page = "main"
                return

            top = 104
            rh = 58
            idx = int((y - top) / rh)
            if idx < 0:
                return

            keys = [
                "muted",
                "demo_mode",
                "audio_mode",
                "brightness",
                "show_frequency",
                "wifi",
                "update",
                "debug",
            ]
            if idx >= len(keys):
                return
            key = keys[idx]

            if key == "muted":
                self._toggle_mute()
            elif key == "demo_mode":
                self._toggle_demo()
                self.page = "main"
            elif key == "audio_mode":
                self.cfg["audio_mode"] = "standard" if self.cfg.get("audio_mode") == "adaptive" else "adaptive"
                save_config(self.cfg)
            elif key == "brightness":
                v = round(float(self.cfg.get("brightness", 1.0)) - 0.1, 1)
                self.cfg["brightness"] = 1.0 if v < 0.4 else v
                save_config(self.cfg)
            elif key == "show_frequency":
                self.cfg["show_frequency"] = not self.cfg.get("show_frequency", True)
                save_config(self.cfg)
            elif key == "wifi":
                self.page = "wifi"
                self._wifi_scan()
            elif key == "update":
                self._update_action()
            elif key == "debug":
                self.page = "debug"

        elif self.page == "wifi":
            if self.wifi_details:
                if y < 100 or y > 700:
                    self.wifi_details = None
                return
            if y < 88:
                self.page = "settings"
                self.wifi_selected = None
                self.wifi_password = ""
                return
            if self.wifi_selected:
                key = self._wifi_key_at(x, y)
                if key:
                    if key == "BACK": self.wifi_password = self.wifi_password[:-1]
                    elif key == "SPACE": self.wifi_password += " "
                    elif key == "ENTER": self._wifi_connect()
                    elif len(self.wifi_password) < 63: self.wifi_password += key
                    return
                if 326 <= y <= 386:
                    self._wifi_connect()
                elif 400 <= y <= 455:
                    self.wifi_selected = None
                    self.wifi_password = ""
                return
            if y >= 690:
                self._wifi_scan()
                return
            top = 150
            rh = 58
            idx = int((y - top) / rh)
            if 0 <= idx < len(self.wifi_networks):
                ssid,sig,sec,active = self.wifi_networks[idx]
                if active:
                    self.wifi_details = self._wifi_details_for_connected(ssid)
                    self.wifi_selected = None
                    return
                self.wifi_selected = ssid
                self.wifi_password = ""
                self.wifi_message = "Enter Wi-Fi password"
                return

        elif self.page == "debug":
            if y < 100 or y > 710:
                self.page = "settings"

    def _wifi_scan(self):
        if self.wifi_scan_busy:
            return
        self.wifi_scan_busy = True
        self.wifi_message = "SCANNING..."
        threading.Thread(target=self._wifi_scan_worker, daemon=True).start()

    def _wifi_scan_worker(self):
        try:
            by_ssid = {}
            scan = subprocess.run(["nmcli","dev","wifi","rescan","ifname","wlan0"], capture_output=True, text=True, timeout=12)
            if scan.returncode == 0:
                # NetworkManager completes scans asynchronously. Merge several cache reads.
                for _ in range(5):
                    time.sleep(1.5)
                    cp = subprocess.run([
                        "nmcli","-t","--escape","yes","-f","IN-USE,SSID,SIGNAL,SECURITY",
                        "dev","wifi","list","--rescan","no","ifname","wlan0"
                    ], capture_output=True, text=True, timeout=12)
                    if cp.returncode != 0:
                        continue
                    for line in cp.stdout.splitlines():
                        parts=_split_nmcli_terse(line,4)
                        if len(parts) < 4: continue
                        active,ssid,signal,sec=parts
                        ssid=ssid.strip()
                        if not ssid: continue
                        try: sig=int(signal)
                        except: sig=0
                        item=(ssid,sig,sec.strip(),active.strip()=="*")
                        old=by_ssid.get(ssid)
                        if old is None or item[3] or sig > old[1]:
                            by_ssid[ssid]=item
            else:
                # A process started outside the active desktop seat can be denied by
                # NetworkManager/polkit. wpa_supplicant exposes its control socket to
                # the netdev group, so use that as a non-privileged scan fallback.
                wp = subprocess.run(["wpa_cli","-i","wlan0","scan"], capture_output=True, text=True, timeout=8)
                if wp.returncode != 0 or "OK" not in wp.stdout:
                    msg=(scan.stderr or scan.stdout or wp.stderr or wp.stdout).strip()
                    raise RuntimeError(msg or "Wi-Fi rescan failed")
                time.sleep(3.0)
                current=""
                st=subprocess.run(["wpa_cli","-i","wlan0","status"], capture_output=True, text=True, timeout=8)
                for line in st.stdout.splitlines():
                    if line.startswith("ssid="):
                        current=line[5:].strip(); break
                cp=subprocess.run(["wpa_cli","-i","wlan0","scan_results"], capture_output=True, text=True, timeout=8)
                if cp.returncode != 0:
                    raise RuntimeError(cp.stderr.strip() or "wpa_cli scan_results failed")
                for line in cp.stdout.splitlines()[1:]:
                    parts=line.split("\t",4)
                    if len(parts) < 5: continue
                    _bssid,_freq,dbm,flags,ssid=parts
                    ssid=ssid.strip()
                    if not ssid: continue
                    try: dbm_i=int(dbm)
                    except: dbm_i=-100
                    sig=max(0,min(100,2*(dbm_i+100)))
                    fu=flags.upper()
                    if "SAE" in fu: sec="WPA3"
                    elif "WPA2" in fu: sec="WPA2"
                    elif "WPA" in fu: sec="WPA"
                    elif "WEP" in fu: sec="WEP"
                    else: sec=""
                    item=(ssid,sig,sec,ssid==current)
                    old=by_ssid.get(ssid)
                    if old is None or item[3] or sig > old[1]:
                        by_ssid[ssid]=item

            nets=list(by_ssid.values())
            nets.sort(key=lambda x:(not x[3],-x[1]))
            self.wifi_networks=nets[:12]
            self.wifi_last_scan=time.time()
            self.wifi_message=f"SCAN DONE - {len(nets)} NETWORKS"
        except Exception:
            self.wifi_message="SCAN ERROR"
        finally:
            self.wifi_scan_busy=False

    def _wifi_details_for_connected(self, ssid):
        d={"ssid":ssid}
        try:
            cp=subprocess.run(["nmcli","-t","-f","GENERAL.CONNECTION,IP4.ADDRESS,IP4.GATEWAY,IP4.DNS","dev","show","wlan0"],capture_output=True,text=True,timeout=8)
            dns=[]
            for line in cp.stdout.splitlines():
                if ":" not in line: continue
                k,v=line.split(":",1)
                if k=="GENERAL.CONNECTION": d["connection"]=v
                elif k=="IP4.ADDRESS[1]": d["ip"]=v
                elif k=="IP4.GATEWAY": d["gateway"]=v
                elif k.startswith("IP4.DNS"): dns.append(v)
            d["dns"]=", ".join(dns)
        except Exception:
            pass
        for n in self.wifi_networks:
            if n[0]==ssid:
                d["signal"]=n[1]; d["security"]=n[2]
                break
        return d

    def _wifi_connect(self):
        if not self.wifi_selected:
            return
        self.wifi_message = "CONNECTING..."
        try:
            cmd = ["nmcli", "dev", "wifi", "connect", self.wifi_selected, "ifname", "wlan0"]
            if self.wifi_password:
                cmd += ["password", self.wifi_password]
            cp = subprocess.run(cmd, capture_output=True, text=True, timeout=25)
            if cp.returncode == 0:
                self.wifi_message = "CONNECTED"
                self.wifi_selected = None
                self.wifi_password = ""
                self._wifi_scan()
            else:
                self.wifi_message = "CONNECT FAILED"
        except Exception:
            self.wifi_message = "CONNECT ERROR"

    def _draw_wifi(self):
        self.ui.fill(BG)
        pygame.draw.rect(self.ui, (7,11,16), (0,0,480,92))
        pygame.draw.circle(self.ui, (18,31,41), (38,45), 24)
        self._text("‹", 38, 43, self.font_xl, BLUE_BRIGHT, center=True)
        self._text("WI-FI SETUP", 82, 24, self.font_l, WHITE)
        self._text(self.wifi_message or self._wifi_text(), 84, 61, self.font_s, DIM)

        if self.wifi_details:
            d=self.wifi_details
            self._text("CONNECTED NETWORK",28,128,self.font_s,DIM)
            self._text(d.get("ssid",""),28,160,self.font_l,GREEN)
            rows=[("IP address",d.get("ip","-")),("Gateway",d.get("gateway","-")),("DNS",d.get("dns","-")),("Signal",f'{d.get("signal",0)}%'),("Security",d.get("security","-"))]
            yy=230
            for label,value in rows:
                pygame.draw.rect(self.ui,(9,13,18),(20,yy,440,62),border_radius=12)
                self._text(label,38,yy+10,self.font_s,DIM)
                self._text(value,38,yy+33,self.font_s,WHITE)
                yy+=72
            self._text("Tap back to return",240,730,self.font_s,DIM,center=True)
            return

        if self.wifi_selected:
            self._text("Network", 28, 132, self.font_s, DIM)
            self._text(self.wifi_selected, 28, 160, self.font_l, WHITE)
            self._text("Password", 28, 225, self.font_s, DIM)
            pygame.draw.rect(self.ui, (10,15,20), (24,254,432,54), border_radius=12)
            masked = "•" * len(self.wifi_password)
            self._text(masked or "type with keyboard...", 42, 270, self.font_m, WHITE if masked else DIM)
            pygame.draw.rect(self.ui, (17,132,212), (24,326,432,60), border_radius=13)
            self._text("CONNECT", 240, 356, self.font_m, WHITE, center=True)
            pygame.draw.rect(self.ui, (20,25,31), (24,400,432,55), border_radius=13)
            self._text("CANCEL", 240, 428, self.font_s, DIM, center=True)
            self._draw_wifi_keyboard()
            return

        self._text("Available networks", 28, 112, self.font_s, DIM)
        top=150; rh=58
        for i,(ssid,sig,sec,active) in enumerate(self.wifi_networks):
            y=top+i*rh
            pygame.draw.rect(self.ui, (9,13,18), (20,y,440,50), border_radius=11)
            col=GREEN if active else WHITE
            self._text(ssid[:24], 38, y+14, self.font_m, col)
            self._text(f"{sig}%", 392, y+15, self.font_s, BLUE_BRIGHT, center=True)
            if sec:
                self._text("LOCK", 440, y+15, self.font_s, DIM, center=True)
        pygame.draw.rect(self.ui, (17,132,212), (20,708,440,54), border_radius=12)
        self._text("SCANNING..." if self.wifi_scan_busy else "RESCAN", 240, 735, self.font_s, WHITE, center=True)

    def _wifi_key_at(self, x, y):
        rows = ["1234567890", "QWERTYUIOP", "ASDFGHJKL", "ZXCVBNM"]
        top = 468
        row_h = 48
        for ri,row in enumerate(rows):
            yy = top + ri*row_h
            if yy <= y < yy+40:
                n=len(row); total=420; kw=total/n; start=30
                if start <= x < start+total:
                    idx=int((x-start)//kw)
                    if 0 <= idx < n: return row[idx]
        if 660 <= y <= 704:
            if 30 <= x <= 132: return "BACK"
            if 146 <= x <= 334: return "SPACE"
            if 348 <= x <= 450: return "ENTER"
        return None

    def _draw_wifi_keyboard(self):
        rows = ["1234567890", "QWERTYUIOP", "ASDFGHJKL", "ZXCVBNM"]
        top=468; row_h=48
        for ri,row in enumerate(rows):
            n=len(row); total=420; kw=total/n; start=30; y=top+ri*row_h
            for i,ch in enumerate(row):
                x=int(start+i*kw)
                w=int(kw-4)
                pygame.draw.rect(self.ui,(18,24,30),(x,y,w,40),border_radius=8)
                self._text(ch,x+w//2,y+20,self.font_s,WHITE,center=True)
        pygame.draw.rect(self.ui,(30,36,44),(30,660,102,44),border_radius=8)
        pygame.draw.rect(self.ui,(30,36,44),(146,660,188,44),border_radius=8)
        pygame.draw.rect(self.ui,(17,132,212),(348,660,102,44),border_radius=8)
        self._text("BACK",81,682,self.font_s,WHITE,center=True)
        self._text("SPACE",240,682,self.font_s,WHITE,center=True)
        self._text("ENTER",399,682,self.font_s,WHITE,center=True)

    def _toggle_mute(self):
        self.cfg["muted"] = not self.cfg.get("muted", False)
        if self.cfg["muted"]:
            self.buzzer.off()
        else:
            self.buzzer.beep_pattern([(70, 0)])
            self.last_beep = time.time() + 0.25
        save_config(self.cfg)

    def _toggle_demo(self):
        enabled = not self.cfg.get("demo_mode", False)
        self.cfg["demo_mode"] = enabled
        self.backend.set_demo(enabled)
        save_config(self.cfg)

    def _wifi_text(self):
        try:
            state = Path("/sys/class/net/wlan0/operstate").read_text().strip()
            return "CONNECTED" if state == "up" else "OFFLINE"
        except Exception:
            return "UNKNOWN"

    def _update_action(self):
        # RESTART used to be display text only: tapping it fell through to a
        # fresh update check. Keep it as a real fallback action as well.
        if self.update_message == "RESTART":
            self._request_app_restart(0.15)
            return
        if self.update_busy:
            return
        if self.update_manifest and self.update_message == "INSTALL":
            self.update_busy = True
            self.update_message = "INSTALLING"
            threading.Thread(target=self._install_update_worker, daemon=True).start()
        else:
            self.update_busy = True
            self.update_message = "CHECKING"
            threading.Thread(target=self._check_update_worker, daemon=True).start()

    def _check_update_worker(self):
        try:
            m = fetch_manifest(self.cfg.get("update_manifest_url", ""))
            self.update_manifest = m
            if version_tuple(m.get("version", "0")) > version_tuple(self.cfg.get("app_version", "0")):
                self.update_message = "INSTALL"
            else:
                self.update_message = "UP TO DATE"
        except Exception:
            self.update_message = "NOT SET" if not self.cfg.get("update_manifest_url") else "ERROR"
        self.update_busy = False

    def _request_app_restart(self, delay=0.0):
        # The appliance service has Restart=always. Ending the normal app loop
        # is therefore enough to perform a reliable restart without sudo or a
        # systemctl subprocess. Normal cleanup closes the SDR, buzzer and pygame
        # before systemd starts the freshly installed files.
        self.update_message = "RESTARTING"
        self.update_busy = True
        if delay > 0:
            time.sleep(float(delay))
        self.running = False

    def _install_update_worker(self):
        try:
            m = self.update_manifest or {}
            data = download_update(m.get("url", ""), m.get("sha256", ""))
            install_zip_bytes(data)
            # Do not wait for another tap. The updater code currently in memory
            # requests a graceful exit after every successful install; systemd
            # then relaunches RF Eye from the newly copied version.
            self._request_app_restart(0.75)
            return
        except Exception:
            self.update_message = "ERROR"
        self.update_busy = False

    def _sound_logic(self, snap):
        if self.cfg.get("muted", False):
            self.buzzer.off()
            return

        # TMB12A03 is an active buzzer with one fixed pitch, so the startup
        # "jingle" is a distinct short-short-long rhythm. It is played exactly
        # once, only after a real SDR scan has reached LIVE state.
        if not self.ready_chime_done and snap.get("status") == "LIVE":
            self.ready_chime_done = True
            if self.cfg.get("startup_chime", True):
                self.buzzer.beep_pattern([(70, 55), (70, 60), (175, 0)])
                self.last_beep = time.time() + 0.25
            return

        peaks = snap["peaks"]
        if not peaks:
            return

        lv = max(float(p.get("level", 0.0)) for p in peaks)
        if lv < 0.15:
            return

        # TMB12A03 has one fixed internal tone, so make the LOW/MEDIUM/HIGH
        # zones deliberately different by rhythm rather than tiny pitch changes.
        if lv > 0.72:
            on_ms = int(self.cfg.get("buzzer_red_ms", 105))
            gap_ms = int(self.cfg.get("buzzer_red_gap_ms", 45))
            pattern = [(on_ms, gap_ms), (on_ms, gap_ms), (on_ms, 0)]
            base_interval = 0.46
        elif lv > 0.43:
            on_ms = int(self.cfg.get("buzzer_yellow_ms", 130))
            gap_ms = int(self.cfg.get("buzzer_yellow_gap_ms", 135))
            pattern = [(on_ms, gap_ms), (on_ms, 0)]
            base_interval = 0.95
        else:
            on_ms = int(self.cfg.get("buzzer_green_ms", 185))
            pattern = [(on_ms, 0)]
            base_interval = 1.85

        if self.cfg.get("audio_mode", "adaptive") == "adaptive":
            if lv > 0.72:
                zone_n = max(0.0, min(1.0, (lv - 0.72) / 0.28))
                interval = max(0.40, base_interval - 0.06 * zone_n)
            elif lv > 0.43:
                zone_n = max(0.0, min(1.0, (lv - 0.43) / 0.29))
                interval = max(0.82, base_interval - 0.13 * zone_n)
            else:
                zone_n = max(0.0, min(1.0, (lv - 0.15) / 0.28))
                interval = max(1.45, base_interval - 0.40 * zone_n)
        else:
            interval = base_interval

        pattern_ms = sum(on + gap for on, gap in pattern)
        interval = max(interval, pattern_ms / 1000.0 + 0.08)

        now = time.time()
        if now - self.last_beep >= interval:
            self.buzzer.beep_pattern(pattern)
            self.last_beep = now

    def _text(self, txt, x, y, font, color, center=False, right=False):
        s = font.render(str(txt), True, color)
        r = s.get_rect()
        if center:
            r.center = (int(x), int(y))
        elif right:
            r.topright = (int(x), int(y))
        else:
            r.topleft = (int(x), int(y))
        self.ui.blit(s, r)

    def _eye(self, cx, cy, r=28):
        pygame.draw.circle(self.ui, BLUE, (cx, cy), r, 7)
        pygame.draw.circle(self.ui, BG, (cx+4, cy+3), int(r*0.34))
        pygame.draw.circle(self.ui, BLUE_BRIGHT, (cx-8, cy-8), 5)

    def _draw_settings_icon(self, cx, cy):
        if self.settings_icon is not None:
            r = self.settings_icon.get_rect(center=(cx, cy))
            self.ui.blit(self.settings_icon, r)
        else:
            self._gear(cx, cy, 42)

    def _gear(self, cx, cy, size=42):
        import math
        # Supersampled vector gear for clean edges on the small SPI panel.
        scale = 4
        side = max(48, int(size * scale))
        icon = pygame.Surface((side, side), pygame.SRCALPHA)
        cc = side // 2
        teeth = 10
        outer = size * 0.48 * scale
        root = size * 0.34 * scale
        pts = []
        for i in range(teeth * 4):
            angle = -math.pi / 2 + i * math.pi / (teeth * 2)
            radius = outer if i % 4 in (1, 2) else root
            pts.append((cc + int(math.cos(angle) * radius), cc + int(math.sin(angle) * radius)))
        pygame.draw.polygon(icon, BLUE_BRIGHT, pts)
        pygame.draw.circle(icon, BLUE_BRIGHT, (cc, cc), int(size * 0.30 * scale))
        pygame.draw.circle(icon, (0, 0, 0, 0), (cc, cc), int(size * 0.115 * scale))
        icon = pygame.transform.smoothscale(icon, (int(size), int(size)))
        self.ui.blit(icon, icon.get_rect(center=(int(cx), int(cy))))

    def _speaker(self, cx, cy, muted):
        pygame.draw.polygon(
            self.ui, BLUE,
            [(cx-24, cy-11), (cx-11, cy-11), (cx+6, cy-24), (cx+6, cy+24), (cx-11, cy+11), (cx-24, cy+11)]
        )
        pygame.draw.arc(self.ui, BLUE, (cx-1, cy-23, 40, 46), -0.75, 0.75, 4)
        pygame.draw.arc(self.ui, BLUE, (cx+8, cy-33, 56, 66), -0.75, 0.75, 4)
        if muted:
            pygame.draw.line(self.ui, WHITE, (cx-31, cy-30), (cx+40, cy+30), 6)

    def _level_color(self, idx, n):
        f = idx / max(1, n - 1)
        if f < 0.45:
            return GREEN
        if f < 0.68:
            return YELLOW
        if f < 0.84:
            return ORANGE
        return RED

    def _draw_main(self, snap):
        self.ui.fill(BG)

        if self.cfg.get("show_brand_text", True):
            self._text("RF EYE", 240, 42, self.font_l, BLUE_BRIGHT, center=True)

        status = snap["status"]
        status_col = GREEN if status == "LIVE" else BLUE if status == "DEMO" else RED
        pygame.draw.circle(self.ui, status_col, (432, 44), 7)

        # The C2000 network state matters more than the USB state. Without a
        # verified base station nearby the detector cannot report anything,
        # and the user must see that rather than read silence as "all clear".
        if status == "DEMO":
            sdr_text, sdr_col = "SDR: DEMO MODE", BLUE_BRIGHT
        elif status != "LIVE":
            # A sagging 5 V rail and a broken dongle look identical on screen
            # but need completely different fixes, so say which one it is.
            if snap.get("power_warning"):
                sdr_text, sdr_col = "SDR LOST - USB POWER LOW", RED
            else:
                sdr_text, sdr_col = "SDR: NOT CONNECTED", RED
        elif snap.get("detector_state") == "ALERT":
            sdr_text, sdr_col = "C2000 ACTIVITY NEARBY", RED
        elif snap.get("network_locked"):
            n = int(snap.get("site_locked_count", 0) or 0)
            sdr_text, sdr_col = f"C2000 NETWORK LOCKED ({n})", GREEN
        else:
            sdr_text, sdr_col = "SEARCHING FOR C2000 NETWORK", YELLOW
        self._text(sdr_text, 240, 88, self.font_s, sdr_col, center=True)

        # Settings button in the physical top-right corner after rotation.
        self._draw_settings_icon(38, 38)

        peaks = snap["peaks"][:3]
        while len(peaks) < 3:
            peaks.append({"level": 0.0, "freq_hz": 0.0})

        x_positions = [75, 200, 325]
        seg_w = 100
        seg_h = 32
        gap = 9
        nseg = 10
        top = 200

        for col, p in enumerate(peaks):
            lv = clamp(float(p.get("level", 0.0)))
            active = int(round(lv * nseg))
            x = x_positions[col]
            for i in range(nseg):
                y = top + (nseg - 1 - i) * (seg_h + gap)
                color = self._level_color(i, nseg) if i < active else SEG_OFF
                pygame.draw.rect(self.ui, color, (x, y, seg_w, seg_h), border_radius=3)

            if self.cfg.get("show_frequency", True):
                if p.get("freq_hz", 0):
                    ftxt = f'{p["freq_hz"] / 1e6:.3f} MHz'
                    fcol = (132, 184, 210)
                else:
                    ftxt = ['381.000 MHz','382.500 MHz','384.000 MHz'][col]
                    fcol = (74, 96, 110)
                self._text(ftxt, x + seg_w//2, 617, self.font_s, fcol, center=True)

        max_lv = float(snap.get("mobile_level", 0.0))
        if status not in ("LIVE", "DEMO"):
            state, col = (("POWER LOW", RED) if snap.get("power_warning")
                          else ("NOT CONNECTED", RED))
        elif (status == "LIVE" and not snap.get("network_locked")
              and max_lv <= 0.15):
            state, col = "NO NETWORK", BLUE_BRIGHT
        elif max_lv > 0.72:
            state, col = "HIGH", RED
        elif max_lv > 0.43:
            state, col = "MEDIUM", YELLOW
        elif max_lv > 0.15:
            state, col = "LOW", GREEN
        else:
            state, col = "CLEAR", DIM

        pygame.draw.rect(self.ui, PANEL, (0, 640, 480, 160))
        self._speaker(72, 708, self.cfg.get("muted", False))
        self._text("MUTE" if self.cfg.get("muted") else "SOUND", 72, 762, self.font_s, RED if self.cfg.get("muted") else BLUE_BRIGHT, center=True)

        pygame.draw.rect(self.ui, (24, 26, 31), (168, 680, 144, 58), border_radius=12)
        self._text("SPECTRUM", 240, 709, self.font_s, WHITE, center=True)
        self._text(f'Noise {snap["noise"]:.0f} dB', 240, 763, self.font_s, DIM, center=True)

        state_font = self.font_s if state == "NOT CONNECTED" else self.font_m
        self._text(state, 398, 708, state_font, col, center=True)
        self._text("STATUS", 398, 762, self.font_s, DIM, center=True)


    def _draw_settings(self):
        self.ui.fill(BG)

        # Header
        pygame.draw.rect(self.ui, (7, 11, 16), (0, 0, 480, 92))
        pygame.draw.circle(self.ui, (18, 31, 41), (38, 45), 24)
        self._text("‹", 38, 43, self.font_xl, BLUE_BRIGHT, center=True)
        self._text("SETTINGS", 82, 24, self.font_l, WHITE)
        self._text(f'RF EYE  v{self.cfg.get("app_version", "")}', 84, 61, self.font_s, DIM)

        rows = [
            ("Sound", "MUTED" if self.cfg.get("muted") else "ON", "toggle"),
            ("Demo mode", "ON" if self.cfg.get("demo_mode") else "OFF", "toggle"),
            ("Audio mode", self.cfg.get("audio_mode", "adaptive").upper(), "value"),
            ("Brightness", f'{int(self.cfg.get("brightness", 1.0) * 100)}%', "value"),
            ("Frequency labels", "ON" if self.cfg.get("show_frequency") else "OFF", "toggle"),
            ("Wi-Fi", self._wifi_text(), "status"),
            ("Software update", self.update_message, "action"),
            ("Debug", "OPEN", "action"),
        ]

        top = 104
        rh = 58
        for i, (label, value, kind) in enumerate(rows):
            y = top + i * rh
            pygame.draw.rect(self.ui, (9, 13, 18), (20, y, 440, 50), border_radius=13)
            pygame.draw.line(self.ui, (19, 28, 36), (34, y + 49), (446, y + 49), 1)
            self._text(label, 40, y + 14, self.font_m, WHITE)

            if kind == "toggle":
                enabled = value == "ON"
                pill = pygame.Rect(362, y + 10, 72, 30)
                pygame.draw.rect(self.ui, BLUE if enabled else (38, 43, 49), pill, border_radius=15)
                knob_x = 419 if enabled else 377
                pygame.draw.circle(self.ui, WHITE, (knob_x, y + 25), 11)
            elif kind == "status":
                col = GREEN if value == "CONNECTED" else RED
                pygame.draw.circle(self.ui, col, (352, y + 25), 6)
                self._text(value, 405, y + 25, self.font_s, col, center=True)
            else:
                col = BLUE_BRIGHT if kind == "action" else (150, 201, 226)
                self._text(value, 410, y + 25, self.font_s, col, center=True)

        self._text("Click a row to change", 240, 728, self.font_s, DIM, center=True)
        self._text("ESC or ‹ to return", 240, 758, self.font_s, DIM, center=True)

    def _draw_debug(self, snap):
        self.ui.fill(BG)
        pygame.draw.rect(self.ui, (7, 11, 16), (0, 0, 480, 92))
        pygame.draw.circle(self.ui, (18, 31, 41), (38, 45), 24)
        self._text("‹", 38, 43, self.font_xl, BLUE_BRIGHT, center=True)
        self._text("DEBUG", 82, 24, self.font_l, WHITE)
        self._text("LIVE PERFORMANCE", 84, 61, self.font_s, DIM)

        age_ms = max(0.0, (time.time() - float(snap.get("last_update", 0.0))) * 1000.0) if snap.get("last_update") else 0.0
        frame_ms = max(0.001, float(self.debug_frame_ms))
        actual_fps = 1000.0 / frame_ms
        phy = list(snap.get("phy") or [])
        best = max(phy, key=lambda q: float(q.get("dqpsk_m", 0)), default=None)
        rows = [
            ("UI refresh", f"{frame_ms:5.1f} ms  {actual_fps:4.1f} FPS"),
            ("Detector state", str(snap.get('detector_state','?'))),
            ("Sites locked", f"{int(snap.get('site_locked_count',0))}"
                             f"  (cand {int(snap.get('site_candidate_count',0))})"),
            ("Watching", f"{len(snap.get('watch_freqs') or [])} ch"
                         f" {str(snap.get('dwell_role','')).lower()}"),
            ("Best DQPSK @18k", (f"{float(best.get('dqpsk_m',0)):.3f}"
                                 f"  x{float(best.get('dqpsk_selectivity',0)):.1f}")
                                if best else "-"),
            ("Last verdict", (str(best.get('reason','?'))[:22]) if best else "-"),
            ("Full cycle", f"{float(snap.get('cycle_ms',0)):7.0f} ms"),
            ("Dwell capture", f"{float(snap.get('dwell_ms',0)):7.0f} ms"),
            ("Verification", f"{float(snap.get('verify_ms',0)):7.0f} ms"),
            ("Supply", str(snap.get('power_warning')
                            or snap.get('power_history') or 'OK')),
            ("Supply detail", str(snap.get('power_detail') or '-')[:24]),
            ("Best channel", _search_best_text(snap)),
            ("Backend", f"{snap.get('status','?')}  age {age_ms:.0f} ms"),
        ]
        y=112
        for label,value in rows:
            pygame.draw.rect(self.ui,(9,13,18),(20,y,440,54),border_radius=11)
            self._text(label,38,y+16,self.font_s,DIM)
            col = GREEN if label == "Backend" and value == "LIVE" else BLUE_BRIGHT
            self._text(value,440,y+27,self.font_s,col,right=True)
            y += 61

        err=str(snap.get('error','')).strip()
        if err:
            self._text("ERR " + err[-46:], 240, 681, self.font_s, RED, center=True)
        self._text("tap top or bottom to return", 240, 758, self.font_s, DIM, center=True)

    def _apply_brightness(self):
        """Dim the picture. Note: the picture, not the backlight.

        This panel exposes no backlight device -- /sys/class/backlight is
        empty and the overlay's led-gpios line is left as an unclaimed input,
        so the lamp is on whenever the unit is -- and a darker screen
        therefore saves no power at all. It only has to be cheap. Rebuilding
        a 320x480 SRCALPHA surface every frame was not: the surface is now
        made once per brightness setting and kept.
        """
        br = clamp(float(self.cfg.get("brightness", 1.0)), 0.35, 1.0)
        if br >= 0.999:
            return
        alpha = int((1.0 - br) * 220)
        overlay = getattr(self, "_dim_overlay", None)
        if overlay is None or self._dim_alpha != alpha or                 overlay.get_size() != (self.uw, self.uh):
            overlay = pygame.Surface((self.uw, self.uh), pygame.SRCALPHA)
            overlay.fill((0, 0, 0, alpha))
            self._dim_overlay = overlay
            self._dim_alpha = alpha
        self.ui.blit(overlay, (0, 0))

def _search_best_text(snap):
    """One line saying what the current band pass has actually found."""
    best = snap.get("search_best") or {}
    if not best:
        return "-"
    verdict = "TETRA" if best.get("ok") else str(best.get("fail") or "?")
    return "%.4f %+.0fdB %s" % (float(best.get("freq_hz", 0.0)) / 1e6,
                                float(best.get("snr_db", 0.0)), verdict)


def _build_app(cfg, fullscreen, attempts=20, delay=1.0):
    """Construct the App, retrying while the display is still coming up.

    Opening the pygame display is the first thing __init__ does, and at boot
    it can fail for a second or two while the compositor finishes bringing the
    panel up. That failure used to end the process, and with Restart=always
    every exit spent one of systemd's five permitted starts -- five inside ten
    seconds and the unit was left failed for good. Retrying here keeps those
    failures inside one process, where they cost nothing and get written down.
    """
    last = None
    for attempt in range(1, int(attempts) + 1):
        try:
            return App(cfg, fullscreen=fullscreen)
        except Exception as exc:
            last = exc
            boot_note("startup attempt %d failed: %s: %s"
                      % (attempt, type(exc).__name__, exc))
            try:
                pygame.display.quit()
            except Exception:
                pass
            time.sleep(float(delay))
    boot_note("giving up after %d startup attempts: %s" % (attempts, last))
    raise SystemExit(1)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--window", action="store_true")
    args = ap.parse_args()

    cfg = load_config()
    boot_note("start v%s" % cfg.get("app_version", "?"))
    # Persist normalized migrations/version fields once; save_config is a
    # no-op when the file is already byte-identical.
    save_config(cfg)
    fullscreen = bool(cfg.get("fullscreen", True)) and not args.window
    _build_app(cfg, fullscreen).run()

if __name__ == "__main__":
    main()
