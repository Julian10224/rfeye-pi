#!/usr/bin/env python3
from pathlib import Path
import sys

if len(sys.argv) != 3:
    raise SystemExit('usage: patch-debug-menu.py /path/to/app.py /path/to/sdr_backend.py')

app = Path(sys.argv[1])
backend = Path(sys.argv[2])

# ---------------- backend timing instrumentation ----------------
s = backend.read_text()

init_needle = "        self._demo_forced=bool(cfg.get('demo_mode',False))\n"
if 'self.last_cycle_ms=' not in s:
    if init_needle not in s:
        raise SystemExit('backend init patch point missing')
    s = s.replace(init_needle, init_needle +
        "        self.last_cycle_ms=0.; self.last_mobile_scan_ms=0.; self.last_site_scan_ms=0.; self.last_capture_ms=0.; self.last_scan_windows=0\n", 1)

snap_old = "            'last_update':float(self.last_update),'demo':bool(self.demo_active)}\n"
snap_new = "            'last_update':float(self.last_update),'demo':bool(self.demo_active),\n            'cycle_ms':float(self.last_cycle_ms),'mobile_scan_ms':float(self.last_mobile_scan_ms),\n            'site_scan_ms':float(self.last_site_scan_ms),'capture_ms':float(self.last_capture_ms),\n            'scan_windows':int(self.last_scan_windows),\n            'sdr_path':('DIRECT' if globals().get('RtlSdr') is not None else 'RTL_SDR CLI')}\n"
if snap_old in s:
    s = s.replace(snap_old, snap_new, 1)
elif "'cycle_ms':float(self.last_cycle_ms)" not in s:
    raise SystemExit('backend snapshot patch point missing')

cap_old = "        for center in self._centers_for(a,b,sr):\n            f,p,stack=self._capture(center,sr,n,blocks,label=='MOBILE'); mask=(f>=a)&(f<=b)&(np.abs(f-center)<=usable)\n"
cap_new = "        for center in self._centers_for(a,b,sr):\n            _cap_t=time.perf_counter()\n            f,p,stack=self._capture(center,sr,n,blocks,label=='MOBILE')\n            self.last_capture_ms=(time.perf_counter()-_cap_t)*1000.; self.last_scan_windows+=1\n            mask=(f>=a)&(f<=b)&(np.abs(f-center)<=usable)\n"
if cap_old in s:
    s = s.replace(cap_old, cap_new, 1)
elif 'self.last_capture_ms=(time.perf_counter()-_cap_t)*1000.' not in s:
    raise SystemExit('backend capture timing patch point missing')

cycle_start = "    def _scan_cycle(self):\n        try:\n"
cycle_new = "    def _scan_cycle(self):\n        _cycle_t=time.perf_counter(); self.last_scan_windows=0; self.last_site_scan_ms=0.\n        try:\n"
if cycle_start in s:
    s = s.replace(cycle_start, cycle_new, 1)
elif '_cycle_t=time.perf_counter(); self.last_scan_windows=0' not in s:
    raise SystemExit('backend cycle timing patch point missing')

mobile_old = "            m,mf,mp,mfloor=self._scan_band(float(self.cfg.get('mobile_band_start_hz',380e6)),float(self.cfg.get('mobile_band_end_hz',385e6)),'MOBILE')\n"
mobile_new = "            _mobile_t=time.perf_counter()\n            m,mf,mp,mfloor=self._scan_band(float(self.cfg.get('mobile_band_start_hz',380e6)),float(self.cfg.get('mobile_band_end_hz',385e6)),'MOBILE')\n            self.last_mobile_scan_ms=(time.perf_counter()-_mobile_t)*1000.\n"
if mobile_old in s:
    s = s.replace(mobile_old, mobile_new, 1)
elif 'self.last_mobile_scan_ms=(time.perf_counter()-_mobile_t)*1000.' not in s:
    raise SystemExit('backend mobile timing patch point missing')

site_old = "                site,_,_,_=self._scan_band(float(self.cfg.get('site_band_start_hz',390e6)),float(self.cfg.get('site_band_end_hz',395e6)),'SITE')\n"
site_new = "                _site_t=time.perf_counter()\n                site,_,_,_=self._scan_band(float(self.cfg.get('site_band_start_hz',390e6)),float(self.cfg.get('site_band_end_hz',395e6)),'SITE')\n                self.last_site_scan_ms=(time.perf_counter()-_site_t)*1000.\n"
if site_old in s:
    s = s.replace(site_old, site_new, 1)
elif 'self.last_site_scan_ms=(time.perf_counter()-_site_t)*1000.' not in s:
    raise SystemExit('backend site timing patch point missing')

success_old = "                self.noise_floor_db=mfloor;self.last_update=now;self.status='LIVE';self.error='';self.demo_active=False;self.last_good_scan=now;self.scan_failures=0\n"
success_new = "                self.noise_floor_db=mfloor;self.last_update=now;self.status='LIVE';self.error='';self.demo_active=False;self.last_good_scan=now;self.scan_failures=0\n                self.last_cycle_ms=(time.perf_counter()-_cycle_t)*1000.\n"
if success_old in s:
    s = s.replace(success_old, success_new, 1)
elif 'self.last_cycle_ms=(time.perf_counter()-_cycle_t)*1000.' not in s:
    raise SystemExit('backend cycle completion patch point missing')

compile(s, str(backend), 'exec')
backend.write_text(s)

# ---------------- UI debug page ----------------
s = app.read_text()

if 'self.debug_frame_ms' not in s:
    needle = '        self.last_beep = 0.0\n'
    if needle not in s:
        raise SystemExit('app debug init patch point missing')
    s = s.replace(needle, needle + '        self.debug_frame_ms = 0.0\n        self.debug_last_frame = time.perf_counter()\n', 1)

run_needle = '        while self.running:\n            self._events()\n'
if 'self.debug_frame_ms = (now_frame - self.debug_last_frame)' not in s:
    if run_needle not in s:
        raise SystemExit('app run timing patch point missing')
    s = s.replace(run_needle,
        '        while self.running:\n            now_frame = time.perf_counter()\n            self.debug_frame_ms = (now_frame - self.debug_last_frame) * 1000.0\n            self.debug_last_frame = now_frame\n            self._events()\n', 1)

route_old = '''            if self.page == "main":
                self._draw_main(snap)
            elif self.page == "settings":
                self._draw_settings()
            elif self.page == "wifi":
                self._draw_wifi()
            else:
                self._draw_spectrum(snap)
'''
route_new = '''            if self.page == "main":
                self._draw_main(snap)
            elif self.page == "settings":
                self._draw_settings()
            elif self.page == "wifi":
                self._draw_wifi()
            elif self.page == "debug":
                self._draw_debug(snap)
            else:
                self._draw_spectrum(snap)
'''
if route_old in s:
    s = s.replace(route_old, route_new, 1)
elif 'elif self.page == "debug":' not in s:
    raise SystemExit('app route patch point missing')

keys_old = '''                "wifi",
                "update",
                "spectrum",
            ]
'''
keys_new = '''                "wifi",
                "update",
                "spectrum",
                "debug",
            ]
'''
if keys_old in s:
    s = s.replace(keys_old, keys_new, 1)
elif '"debug",' not in s:
    raise SystemExit('settings key patch point missing')

act_old = '''            elif key == "spectrum":
                self.page = "spectrum"
'''
act_new = '''            elif key == "spectrum":
                self.page = "spectrum"
            elif key == "debug":
                self.page = "debug"
'''
if act_old in s:
    s = s.replace(act_old, act_new, 1)
elif 'elif key == "debug":' not in s:
    raise SystemExit('debug action patch point missing')

spec_tap = '''        elif self.page == "spectrum":
            if y < 90 or y > 725:
                self.page = "main"
'''
debug_tap = '''        elif self.page == "spectrum":
            if y < 90 or y > 725:
                self.page = "main"

        elif self.page == "debug":
            if y < 100 or y > 710:
                self.page = "settings"
'''
if spec_tap in s:
    s = s.replace(spec_tap, debug_tap, 1)
elif 'elif self.page == "debug":' not in s[s.find('def _tap'):s.find('def _wifi_scan')]:
    raise SystemExit('debug tap patch point missing')

rows_old = '''            ("Software update", self.update_message, "action"),
            ("Spectrum", "OPEN", "action"),
        ]

        top = 116
        rh = 66
'''
rows_new = '''            ("Software update", self.update_message, "action"),
            ("Spectrum", "OPEN", "action"),
            ("Debug", "OPEN", "action"),
        ]

        top = 104
        rh = 58
'''
if rows_old in s:
    s = s.replace(rows_old, rows_new, 1)
elif '("Debug", "OPEN", "action")' not in s:
    raise SystemExit('settings rows patch point missing')

# Compact settings row geometry for ten rows.
s = s.replace('(20, y, 440, 56)', '(20, y, 440, 50)')
s = s.replace('(34, y + 55), (446, y + 55)', '(34, y + 49), (446, y + 49)')
s = s.replace('y + 17, self.font_m', 'y + 14, self.font_m')
s = s.replace('y + 13, 72, 30', 'y + 10, 72, 30')
s = s.replace('(knob_x, y + 28)', '(knob_x, y + 25)')
s = s.replace('(352, y + 28)', '(352, y + 25)')
s = s.replace('405, y + 28, self.font_s', '405, y + 25, self.font_s')
s = s.replace('410, y + 28, self.font_s', '410, y + 25, self.font_s')

method_anchor = '    def _draw_spectrum(self, snap):\n'
if '    def _draw_debug(self, snap):\n' not in s:
    if method_anchor not in s:
        raise SystemExit('debug method insertion point missing')
    method = '''    def _draw_debug(self, snap):
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

'''
    s = s.replace(method_anchor, method + method_anchor, 1)

compile(s, str(app), 'exec')
app.write_text(s)
print('RF Eye debug timing/menu patch installed')
