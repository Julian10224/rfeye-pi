import math
import shutil
import subprocess
import os
import signal
import threading
import time
import numpy as np

class SDRBackend:
    """Passive C2000/TETRA RF activity monitor."""
    def __init__(self, cfg):
        self.cfg = cfg
        self.lock = threading.Lock()
        self.running = False
        self.thread = None
        self.status = "STARTING"
        self.error = ""
        self.peaks = []
        self.mobile_peaks = []
        self.site_peaks = []
        self.mobile_level = 0.0
        self.site_level = 0.0
        self.mobile_hits = 0
        self.last_good_scan = 0.0
        self.scan_failures = 0
        self.last_usb_reset = 0.0
        self.spectrum_freqs = np.array([], dtype=np.float32)
        self.spectrum_db = np.array([], dtype=np.float32)
        self.noise_floor_db = -100.0
        self.last_update = 0.0
        self.demo_active = False
        self._demo_forced = bool(cfg.get("demo_mode", False))

    def set_demo(self, enabled):
        with self.lock:
            self._demo_forced = bool(enabled)
            self.cfg["demo_mode"] = bool(enabled)

    def start(self):
        self.running = True
        self.thread = threading.Thread(target=self._run, daemon=True)
        self.thread.start()

    def stop(self):
        self.running = False
        if self.thread:
            self.thread.join(timeout=2.0)

    def snapshot(self):
        with self.lock:
            return {
                "status": self.status,
                "error": self.error,
                "peaks": [dict(p) for p in self.peaks],
                "mobile_peaks": [dict(p) for p in self.mobile_peaks],
                "site_peaks": [dict(p) for p in self.site_peaks],
                "mobile_level": float(self.mobile_level),
                "site_level": float(self.site_level),
                "mobile_confirmed": self.mobile_hits >= int(self.cfg.get("confirm_hits", 2)),
                "freqs": self.spectrum_freqs.copy(),
                "spectrum": self.spectrum_db.copy(),
                "noise": float(self.noise_floor_db),
                "last_update": float(self.last_update),
                "demo": bool(self.demo_active),
            }

    def _run(self):
        while self.running:
            with self.lock:
                demo = self._demo_forced
            if demo:
                self._demo_once()
            else:
                ok = self._scan_cycle()
                if not ok:
                    if self.cfg.get("auto_demo_if_no_sdr", False):
                        self._demo_once()
                    else:
                        time.sleep(0.5)

    def _centers_for(self, start_hz, end_hz, sr):
        usable = sr * 0.72
        half = usable / 2.0
        if end_hz - start_hz <= usable:
            return [(start_hz + end_hz) / 2.0]
        centers = []
        c = start_hz + half
        while c < end_hz:
            centers.append(c)
            c += usable
        if centers and centers[-1] + half < end_hz:
            centers.append(end_hz - half)
        return centers

    def _recover_sdr_usb(self):
        now = time.time()
        if now - self.last_usb_reset < 5.0:
            return False
        self.last_usb_reset = now
        exe = shutil.which("usbreset")
        if not exe:
            return False
        try:
            cp = subprocess.run([exe, "0bda:2838"], capture_output=True, text=True, timeout=6)
            if cp.returncode == 0:
                time.sleep(1.5)
                return True
        except Exception:
            pass
        return False

    def _capture(self, center, sr, fft_size, nblocks):
        exe = shutil.which("rtl_sdr")
        if not exe:
            raise RuntimeError("rtl_sdr command not found")
        nsamp = fft_size * nblocks
        cmd = [exe, "-f", str(int(center)), "-s", str(int(sr)),
               "-p", str(int(self.cfg.get("ppm", 0))),
               "-n", str(int(nsamp)), "-"]
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                start_new_session=True)
        try:
            out, err = proc.communicate(timeout=3.0)
        except subprocess.TimeoutExpired:
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except Exception:
                proc.kill()
            out, err = proc.communicate()
            raise RuntimeError("rtl_sdr capture timeout")
        if proc.returncode != 0 or len(out) < fft_size * 2:
            msg = err.decode("utf-8", "ignore").strip()[-240:]
            raise RuntimeError(msg or f"rtl_sdr exit {proc.returncode}")
        raw = np.frombuffer(out, dtype=np.uint8).astype(np.float32)
        iq = (raw[0::2] - 127.5) + 1j * (raw[1::2] - 127.5)
        window = np.hanning(fft_size).astype(np.float32)
        actual = min(nblocks, len(iq) // fft_size)
        acc = None
        for i in range(actual):
            blk = iq[i*fft_size:(i+1)*fft_size]
            spec = np.fft.fftshift(np.fft.fft(blk * window))
            power = 20.0 * np.log10(np.abs(spec) + 1e-12)
            acc = power if acc is None else acc + power
        psd = acc / max(1, actual)
        freqs = np.fft.fftshift(np.fft.fftfreq(fft_size, 1.0/sr)) + center
        return freqs, psd

    def _scan_band(self, start_hz, end_hz, label):
        sr = int(self.cfg.get("sample_rate", 2048000))
        fft_size = int(self.cfg.get("fft_size", 1024))
        nblocks = int(self.cfg.get("fft_blocks", 8))
        all_f, all_p = [], []
        for center in self._centers_for(start_hz, end_hz, sr):
            f, p = self._capture(center, sr, fft_size, nblocks)
            mask = (f >= start_hz) & (f <= end_hz)
            if np.any(mask):
                all_f.append(f[mask]); all_p.append(p[mask])
        if not all_p:
            return [], np.array([]), np.array([]), -120.0
        f = np.concatenate(all_f); p = np.concatenate(all_p)
        order = np.argsort(f); f, p = f[order], p[order]
        floor = float(np.percentile(p, 40))
        threshold = floor + float(self.cfg.get("threshold_db", 10.0))
        loc = np.where((p[1:-1] > p[:-2]) & (p[1:-1] >= p[2:]) &
                       (p[1:-1] > threshold))[0] + 1
        cand = sorted([(float(p[i]), float(f[i])) for i in loc], reverse=True)
        peaks = []
        for power, freq in cand:
            if any(abs(freq-q["freq_hz"]) < 18000 for q in peaks):
                continue
            snr = power - floor
            level = max(0.0, min(1.0, (snr - threshold + floor) / 24.0))
            peaks.append({"freq_hz": freq, "power_db": power,
                          "snr_db": snr, "level": level,
                          "band": label, "last_seen": time.time()})
            if len(peaks) >= 3:
                break
        return peaks, f, p, floor

    def _scan_cycle(self):
        try:
            m0 = float(self.cfg.get("mobile_band_start_hz", 380e6))
            m1 = float(self.cfg.get("mobile_band_end_hz", 385e6))
            s0 = float(self.cfg.get("site_band_start_hz", 390e6))
            s1 = float(self.cfg.get("site_band_end_hz", 395e6))
            mobile, mf, mp, mfloor = self._scan_band(m0, m1, "MOBILE")
            site, sf, sp, sfloor = self._scan_band(s0, s1, "SITE")
            paired_mobile = []
            for m in mobile:
                target = m["freq_hz"] + 10_000_000.0
                if any(abs(s["freq_hz"] - target) <= 20_000.0 for s in site):
                    paired_mobile.append(m)
            ml = max([p["level"] for p in paired_mobile], default=0.0)
            sl = max([p["level"] for p in site], default=0.0)
            if ml >= 0.22:
                self.mobile_hits = min(6, self.mobile_hits + 1)
            else:
                self.mobile_hits = max(0, self.mobile_hits - int(self.cfg.get("clear_hits", 2)))
            confirmed = self.mobile_hits >= int(self.cfg.get("confirm_hits", 2))
            shown = ml if confirmed else 0.0
            idx = np.linspace(0, len(mp)-1, min(240, len(mp))).astype(int) if len(mp) else np.array([], dtype=int)
            with self.lock:
                self.mobile_peaks = paired_mobile
                self.site_peaks = site
                self.peaks = paired_mobile[:3] if confirmed else []
                self.mobile_level = shown
                self.site_level = sl
                self.spectrum_freqs = mf[idx].astype(np.float64) if len(idx) else np.array([])
                self.spectrum_db = mp[idx].astype(np.float32) if len(idx) else np.array([])
                self.noise_floor_db = mfloor
                self.last_update = time.time()
                self.status = "LIVE"
                self.error = ""
                self.demo_active = False
                self.last_good_scan = time.time()
                self.scan_failures = 0
            return True
        except Exception as e:
            err = str(e)
            if "timeout" in err.lower():
                self._recover_sdr_usb()
            with self.lock:
                self.scan_failures += 1
                recently_good = self.last_good_scan and (time.time() - self.last_good_scan < 8.0)
                if not recently_good and self.scan_failures >= 3:
                    self.status = "NO SDR"
                    self.peaks = []
                    self.mobile_peaks = []
                    self.site_peaks = []
                    self.mobile_level = 0.0
                    self.site_level = 0.0
                    self.mobile_hits = 0
                    self.spectrum_freqs = np.array([], dtype=np.float32)
                    self.spectrum_db = np.array([], dtype=np.float32)
                self.error = err
                self.demo_active = False
            return False

    def _demo_once(self):
        t = time.time()
        vals = [0.10 + 0.75*max(0.0, math.sin(t*0.70))**8,
                0.06 + 0.50*max(0.0, math.sin(t*0.44+1.7))**10,
                0.04 + 0.85*max(0.0, math.sin(t*0.28+3.1))**12]
        base = float(self.cfg.get("mobile_band_start_hz", 380e6))
        span = float(self.cfg.get("mobile_band_end_hz", 385e6)) - base
        peaks = [{"freq_hz": base+span*(0.2+i*0.3), "power_db": -80+v*30,
                  "snr_db": 8+v*30, "level": v, "band":"MOBILE",
                  "last_seen": time.time()} for i,v in enumerate(vals) if v>0.12]
        x=np.linspace(0,1,220); spec=-105+4*np.sin(x*15+t)
        with self.lock:
            self.peaks = peaks[:3]
            self.mobile_peaks = peaks[:3]
            self.site_peaks = []
            self.mobile_level = max(vals)
            self.site_level = 0.45
            self.spectrum_freqs = np.linspace(base, base+span, len(x))
            self.spectrum_db = spec.astype(np.float32)
            self.noise_floor_db = -103.0
            self.last_update = time.time()
            self.status = "DEMO"
            self.error = ""
            self.demo_active = True
        time.sleep(0.08)
