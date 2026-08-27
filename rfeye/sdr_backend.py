import math
import shutil
import subprocess
import os
import signal
import threading
import time
import numpy as np

class SDRBackend:
    """Passive C2000/TETRA RF activity monitor.

    The mobile/uplink band is treated as burst activity. The site/downlink
    band is measured separately so continuous infrastructure carriers do not
    appear as mobile RF activity.
    """
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
        self.site_recent = {}
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
        # Use only the flatter middle of the RTL-SDR passband and overlap sweeps.
        # Tune on 25 kHz channel *boundaries* so tuner DC lands between TETRA carriers.
        usable = sr * 0.68
        half = usable / 2.0
        step = usable * 0.90
        centers = []
        c = start_hz + half
        while True:
            snapped = start_hz + round((c - start_hz) / 25_000.0) * 25_000.0
            if centers and snapped <= centers[-1]:
                snapped = centers[-1] + 25_000.0
            centers.append(snapped)
            if snapped + half >= end_hz:
                break
            c = snapped + step
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

    def _capture(self, center, sr, fft_size, nblocks, transient=False):
        exe = shutil.which("rtl_sdr")
        if not exe:
            raise RuntimeError("rtl_sdr command not found")
        nsamp = fft_size * nblocks
        cmd = [exe, "-f", str(int(center)), "-s", str(int(sr)),
               "-p", str(int(self.cfg.get("ppm", 0)))]
        gain = self.cfg.get("gain", "auto")
        if gain != "auto":
            cmd += ["-g", str(float(gain))]
        cmd += ["-n", str(int(nsamp)), "-"]
        proc = subprocess.Popen(
            cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, start_new_session=True
        )
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
        blocks = []
        for i in range(actual):
            blk = iq[i*fft_size:(i+1)*fft_size]
            blk = blk - np.mean(blk)
            spec = np.fft.fftshift(np.fft.fft(blk * window))
            blocks.append(20.0 * np.log10(np.abs(spec) + 1e-12))
        if not blocks:
            raise RuntimeError("no complete FFT blocks")
        stack = np.vstack(blocks)
        percentile = float(self.cfg.get("mobile_percentile", 95.0))
        psd = np.percentile(stack, percentile, axis=0) if transient else np.mean(stack, axis=0)
        mid = len(psd) // 2
        lo, hi = max(0, mid-2), min(len(psd), mid+3)
        if lo > 0 and hi < len(psd):
            fill = np.median(np.concatenate((psd[max(0,lo-8):lo], psd[hi:min(len(psd),hi+8)])))
            psd[lo:hi] = fill
            stack[:, lo:hi] = fill
        freqs = np.fft.fftshift(np.fft.fftfreq(fft_size, 1.0/sr)) + center
        return freqs, psd, stack

    def _scan_band(self, start_hz, end_hz, label):
        sr = int(self.cfg.get("sample_rate", 2048000))
        fft_size = int(self.cfg.get("fft_size", 1024))
        base_blocks = int(self.cfg.get("fft_blocks", 8))
        capture_ms = float(self.cfg.get(
            "mobile_capture_ms" if label == "MOBILE" else "site_capture_ms", 72.0
        ))
        nblocks = max(base_blocks, int(math.ceil((sr * capture_ms / 1000.0) / fft_size)))
        usable_half = sr * 0.68 / 2.0
        chan_half = float(self.cfg.get("tetra_channel_half_width_hz", 9000.0))
        gate_db = float(self.cfg.get("burst_gate_db", 6.0))
        all_f, all_p = [], []
        observations = {}

        channels = np.arange(start_hz + 12_500.0, end_hz, 25_000.0)
        for center in self._centers_for(start_hz, end_hz, sr):
            f, p, stack = self._capture(center, sr, fft_size, nblocks, transient=(label == "MOBILE"))
            usable_mask = ((f >= start_hz) & (f <= end_hz) &
                           (np.abs(f - center) <= usable_half))
            if np.any(usable_mask):
                all_f.append(f[usable_mask]); all_p.append(p[usable_mask])

            for ch in channels:
                edge_margin = usable_half - abs(ch - center)
                if edge_margin < 15_000.0:
                    continue
                bins = np.where(np.abs(f - ch) <= chan_half)[0]
                if len(bins) < 2:
                    continue
                # Integrate power across the occupied part of one 25 kHz TETRA channel.
                series = 10.0 * np.log10(
                    np.sum(np.power(10.0, stack[:, bins] / 10.0), axis=1) + 1e-20
                )
                low = float(np.percentile(series, 20))
                median = float(np.percentile(series, 50))
                high = float(np.percentile(series, 95))
                span = high - low
                duty = float(np.mean(series > (low + gate_db)))
                obs = {
                    "freq_hz": float(ch), "low_db": low, "median_db": median,
                    "power_db": high, "snr_db": span, "burst_span_db": span,
                    "duty": duty, "band": label, "last_seen": time.time(),
                    "edge_margin": float(edge_margin),
                }
                old = observations.get(float(ch))
                if old is None or obs["edge_margin"] > old["edge_margin"]:
                    observations[float(ch)] = obs

        if not all_p:
            return [], np.array([]), np.array([]), -120.0
        f = np.concatenate(all_f); p = np.concatenate(all_p)
        order = np.argsort(f); f, p = f[order], p[order]
        floor = float(np.percentile(p, 40))
        obs = list(observations.values())
        peaks = []

        if label == "MOBILE":
            threshold = float(self.cfg.get("threshold_db", 12.0))
            required_span = max(float(self.cfg.get("min_burst_span_db", 9.0)), threshold)
            duty_min = float(self.cfg.get("min_burst_duty", 0.035))
            duty_max = float(self.cfg.get("max_burst_duty", 0.65))
            for q in obs:
                if q["burst_span_db"] < required_span:
                    continue
                if not (duty_min <= q["duty"] <= duty_max):
                    continue
                q = dict(q)
                q["level"] = max(0.0, min(1.0,
                    0.25 + (q["burst_span_db"] - required_span) / 20.0))
                q["raster_ok"] = True
                peaks.append(q)
            peaks.sort(key=lambda q: (q["burst_span_db"], q["power_db"]), reverse=True)
        else:
            # Base stations may be continuous. Compare exact 25 kHz channel power
            # against the population of channels instead of applying burst criteria.
            if obs:
                channel_floor = float(np.percentile([q["low_db"] for q in obs], 40))
                site_min = float(self.cfg.get("site_min_snr_db", 5.0))
                burst_min = float(self.cfg.get("site_burst_snr_db", 8.0))
                for q in obs:
                    steady_snr = q["low_db"] - channel_floor
                    burst_snr = q["power_db"] - channel_floor
                    if steady_snr < site_min and burst_snr < burst_min:
                        continue
                    q = dict(q)
                    q["snr_db"] = max(steady_snr, burst_snr - 2.0)
                    q["level"] = max(0.0, min(1.0, 0.2 + (q["snr_db"] - site_min) / 18.0))
                    peaks.append(q)
                peaks.sort(key=lambda q: q["snr_db"], reverse=True)

        candidate_limit = max(32, int(self.cfg.get("max_signals", 3)) * 10)
        return peaks[:candidate_limit], f, p, floor

    def _scan_cycle(self):
        try:
            m0 = float(self.cfg.get("mobile_band_start_hz", 380e6))
            m1 = float(self.cfg.get("mobile_band_end_hz", 385e6))
            s0 = float(self.cfg.get("site_band_start_hz", 390e6))
            s1 = float(self.cfg.get("site_band_end_hz", 395e6))
            mobile, mf, mp, mfloor = self._scan_band(m0, m1, "MOBILE")
            site, sf, sp, sfloor = self._scan_band(s0, s1, "SITE")

            now = time.time()
            for q in site:
                self.site_recent[round(q["freq_hz"])] = now
            max_age = float(self.cfg.get("site_pair_memory_s", 4.0))
            self.site_recent = {f:t for f,t in self.site_recent.items() if now - t <= max_age}

            # Network-mode TETRA uses paired 25 kHz channels 10 MHz apart.
            # Requiring recent downlink evidence prevents unrelated 380-385 MHz
            # interference from being presented as network mobile activity.
            require_pair = bool(self.cfg.get("require_duplex_pair", True))
            accepted_mobile = []
            for m in mobile:
                target = round(m["freq_hz"] + 10_000_000.0)
                paired_now = any(abs(s["freq_hz"] - target) <= 1_500.0 for s in site)
                paired_recent = any(abs(f - target) <= 1_500 for f in self.site_recent)
                paired = paired_now or paired_recent
                if require_pair and not paired:
                    continue
                q = dict(m)
                q["paired"] = paired
                accepted_mobile.append(q)
            accepted_mobile.sort(key=lambda q: (q.get("burst_span_db", 0.0), q["power_db"]), reverse=True)
            shown_mobile = accepted_mobile[:int(self.cfg.get("max_signals", 3))]

            ml = max([p["level"] for p in shown_mobile], default=0.0)
            sl = max([p["level"] for p in site], default=0.0)
            if ml >= 0.22:
                self.mobile_hits = min(6, self.mobile_hits + 1)
            else:
                self.mobile_hits = max(0, self.mobile_hits - int(self.cfg.get("clear_hits", 2)))
            confirmed = self.mobile_hits >= int(self.cfg.get("confirm_hits", 2))
            shown = ml if confirmed else 0.0

            idx = np.linspace(0, len(mp)-1, min(240, len(mp))).astype(int) if len(mp) else np.array([], dtype=int)
            with self.lock:
                self.mobile_peaks = accepted_mobile
                self.site_peaks = site
                self.peaks = shown_mobile if confirmed else []
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
