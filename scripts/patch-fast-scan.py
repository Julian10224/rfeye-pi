#!/usr/bin/env python3
from pathlib import Path
import sys

if len(sys.argv) != 2:
    raise SystemExit('usage: patch-fast-scan.py /path/to/sdr_backend.py')

p = Path(sys.argv[1])
s = p.read_text()

old = "    def set_demo(self,v):\n        with self.lock: self._demo_forced=bool(v); self.cfg['demo_mode']=bool(v)\n"
new = """    def set_demo(self,v):
        v=bool(v)
        with self.lock:
            self._demo_forced=v; self.cfg['demo_mode']=v
            if not v:
                self.demo_active=False; self.peaks=[]; self.mobile_peaks=[]; self.site_peaks=[]
                self.mobile_level=0.; self.site_level=0.; self.activity_confidence=0.; self.mobile_confirmed=False
                self.status='SCANNING'; self.error=''; self.last_update=time.time()
"""
if old in s:
    s=s.replace(old,new,1)
elif "self.status='SCANNING'" not in s:
    raise SystemExit('set_demo patch point missing')

init_needle = "        self._demo_forced=bool(cfg.get('demo_mode',False))\n"
if init_needle in s and 'self._idle_site_cycle' not in s:
    s=s.replace(init_needle, init_needle + "        self._idle_site_cycle=0\n", 1)

old_cycle = "            m,mf,mp,mfloor=self._scan_band(float(self.cfg.get('mobile_band_start_hz',380e6)),float(self.cfg.get('mobile_band_end_hz',385e6)),'MOBILE')\n            site,_,_,_=self._scan_band(float(self.cfg.get('site_band_start_hz',390e6)),float(self.cfg.get('site_band_end_hz',395e6)),'SITE'); now=time.time(); self._remember_sites(site,now)\n"
new_cycle = """            m,mf,mp,mfloor=self._scan_band(float(self.cfg.get('mobile_band_start_hz',380e6)),float(self.cfg.get('mobile_band_end_hz',385e6)),'MOBILE')
            # Keep the high-priority mobile band on the fastest revisit cadence.
            # Preserve site/downlink context by refreshing it periodically even
            # during idle periods, and always refresh immediately for a candidate.
            self._idle_site_cycle=(self._idle_site_cycle+1)%3
            if m or self._idle_site_cycle==0:
                site,_,_,_=self._scan_band(float(self.cfg.get('site_band_start_hz',390e6)),float(self.cfg.get('site_band_end_hz',395e6)),'SITE')
            else:
                site=[]
            now=time.time(); self._remember_sites(site,now)
"""
if old_cycle in s:
    s=s.replace(old_cycle,new_cycle,1)
elif 'self._idle_site_cycle=(self._idle_site_cycle+1)%3' not in s:
    # Upgrade the previous fast-path version if present.
    previous = """            m,mf,mp,mfloor=self._scan_band(float(self.cfg.get('mobile_band_start_hz',380e6)),float(self.cfg.get('mobile_band_end_hz',385e6)),'MOBILE')
            # Fast path: when the mobile/uplink sweep has no plausible activity,
            # do not spend another full sweep on the site/downlink band. This
            # roughly halves idle scan-cycle work and increases revisit rate.
            if m:
                site,_,_,_=self._scan_band(float(self.cfg.get('site_band_start_hz',390e6)),float(self.cfg.get('site_band_end_hz',395e6)),'SITE')
            else:
                site=[]
            now=time.time(); self._remember_sites(site,now)
"""
    if previous not in s:
        raise SystemExit('scan-cycle patch point missing')
    s=s.replace(previous,new_cycle,1)

compile(s, str(p), 'exec')
p.write_text(s)
print('RF Eye fast scan patch installed:', p)
