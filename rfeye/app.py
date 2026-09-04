#!/usr/bin/env python3
import argparse
import os
import time
import threading
import subprocess
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

def clamp(v, lo=0.0, hi=1.0):
    return max(lo, min(hi, v))

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
        self.last_beep = 0.0
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

    def run(self):
        fps = int(self.cfg.get("ui_fps", 20))
        clock = pygame.time.Clock()

        while self.running:
            now_frame = time.perf_counter()
            self.debug_frame_ms = (now_frame - self.debug_last_frame) * 1000.0
            self.debug_last_frame = now_frame
            self._events()
            if self.last_mouse_motion and time.time() - self.last_mouse_motion > self.mouse_hide_delay:
                pygame.mouse.set_visible(False)
                self.last_mouse_motion = 0.0
            snap = self.backend.snapshot()
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
            elif self.page == "recordings":
                self._draw_recordings()
            elif self.page == "recording_detail":
                self._draw_recording_detail()
            elif self.page == "recording_delete_confirm":
                self._draw_recording_delete_confirm()
            elif self.page == "recording_replay":
                self._draw_recording_replay()
            else:
                self._draw_spectrum(snap)

            self._apply_brightness()
            self._present_rotated()
            pygame.display.flip()
            clock.tick(fps)

        self.backend.stop()
        self.buzzer.close()
        pygame.quit()

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
                elif e.key == pygame.K_t:
                    self.page = "spectrum"
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

    def _tap(self, x, y):
        if self.page == "main":
            if x <= 82 and y <= 82:
                self.page = "settings"
                return
            if x < 120 and y > 655:
                self._toggle_mute()
                return
            if 160 <= x <= 320 and y > 675:
                self.page = "spectrum"
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
                "spectrum",
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
            elif key == "spectrum":
                self.page = "spectrum"
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

        elif self.page == "spectrum":
            if y < 90 or y > 725:
                self.page = "main"

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
                        "nmcli","-t","--escape","no","-f","IN-USE,SSID,SIGNAL,SECURITY",
                        "dev","wifi","list","--rescan","no","ifname","wlan0"
                    ], capture_output=True, text=True, timeout=12)
                    if cp.returncode != 0:
                        continue
                    for line in cp.stdout.splitlines():
                        parts=line.split(":",3)
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

        # Clear RTL-SDR hardware/status indication on the main screen.
        if status == "LIVE":
            sdr_text = "SDR: CONNECTED"
            sdr_col = GREEN
        elif status == "DEMO":
            sdr_text = "SDR: DEMO MODE"
            sdr_col = BLUE_BRIGHT
        else:
            sdr_text = "SDR: NOT CONNECTED"
            sdr_col = RED
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
            state, col = "NOT CONNECTED", RED
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
            ("Spectrum", "OPEN", "action"),
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
        rows = [
            ("UI refresh", f"{frame_ms:5.1f} ms  {actual_fps:4.1f} FPS"),
            ("Data age", f"{age_ms:7.0f} ms"),
            ("Full cycle", f"{float(snap.get('cycle_ms',0)):7.0f} ms"),
            ("Mobile sweep", f"{float(snap.get('mobile_scan_ms',0)):7.0f} ms"),
            ("Site sweep", f"{float(snap.get('site_scan_ms',0)):7.0f} ms"),
            ("Last capture", f"{float(snap.get('capture_ms',0)):7.0f} ms"),
            ("Tune windows", str(int(snap.get('scan_windows',0)))),
            ("SDR path", str(snap.get('sdr_path','?'))),
            ("Backend", str(snap.get('status','?'))),
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

    def _draw_spectrum(self, snap):
        self.ui.fill(BG)
        self._text("<", 28, 30, self.font_xl, BLUE)
        self._text("SPECTRUM", 240, 45, self.font_l, BLUE_BRIGHT, center=True)

        plot = pygame.Rect(26, 115, 428, 320)
        pygame.draw.rect(self.ui, (7, 8, 10), plot)
        pygame.draw.rect(self.ui, (45, 48, 54), plot, 1)

        p = snap["spectrum"]
        if len(p) > 2:
            pmin = snap["noise"] - 12
            pmax = max(max(float(v) for v in p), pmin + 45)
            pts = []
            for i, v in enumerate(p):
                x = plot.left + int(i * (plot.width - 1) / (len(p) - 1))
                norm = clamp((float(v) - pmin) / (pmax - pmin))
                y = plot.bottom - int(norm * (plot.height - 1))
                pts.append((x, y))
            if len(pts) > 1:
                pygame.draw.lines(self.ui, BLUE_BRIGHT, False, pts, 2)

        self._text(f'{self.cfg.get("mobile_band_start_hz", self.cfg["scan_start_hz"]) / 1e6:.3f} MHz', 30, 446, self.font_s, DIM)
        self._text(f'{self.cfg.get("mobile_band_end_hz", self.cfg["scan_end_hz"]) / 1e6:.3f} MHz', 304, 446, self.font_s, DIM)
        self._text(f'Noise floor {snap["noise"]:.1f} dB', 240, 495, self.font_m, WHITE, center=True)

        if snap["peaks"]:
            y = 555
            for i, peak in enumerate(snap["peaks"][:3]):
                self._text(f'{i+1}.', 38, y + i * 54, self.font_m, BLUE_BRIGHT)
                self._text(f'{peak["freq_hz"] / 1e6:.5f} MHz', 80, y + i * 54, self.font_m, WHITE)
                self._text(f'{int(peak["level"] * 100):3d}%', 392, y + i * 54, self.font_m, DIM, center=True)
        else:
            self._text("No transient RF activity", 240, 610, self.font_m, DIM, center=True)

        self._text("tap top or bottom to return", 240, 770, self.font_s, DIM, center=True)

    def _apply_brightness(self):
        br = clamp(float(self.cfg.get("brightness", 1.0)), 0.35, 1.0)
        if br < 0.999:
            alpha = int((1.0 - br) * 220)
            overlay = pygame.Surface((self.uw, self.uh), pygame.SRCALPHA)
            overlay.fill((0, 0, 0, alpha))
            self.ui.blit(overlay, (0, 0))

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--window", action="store_true")
    args = ap.parse_args()

    cfg = load_config()
    App(cfg, fullscreen=not args.window).run()

if __name__ == "__main__":
    main()
