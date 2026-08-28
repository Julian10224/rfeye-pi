#!/usr/bin/env python3
from pathlib import Path
import sys
import subprocess

if len(sys.argv) != 2:
    raise SystemExit('usage: patch-persistent-sdr.py /path/to/sdr_backend.py')

p = Path(sys.argv[1])
s = p.read_text()

if 'from rtlsdr import RtlSdr' not in s:
    s = s.replace(
        'import numpy as np\n',
        'import numpy as np\n\ntry:\n    from rtlsdr import RtlSdr\nexcept Exception:\n    RtlSdr = None\n',
        1,
    )

init_old = "        self._demo_forced=bool(cfg.get('demo_mode',False))\n"
init_new = init_old + "        self.sdr=None; self.sdr_sample_rate=None; self.sdr_gain=None; self._fft_window_cache={}\n"
if init_old in s and 'self.sdr_sample_rate' not in s:
    s = s.replace(init_old, init_new, 1)
elif 'self._fft_window_cache' not in s:
    s=s.replace('        self.sdr=None; self.sdr_sample_rate=None; self.sdr_gain=None\n','        self.sdr=None; self.sdr_sample_rate=None; self.sdr_gain=None; self._fft_window_cache={}\n',1)

stop_old = "    def stop(self):\n        self.running=False\n        if self.thread:self.thread.join(timeout=2.)\n"
stop_new = """    def stop(self):
        self.running=False
        if self.thread:self.thread.join(timeout=2.)
        self._close_direct_sdr()
"""
if stop_old in s:
    s = s.replace(stop_old, stop_new, 1)

capture_start = s.find('    def _capture(self,center,sr,n,blocks,transient=False):\n')
capture_end = s.find('    @staticmethod\n    def _duty_q', capture_start)
if capture_start < 0 or capture_end < 0:
    raise SystemExit('capture method patch point missing')

new_capture = '''    def _close_direct_sdr(self):
        dev=self.sdr
        self.sdr=None; self.sdr_sample_rate=None; self.sdr_gain=None
        if dev is not None:
            try: dev.close()
            except Exception: pass

    def _direct_samples(self,center,sr,count):
        if RtlSdr is None:
            return None
        try:
            if self.sdr is None:
                self.sdr=RtlSdr()
                self.sdr_sample_rate=None; self.sdr_gain=None
            if self.sdr_sample_rate != int(sr):
                self.sdr.sample_rate=int(sr); self.sdr_sample_rate=int(sr)
            ppm=int(self.cfg.get('ppm',0))
            try: self.sdr.freq_correction=ppm
            except Exception: pass
            gain=self.cfg.get('gain','auto')
            wanted_gain='auto' if gain=='auto' else float(gain)
            if self.sdr_gain != wanted_gain:
                self.sdr.gain=wanted_gain; self.sdr_gain=wanted_gain
            self.sdr.center_freq=int(center)
            # Keep the same retune settling margin and requested sample count.
            # Normal capture time is still determined by the RF observation
            # window, not by process startup or multi-second fallback timeouts.
            discard=max(4096, int(sr*0.006))
            self.sdr.read_samples(discard)
            return np.asarray(self.sdr.read_samples(int(count)),dtype=np.complex64)
        except Exception as e:
            self._close_direct_sdr()
            raise RuntimeError('direct RTL-SDR read failed: ' + str(e))

    def _capture(self,center,sr,n,blocks,transient=False):
        # pyrtlsdr is installed by RF Eye and is the real-time path. If it is
        # present but a device read fails, abort this scan cycle immediately so
        # recovery can run. The old code fell back to a 3-second rtl_sdr timeout
        # for every tuning window, which could turn one failed cycle into ~24 s.
        if RtlSdr is not None:
            iq=self._direct_samples(center,sr,n*blocks)
        else:
            # Compatibility fallback only when pyrtlsdr itself is unavailable.
            exe=shutil.which('rtl_sdr')
            if not exe:raise RuntimeError('rtl_sdr command not found')
            cmd=[exe,'-f',str(int(center)),'-s',str(int(sr)),'-p',str(int(self.cfg.get('ppm',0)))]
            gain=self.cfg.get('gain','auto')
            if gain!='auto':cmd+=['-g',str(float(gain))]
            cmd+=['-n',str(int(n*blocks)),'-']
            proc=subprocess.Popen(cmd,stdout=subprocess.PIPE,stderr=subprocess.PIPE,start_new_session=True)
            try:out,err=proc.communicate(timeout=0.8)
            except subprocess.TimeoutExpired:
                try:os.killpg(proc.pid,signal.SIGKILL)
                except Exception:proc.kill()
                out,err=proc.communicate();raise RuntimeError('rtl_sdr capture timeout')
            if proc.returncode!=0 or len(out)<n*2:
                msg=err.decode('utf-8','ignore').strip()[-240:];raise RuntimeError(msg or f'rtl_sdr exit {proc.returncode}')
            raw=np.frombuffer(out,dtype=np.uint8).astype(np.float32); iq=(raw[0::2]-127.5)+1j*(raw[1::2]-127.5)

        if iq is None or len(iq)<n:
            raise RuntimeError('short RTL-SDR capture')
        rows_count=min(blocks,len(iq)//n)
        if rows_count<1:raise RuntimeError('no complete FFT blocks')

        # Process all FFT blocks in one NumPy batch. Samples, windowing, FFT
        # size, percentiles and thresholds are unchanged.
        matrix=np.asarray(iq[:rows_count*n],dtype=np.complex64).reshape(rows_count,n).copy()
        matrix-=np.mean(matrix,axis=1,keepdims=True)
        win=self._fft_window_cache.get(n)
        if win is None:
            win=np.hanning(n).astype(np.float32); self._fft_window_cache[n]=win
        spectra=np.fft.fftshift(np.fft.fft(matrix*win,axis=1),axes=1)
        stack=20*np.log10(np.abs(spectra)+1e-12)
        psd=np.percentile(stack,float(self.cfg.get('mobile_percentile',95)),axis=0) if transient else np.mean(stack,axis=0)
        mid=len(psd)//2; lo=max(0,mid-2); hi=min(len(psd),mid+3)
        if lo>0 and hi<len(psd):
            side=np.r_[psd[max(0,lo-8):lo],psd[hi:min(len(psd),hi+8)]]; fill=float(np.median(side)); psd[lo:hi]=fill; stack[:,lo:hi]=fill
        return np.fft.fftshift(np.fft.fftfreq(n,1./sr))+center,psd,stack

'''
s = s[:capture_start] + new_capture + s[capture_end:]

s = s.replace(
    "        self.last_usb_reset=now; exe=shutil.which('usbreset')\n",
    "        self.last_usb_reset=now; self._close_direct_sdr(); exe=shutil.which('usbreset')\n",
    1,
)

compile(s, str(p), 'exec')
p.write_text(s)
print('RF Eye persistent SDR patch installed:', p)

# Keep the installer entry-point simple: this patch already runs after the other
# app/backend patches, so install the debug timing page from here as the final
# source transformation. That avoids an extra installer dependency/order issue.
debug_patch = Path(__file__).with_name('patch-debug-menu.py')
app_path = p.with_name('app.py')
if debug_patch.exists() and app_path.exists():
    subprocess.run([sys.executable, str(debug_patch), str(app_path), str(p)], check=True)
