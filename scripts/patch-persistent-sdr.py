#!/usr/bin/env python3
from pathlib import Path
import sys

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
init_new = init_old + "        self.sdr=None; self.sdr_sample_rate=None; self.sdr_gain=None\n"
if init_old in s and 'self.sdr_sample_rate' not in s:
    s = s.replace(init_old, init_new, 1)

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
            # Discard a very short retune transient, then capture the requested
            # window. Keeping the USB device open removes repeated process/device
            # startup without shortening the observation window itself.
            discard=max(4096, int(sr*0.006))
            self.sdr.read_samples(discard)
            return np.asarray(self.sdr.read_samples(int(count)),dtype=np.complex64)
        except Exception:
            self._close_direct_sdr()
            return None

    def _capture(self,center,sr,n,blocks,transient=False):
        iq=self._direct_samples(center,sr,n*blocks)
        if iq is None or len(iq)<n:
            exe=shutil.which('rtl_sdr')
            if not exe:raise RuntimeError('rtl_sdr command not found')
            cmd=[exe,'-f',str(int(center)),'-s',str(int(sr)),'-p',str(int(self.cfg.get('ppm',0)))]
            gain=self.cfg.get('gain','auto')
            if gain!='auto':cmd+=['-g',str(float(gain))]
            cmd+=['-n',str(int(n*blocks)),'-']
            p=subprocess.Popen(cmd,stdout=subprocess.PIPE,stderr=subprocess.PIPE,start_new_session=True)
            try:out,err=p.communicate(timeout=3.)
            except subprocess.TimeoutExpired:
                try:os.killpg(p.pid,signal.SIGKILL)
                except Exception:p.kill()
                out,err=p.communicate();raise RuntimeError('rtl_sdr capture timeout')
            if p.returncode!=0 or len(out)<n*2:
                msg=err.decode('utf-8','ignore').strip()[-240:];raise RuntimeError(msg or f'rtl_sdr exit {p.returncode}')
            raw=np.frombuffer(out,dtype=np.uint8).astype(np.float32); iq=(raw[0::2]-127.5)+1j*(raw[1::2]-127.5)
        win=np.hanning(n).astype(np.float32); rows=[]
        for i in range(min(blocks,len(iq)//n)):
            x=iq[i*n:(i+1)*n].astype(np.complex64,copy=True); x-=np.mean(x); spectrum=np.fft.fftshift(np.fft.fft(x*win)); rows.append(20*np.log10(np.abs(spectrum)+1e-12))
        if not rows:raise RuntimeError('no complete FFT blocks')
        stack=np.vstack(rows); psd=np.percentile(stack,float(self.cfg.get('mobile_percentile',95)),axis=0) if transient else np.mean(stack,axis=0)
        mid=len(psd)//2; lo=max(0,mid-2); hi=min(len(psd),mid+3)
        if lo>0 and hi<len(psd):
            side=np.r_[psd[max(0,lo-8):lo],psd[hi:min(len(psd),hi+8)]]; fill=float(np.median(side)); psd[lo:hi]=fill; stack[:,lo:hi]=fill
        return np.fft.fftshift(np.fft.fftfreq(n,1./sr))+center,psd,stack

'''
s = s[:capture_start] + new_capture + s[capture_end:]

# Close the persistent handle before an attempted USB reset.
s = s.replace(
    "        self.last_usb_reset=now; exe=shutil.which('usbreset')\n",
    "        self.last_usb_reset=now; self._close_direct_sdr(); exe=shutil.which('usbreset')\n",
    1,
)

compile(s, str(p), 'exec')
p.write_text(s)
print('RF Eye persistent SDR patch installed:', p)
