#!/usr/bin/env python3
from pathlib import Path
import sys

if len(sys.argv) != 2:
    raise SystemExit('usage: patch-ctypes-sdr.py /path/to/sdr_backend.py')

p = Path(sys.argv[1])
s = p.read_text()

s = s.replace(
    'import math, os, shutil, signal, subprocess, threading, time\nimport numpy as np\n',
    'import math, os, shutil, signal, subprocess, threading, time, ctypes, ctypes.util\nimport numpy as np\n',
    1,
)

marker = 'def clamp(v, lo=0.0, hi=1.0): return max(lo, min(hi, float(v)))\n'
wrapper = r'''

class _PersistentRTL:
    """Persistent librtlsdr wrapper using only stable C API calls.

    This avoids the pyrtlsdr/librtlsdr ABI mismatch seen on RTL-SDR Blog V4
    systems while keeping one USB device handle open across tuning windows.
    """
    def __init__(self, index=0):
        name=ctypes.util.find_library('rtlsdr') or 'librtlsdr.so.0'
        self.lib=ctypes.CDLL(name)
        self.dev=ctypes.c_void_p()
        self.lib.rtlsdr_open.argtypes=[ctypes.POINTER(ctypes.c_void_p),ctypes.c_uint32]
        self.lib.rtlsdr_open.restype=ctypes.c_int
        self.lib.rtlsdr_close.argtypes=[ctypes.c_void_p]
        self.lib.rtlsdr_set_sample_rate.argtypes=[ctypes.c_void_p,ctypes.c_uint32]
        self.lib.rtlsdr_set_center_freq.argtypes=[ctypes.c_void_p,ctypes.c_uint32]
        self.lib.rtlsdr_set_freq_correction.argtypes=[ctypes.c_void_p,ctypes.c_int]
        self.lib.rtlsdr_set_tuner_gain_mode.argtypes=[ctypes.c_void_p,ctypes.c_int]
        self.lib.rtlsdr_set_tuner_gain.argtypes=[ctypes.c_void_p,ctypes.c_int]
        self.lib.rtlsdr_get_tuner_gains.argtypes=[ctypes.c_void_p,ctypes.POINTER(ctypes.c_int)]
        self.lib.rtlsdr_reset_buffer.argtypes=[ctypes.c_void_p]
        self.lib.rtlsdr_read_sync.argtypes=[ctypes.c_void_p,ctypes.c_void_p,ctypes.c_int,ctypes.POINTER(ctypes.c_int)]
        rc=self.lib.rtlsdr_open(ctypes.byref(self.dev),int(index))
        if rc != 0: raise RuntimeError(f'rtlsdr_open failed {rc}')
        self.sample_rate=None; self.gain=None; self.ppm=None

    def _check(self, rc, what):
        if rc != 0: raise RuntimeError(f'{what} failed {rc}')

    def configure(self, sr, ppm, gain):
        if self.sample_rate != int(sr):
            self._check(self.lib.rtlsdr_set_sample_rate(self.dev,int(sr)),'set_sample_rate')
            self.sample_rate=int(sr)
        if self.ppm != int(ppm):
            # Some RTL-SDR Blog V4 builds return -2 for an explicit zero PPM
            # request. Zero already means no correction, so skip that call.
            if int(ppm) != 0:
                self._check(self.lib.rtlsdr_set_freq_correction(self.dev,int(ppm)),'set_freq_correction')
            self.ppm=int(ppm)
        wanted='auto' if gain=='auto' else float(gain)
        if self.gain != wanted:
            if wanted == 'auto':
                self._check(self.lib.rtlsdr_set_tuner_gain_mode(self.dev,0),'gain_auto')
            else:
                self._check(self.lib.rtlsdr_set_tuner_gain_mode(self.dev,1),'gain_manual')
                n=self.lib.rtlsdr_get_tuner_gains(self.dev,None)
                tenth=int(round(float(wanted)*10))
                if n>0:
                    arr=(ctypes.c_int*n)()
                    self.lib.rtlsdr_get_tuner_gains(self.dev,arr)
                    tenth=min(arr,key=lambda x:abs(int(x)-tenth))
                self._check(self.lib.rtlsdr_set_tuner_gain(self.dev,int(tenth)),'set_tuner_gain')
            self.gain=wanted

    def tune(self, center):
        self._check(self.lib.rtlsdr_set_center_freq(self.dev,int(center)),'set_center_freq')

    def reset(self):
        self._check(self.lib.rtlsdr_reset_buffer(self.dev),'reset_buffer')

    def read_complex(self, count):
        nbytes=int(count)*2
        buf=(ctypes.c_ubyte*nbytes)()
        got=ctypes.c_int()
        self._check(self.lib.rtlsdr_read_sync(self.dev,buf,nbytes,ctypes.byref(got)),'read_sync')
        if got.value < nbytes:
            raise RuntimeError(f'short read {got.value}/{nbytes}')
        raw=np.ctypeslib.as_array(buf).astype(np.float32)
        return ((raw[0::2]-127.5)+1j*(raw[1::2]-127.5)).astype(np.complex64,copy=False)

    def close(self):
        if self.dev:
            try: self.lib.rtlsdr_close(self.dev)
            finally: self.dev=None
'''

if '_PersistentRTL' not in s:
    if marker not in s:
        raise SystemExit('clamp insertion point missing')
    s=s.replace(marker,marker+wrapper,1)

if 'self.sdr_path=' not in s:
    s=s.replace(
        '        self.sdr=None; self.sdr_sample_rate=None; self.sdr_gain=None; self._fft_window_cache={}\n',
        '        self.sdr=None; self.sdr_sample_rate=None; self.sdr_gain=None; self._fft_window_cache={}; self.sdr_path="UNOPENED"\n',
        1,
    )

start=s.find('    def _close_direct_sdr(self):\n')
end=s.find('    def _capture(self,center,sr,n,blocks,transient=False):\n',start)
if start < 0 or end < 0:
    raise SystemExit('direct SDR method patch point missing')

new_direct = r'''    def _close_direct_sdr(self):
        dev=self.sdr
        self.sdr=None; self.sdr_sample_rate=None; self.sdr_gain=None; self.sdr_path='UNOPENED'
        if dev is not None:
            try: dev.close()
            except Exception: pass

    def _direct_samples(self,center,sr,count):
        try:
            if self.sdr is None:
                self.sdr=_PersistentRTL(int(self.cfg.get('sdr_device_index',0)))
                self.sdr_path='CTYPES PERSISTENT'
            self.sdr.configure(int(sr),int(self.cfg.get('ppm',0)),self.cfg.get('gain','auto'))
            self.sdr.tune(int(center))
            # Clear stale USB data after retune, then keep the same 6 ms settling
            # interval and exactly the same measurement sample count as before.
            self.sdr.reset()
            discard=max(4096,int(sr*0.006))
            self.sdr.read_complex(discard)
            return self.sdr.read_complex(int(count))
        except Exception as e:
            self._close_direct_sdr()
            raise RuntimeError('persistent librtlsdr read failed: '+str(e))

'''
s=s[:start]+new_direct+s[end:]

capstart=s.find('    def _capture(self,center,sr,n,blocks,transient=False):\n')
iqpos=s.find('        if iq is None or len(iq)<n:\n',capstart)
if capstart < 0 or iqpos < 0:
    raise SystemExit('capture patch point missing')

capture_prefix = r'''    def _capture(self,center,sr,n,blocks,transient=False):
        try:
            iq=self._direct_samples(center,sr,n*blocks)
        except Exception:
            if not self.cfg.get('allow_cli_sdr_fallback',True):
                raise
            self.sdr_path='RTL_SDR CLI'
            exe=shutil.which('rtl_sdr')
            if not exe: raise
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
                msg=err.decode('utf-8','ignore').strip()[-240:]
                raise RuntimeError(msg or f'rtl_sdr exit {proc.returncode}')
            raw=np.frombuffer(out,dtype=np.uint8).astype(np.float32)
            iq=(raw[0::2]-127.5)+1j*(raw[1::2]-127.5)

'''
s=s[:capstart]+capture_prefix+s[iqpos:]

# If the debug patch is already present, report the actual runtime path.
s=s.replace(
    "'sdr_path':('DIRECT' if globals().get('RtlSdr') is not None else 'RTL_SDR CLI')}",
    "'sdr_path':str(getattr(self,'sdr_path','?'))}",
)

compile(s,str(p),'exec')
p.write_text(s)
print('RF Eye ctypes persistent SDR patch installed:',p)
