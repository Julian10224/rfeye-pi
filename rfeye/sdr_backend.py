import json, math, os, shutil, signal, subprocess, threading, time, ctypes, ctypes.util
from pathlib import Path
import numpy as np

try:
    from rtlsdr import RtlSdr
except Exception:
    RtlSdr = None

def clamp(v, lo=0.0, hi=1.0): return max(lo, min(hi, float(v)))


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

class SDRBackend:
    """Passive generic TETRA/RF activity monitor; no identity or distance inference."""
    def __init__(self,cfg):
        self.cfg=cfg; self.lock=threading.Lock(); self.running=False; self.thread=None
        self.status='STARTING'; self.error=''; self.peaks=[]; self.mobile_peaks=[]; self.site_peaks=[]
        self.mobile_level=0.; self.site_level=0.; self.activity_confidence=0.; self.mobile_confirmed=False
        self.site_recent={}; self.last_good_scan=0.; self.scan_failures=0; self.last_usb_reset=0.
        self.spectrum_freqs=np.array([],dtype=np.float32); self.spectrum_db=np.array([],dtype=np.float32)
        self.noise_floor_db=-100.; self.last_update=0.; self.demo_active=False
        self._demo_forced=bool(cfg.get('demo_mode',False))
        self.last_cycle_ms=0.; self.last_mobile_scan_ms=0.; self.last_site_scan_ms=0.; self.last_capture_ms=0.; self.last_scan_windows=0
        self.sdr=None; self.sdr_sample_rate=None; self.sdr_gain=None; self._fft_window_cache={}; self.sdr_path="UNOPENED"
        self._idle_site_cycle=0
        self._confirm_streak=0; self._clear_streak=0; self._candidate_freq=None
        self._carrier_state={}; self._last_detection_time=0.
        self._artifact_sweep=0; self._artifact_samples={}; self._artifact_baseline={}; self._artifact_tainted=set()
        self._artifact_baseline_loaded=False
        self._artifact_comb_phase_hz=None; self._artifact_comb_support=0; self._artifact_comb_teeth=0
        self._load_artifact_baseline()
        self._mobile_temporal={}
        self.last_raw_mobile_candidates=0; self.last_post_artifact_candidates=0; self.last_broadband_kept=0
        self.last_post_comb_candidates=0; self.last_coherent_comb_rejected=0; self.last_comb_event_teeth=0
        self.last_novelty_rejected=0; self.last_pair_rejected=0; self.last_confidence_rejected=0
        self.debug_mobile_candidates=[]
        self.last_broadband_rejected=False
        self.last_static_rejected=False
        self.last_comb_rejected=False
    def set_demo(self,v):
        v=bool(v)
        with self.lock:
            self._demo_forced=v; self.cfg['demo_mode']=v
            if not v:
                self.demo_active=False; self.peaks=[]; self.mobile_peaks=[]; self.site_peaks=[]
                self.mobile_level=0.; self.site_level=0.; self.activity_confidence=0.; self.mobile_confirmed=False
                self._confirm_streak=0; self._clear_streak=0; self._candidate_freq=None
                self._carrier_state={}; self._last_detection_time=0.
                self._artifact_sweep=0; self._artifact_samples={}; self._artifact_baseline={}; self._artifact_tainted=set()
                self._artifact_baseline_loaded=False
                self._artifact_comb_phase_hz=None; self._artifact_comb_support=0; self._artifact_comb_teeth=0
                self._load_artifact_baseline()
                self._mobile_temporal={}
                self.last_raw_mobile_candidates=0; self.last_post_artifact_candidates=0; self.last_broadband_kept=0
                self.last_post_comb_candidates=0; self.last_coherent_comb_rejected=0; self.last_comb_event_teeth=0
                self.last_novelty_rejected=0; self.last_pair_rejected=0; self.last_confidence_rejected=0
                self.debug_mobile_candidates=[]
                self.last_broadband_rejected=False
                self.last_static_rejected=False
                self.last_comb_rejected=False
                self.status='SCANNING'; self.error=''; self.last_update=time.time()
    def start(self):
        if self.running:return
        self.running=True; self.thread=threading.Thread(target=self._run,daemon=True); self.thread.start()
    def stop(self):
        self.running=False
        t=self.thread
        # Never close a librtlsdr handle from the UI/service thread while the
        # scan thread may still be inside rtlsdr_read_sync(). That race can
        # wedge the RTL-SDR Blog V4 on the USB bus during restart/shutdown.
        if t and t is not threading.current_thread():
            timeout=max(3.0,float(self.cfg.get('sdr_stop_join_s',8.0)))
            t.join(timeout=timeout)
        # _run() owns normal handle shutdown in its finally block. Only close
        # here when there is no live scan thread (e.g. backend never started).
        if not t or not t.is_alive():
            self._close_direct_sdr()
    def snapshot(self):
        with self.lock:
            return {'status':self.status,'error':self.error,'peaks':[dict(p) for p in self.peaks],
            'mobile_peaks':[dict(p) for p in self.mobile_peaks],'site_peaks':[dict(p) for p in self.site_peaks],
            'mobile_level':float(self.mobile_level),'site_level':float(self.site_level),
            'activity_confidence':float(self.activity_confidence),'mobile_confirmed':bool(self.mobile_confirmed),
            'freqs':self.spectrum_freqs.copy(),'spectrum':self.spectrum_db.copy(),'noise':float(self.noise_floor_db),
            'last_update':float(self.last_update),'demo':bool(self.demo_active),
            'cycle_ms':float(self.last_cycle_ms),'mobile_scan_ms':float(self.last_mobile_scan_ms),
            'site_scan_ms':float(self.last_site_scan_ms),'capture_ms':float(self.last_capture_ms),
            'scan_windows':int(self.last_scan_windows),
            'confirm_streak':int(self._confirm_streak),'clear_streak':int(self._clear_streak),
            'broadband_rejected':bool(self.last_broadband_rejected),
            'comb_rejected':bool(self.last_comb_rejected),
            'static_rejected':bool(self.last_static_rejected),
            'artifact_calibrating':bool(self._artifact_sweep<int(self.cfg.get('artifact_calibration_sweeps',5))),
            'artifact_sweep':int(self._artifact_sweep),'artifact_baseline_count':len(self._artifact_baseline),
            'artifact_baseline_loaded':bool(self._artifact_baseline_loaded),
            'artifact_tainted_count':len(self._artifact_tainted),
            'raw_mobile_candidate_count':int(self.last_raw_mobile_candidates),
            'post_artifact_candidate_count':int(self.last_post_artifact_candidates),
            'post_comb_candidate_count':int(self.last_post_comb_candidates),
            'coherent_comb_rejected_count':int(self.last_coherent_comb_rejected),
            'comb_event_teeth':int(self.last_comb_event_teeth),
            'comb_profile_support':int(self._artifact_comb_support),
            'comb_profile_teeth':int(self._artifact_comb_teeth),
            'comb_profile_phase_hz':(float(self._artifact_comb_phase_hz)
                                     if self._artifact_comb_phase_hz is not None else None),
            'broadband_kept_count':int(self.last_broadband_kept),
            'novelty_rejected_count':int(self.last_novelty_rejected),
            'pair_rejected_count':int(self.last_pair_rejected),
            'confidence_rejected_count':int(self.last_confidence_rejected),
            'debug_mobile_candidates':[dict(p) for p in self.debug_mobile_candidates],
            'sdr_path':str(getattr(self,'sdr_path','?'))}
    def _run(self):
        try:
            while self.running:
                with self.lock: demo=self._demo_forced
                if demo:self._demo_once();continue
                if not self._scan_cycle():
                    if self.cfg.get('auto_demo_if_no_sdr',False):self._demo_once()
                    else:time.sleep(.5)
        finally:
            # Close from the same worker that performs synchronous USB reads.
            # This avoids cross-thread librtlsdr close/read races.
            self._close_direct_sdr()
    def _carrier_key(self,freq_hz):
        # C2000/TETRA carriers in this profile are on the ETSI +12.5 kHz
        # offset raster: 380.0125 MHz + N*25 kHz. Never round absolute
        # frequency to 25 kHz multiples; that can merge adjacent carriers.
        base=float(self.cfg.get('mobile_band_start_hz',380e6))+float(self.cfg.get('tetra_raster_offset_hz',12500.0))
        step=float(self.cfg.get('tetra_channel_spacing_hz',25000.0))
        n=int(math.floor((float(freq_hz)-base)/step+0.5))
        return int(round(base+n*step))

    def _centers_for(self,a,b,sr):
        usable=sr*.68; half=usable/2; step=usable*.9; out=[]; c=a+half
        raster=max(1.0,float(self.cfg.get('tetra_channel_spacing_hz',25000.0)))
        while True:
            x=a+round((c-a)/raster)*raster
            if out and x<=out[-1]:x=out[-1]+raster
            out.append(x)
            if x+half>=b:return out
            c=x+step
    def _recover_sdr_usb(self):
        now=time.time()
        if now-self.last_usb_reset<5:return False
        self.last_usb_reset=now; self._close_direct_sdr(); exe=shutil.which('usbreset')
        if not exe:return False
        try:
            cp=subprocess.run([exe,'0bda:2838'],capture_output=True,text=True,timeout=6)
            if cp.returncode==0:time.sleep(1.5);return True
        except Exception:pass
        return False
    def _close_direct_sdr(self):
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

    def _capture(self,center,sr,n,blocks,transient=False):
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
            expected_bytes=int(n*blocks)*2
            if proc.returncode!=0 or len(out)<expected_bytes:
                msg=err.decode('utf-8','ignore').strip()[-240:]
                if proc.returncode==0 and len(out)<expected_bytes:
                    msg=f'rtl_sdr short capture {len(out)}/{expected_bytes} bytes'
                raise RuntimeError(msg or f'rtl_sdr exit {proc.returncode}')
            raw=np.frombuffer(out[:expected_bytes],dtype=np.uint8).astype(np.float32)
            iq=(raw[0::2]-127.5)+1j*(raw[1::2]-127.5)

        if iq is None or len(iq)<n:
            raise RuntimeError('short RTL-SDR capture')
        rows_count=min(blocks,len(iq)//n)
        if rows_count<1:raise RuntimeError('no complete FFT blocks')
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

    @staticmethod
    def _duty_q(d,lo,hi,pref_lo,pref_hi):
        if pref_lo<=d<=pref_hi:return 1.
        if d<pref_lo:return clamp((d-lo)/max(1e-6,pref_lo-lo))
        return clamp((hi-d)/max(1e-6,hi-pref_hi))

    def _annotate_mobile_temporal(self,vals,ch_floor):
        # Keep a slow per-channel reference for every raster observation, not
        # only channels that already qualify as peaks. A newly appearing local
        # burst can therefore be compared with what that same channel looked
        # like before it became a candidate.
        now=time.time()
        alpha=clamp(float(self.cfg.get('temporal_baseline_alpha',.08)),.01,.35)
        max_age=max(3.,float(self.cfg.get('temporal_state_max_age_s',30.0)))
        rscale=max(.5,float(self.cfg.get('temporal_rf_snr_scale_db',4.0)))
        dscale=max(.01,float(self.cfg.get('temporal_duty_scale',.10)))
        sscale=max(.5,float(self.cfg.get('temporal_span_scale_db',3.0)))
        rows=[];rf_deltas=[];span_deltas=[]
        for q0 in vals:
            q=dict(q0);f=int(round(float(q['freq_hz'])))
            rf=float(q['power_db'])-float(ch_floor);span=float(q.get('burst_span_db',0));duty=float(q.get('duty',0))
            q['rf_snr_db']=rf
            st=self._mobile_temporal.get(f)
            if st is not None and now-float(st.get('last',0))<=max_age:
                prev=(float(st['rf_snr']),float(st['duty']),float(st['span']))
                q['_temporal_prev']=prev
                rf_deltas.append(rf-prev[0]);span_deltas.append(span-prev[2])
            else:
                q['_temporal_prev']=None
            rows.append(q)
        # RTL-SDR AGC can move much of the band together. Remove the median
        # common-mode RF/span movement before calling one carrier transient.
        common_rf=float(np.median(rf_deltas)) if rf_deltas else 0.
        common_span=float(np.median(span_deltas)) if span_deltas else 0.
        seen=set()
        for q in rows:
            f=int(round(float(q['freq_hz'])));seen.add(f)
            rf=float(q['rf_snr_db']);duty=float(q.get('duty',0));span=float(q.get('burst_span_db',0))
            prev=q.pop('_temporal_prev',None)
            if prev is None:
                dr=dd=ds=departure=0.
                self._mobile_temporal[f]={'rf_snr':rf,'duty':duty,'span':span,'last':now}
            else:
                dr=abs((rf-prev[0])-common_rf)
                dd=abs(duty-prev[1])
                ds=abs((span-prev[2])-common_span)
                departure=max(dr/rscale,dd/dscale,ds/sscale)
                st=self._mobile_temporal[f]
                st['rf_snr']=float(st['rf_snr']*(1.-alpha)+rf*alpha)
                st['duty']=float(st['duty']*(1.-alpha)+duty*alpha)
                st['span']=float(st['span']*(1.-alpha)+span*alpha)
                st['last']=now
            q.update(temporal_departure=float(departure),
                     temporal_rf_delta_db=float(dr),temporal_duty_delta=float(dd),
                     temporal_span_delta_db=float(ds))
        self._mobile_temporal={f:s for f,s in self._mobile_temporal.items()
                               if now-float(s.get('last',0))<=max_age}
        return rows

    def _apply_broadband_guard(self,peaks,limit):
        self.last_broadband_rejected=False
        self.last_broadband_kept=len(peaks)
        if not limit or len(peaks)<=limit:
            return peaks
        self.last_broadband_rejected=True
        mindep=max(.5,float(self.cfg.get('broadband_temporal_min_departure',1.25)))
        keep=max(1,int(self.cfg.get('broadband_dynamic_keep_max',6)))
        # Crucial difference from 0.7.31: a busy sweep is no longer erased.
        # Preserve only locally changing carriers; stable broad clutter is
        # suppressed while a narrow transient can continue to pairing.
        out=[q for q in peaks if float(q.get('temporal_departure',0))>=mindep or q.get('warmup_transient')]
        out.sort(key=lambda q:(float(q.get('temporal_departure',0)),
                               float(q.get('baseline_departure',0)),
                               float(q.get('burst_quality',0)),
                               float(q.get('rf_quality',0))),reverse=True)
        out=out[:keep]
        self.last_broadband_kept=len(out)
        return out

    def _scan_band(self,a,b,label):
        sr=int(self.cfg.get('sample_rate',2048000)); n=int(self.cfg.get('fft_size',1024)); base=int(self.cfg.get('fft_blocks',8))
        ms=float(self.cfg.get('mobile_capture_ms' if label=='MOBILE' else 'site_capture_ms',64.)); blocks=max(base,math.ceil(sr*ms/1000/n))
        usable=sr*.68/2; half=float(self.cfg.get('tetra_channel_half_width_hz',9000)); gate=float(self.cfg.get('burst_gate_db',6))
        spacing=max(1.0,float(self.cfg.get('tetra_channel_spacing_hz',25000.0)))
        offset=float(self.cfg.get('tetra_raster_offset_hz',12500.0))
        channels=np.arange(a+offset,b,spacing,dtype=np.float64); obs={}; fs=[]; ps=[]
        bin_hz=float(sr)/float(n); half_bins=max(1,int(math.ceil(half/bin_hz)))
        offsets=np.arange(-half_bins,half_bins+1,dtype=np.int32); db_to_ln=math.log(10.0)/10.0
        for center in self._centers_for(a,b,sr):
            _cap_t=time.perf_counter()
            f,p,stack=self._capture(center,sr,n,blocks,label=='MOBILE')
            self.last_capture_ms=(time.perf_counter()-_cap_t)*1000.; self.last_scan_windows+=1
            mask=(f>=a)&(f<=b)&(np.abs(f-center)<=usable)
            if np.any(mask):fs.append(f[mask]);ps.append(p[mask])
            local=channels[(np.abs(channels-center)<=usable-15000.)]
            if not len(local):continue
            mids=np.rint((local-center)/bin_hz+n/2.).astype(np.int32)
            idx=mids[:,None]+offsets[None,:]
            valid=(idx[:,0]>=0)&(idx[:,-1]<stack.shape[1])
            local=local[valid];idx=idx[valid]
            if not len(local):continue
            # Vectorize all raster channels in this tuning window. The old
            # version repeated percentile and power conversion work per carrier,
            # which dominated CPU time on the Pi.
            energy=np.exp(stack[:,idx]*db_to_ln).sum(axis=2)
            series=(10.0/math.log(10.0))*np.log(energy+1e-20)
            q20,q50,q95=np.percentile(series,(20,50,95),axis=0)
            duties=np.mean(series>(q20[None,:]+gate),axis=0)
            margins=usable-np.abs(local-center); seen_at=time.time()
            for j,ch in enumerate(local):
                q={'freq_hz':float(ch),'low_db':float(q20[j]),'median_db':float(q50[j]),'power_db':float(q95[j]),
                   'snr_db':float(q95[j]-q20[j]),'burst_span_db':float(q95[j]-q20[j]),'duty':float(duties[j]),
                   'band':label,'last_seen':seen_at,'edge_margin':float(margins[j])}
                old=obs.get(float(ch))
                if old is None or q['edge_margin']>old['edge_margin']:obs[float(ch)]=q
        if not ps:return [],np.array([]),np.array([]),-120.
        f=np.concatenate(fs); p=np.concatenate(ps); order=np.argsort(f); f,p=f[order],p[order]; floor=float(np.percentile(p,40)); vals=list(obs.values())
        if not vals:return [],f,p,floor
        ch_floor=float(np.percentile([q['low_db'] for q in vals],40)); peaks=[]
        if label=='MOBILE':
            # Detection sensitivity is intentionally soft and automatic. There
            # is no user dB threshold: burst span and RF SNR continuously change
            # quality/confidence instead of crossing an adjustable hard cutoff.
            # Temporal annotation runs on every observed raster channel before
            # candidate gating, so a burst can be compared with its quiet past.
            vals=self._annotate_mobile_temporal(vals,ch_floor)
            gate=float(self.cfg.get('burst_gate_db',6.0))
            dlo=float(self.cfg.get('min_burst_duty',.035)); dhi=float(self.cfg.get('max_burst_duty',.65))
            plo=float(self.cfg.get('preferred_burst_duty_min',.06)); phi=float(self.cfg.get('preferred_burst_duty_max',.45))
            minsnr=float(self.cfg.get('mobile_min_rf_snr_db',5.0))
            for q0 in vals:
                if not dlo<=q0['duty']<=dhi:continue
                q=dict(q0); snr=float(q['rf_snr_db']); span=float(q['burst_span_db'])
                burst_q=clamp((span-gate+2.0)/14.0)
                rf_q=clamp((snr-minsnr+4.0)/16.0)
                if burst_q<=0.0 or rf_q<=0.0:continue
                q.update(burst_quality=burst_q,
                         duty_quality=self._duty_q(q['duty'],dlo,dhi,plo,phi),
                         rf_quality=rf_q,signal_strength=clamp((snr-minsnr+2.0)/24.0),raster_ok=True)
                q['level']=q['signal_strength'];peaks.append(q)
            peaks.sort(key=lambda q:(q['burst_quality'],q['rf_quality'],q['power_db']),reverse=True)
        else:
            smin=float(self.cfg.get('site_min_snr_db',5)); bmin=float(self.cfg.get('site_burst_snr_db',8))
            for q0 in vals:
                steady=q0['low_db']-ch_floor; burst=q0['power_db']-ch_floor
                if steady<smin and burst<bmin:continue
                q=dict(q0); snr=max(steady,burst-2); q.update(snr_db=float(snr),site_quality=clamp((snr-smin+3)/14),signal_strength=clamp((snr-smin)/24));q['level']=q['signal_strength'];peaks.append(q)
            peaks.sort(key=lambda q:(q['site_quality'],q['snr_db']),reverse=True)
        limit=max(32,int(self.cfg.get('max_signals',3))*10)
        if label=='SITE':
            limit=max(limit,int(self.cfg.get('site_max_candidates',64)))
        return peaks[:limit],f,p,floor

    def _comb_distance(self,freq_hz,phase_hz=None):
        phase=self._artifact_comb_phase_hz if phase_hz is None else phase_hz
        if phase is None:
            return 1e18
        period=max(float(self.cfg.get('artifact_comb_period_hz',400000.0)),25000.0)
        d=(float(freq_hz)-float(phase))%period
        return min(d,period-d)

    def _update_artifact_comb_profile(self):
        self._artifact_comb_phase_hz=None
        self._artifact_comb_support=0
        self._artifact_comb_teeth=0
        fs=sorted(int(f) for f in self._artifact_baseline)
        if not fs:
            return False
        period=max(float(self.cfg.get('artifact_comb_period_hz',400000.0)),25000.0)
        half=max(float(self.cfg.get('artifact_comb_half_width_hz',50000.0)),0.0)
        step=max(float(self.cfg.get('tetra_channel_spacing_hz',25000.0)),1.0)
        origin=float(self.cfg.get('mobile_band_start_hz',380e6))+float(
            self.cfg.get('tetra_raster_offset_hz',12500.0))
        bins=max(1,int(round(period/step)))
        best=None
        for k in range(bins):
            phase=origin+k*step
            matched=[f for f in fs if self._comb_distance(f,phase)<=half]
            teeth=len(set(int(round((f-phase)/period)) for f in matched))
            score=(len(matched),teeth)
            if best is None or score>best[0]:
                best=(score,phase)
        if best is None:
            return False
        support,teeth=best[0]
        fraction=float(support)/max(1,len(fs))
        if (support < int(self.cfg.get('artifact_comb_min_baseline_support',8)) or
                teeth < int(self.cfg.get('artifact_comb_min_baseline_teeth',4)) or
                fraction < float(self.cfg.get('artifact_comb_min_baseline_fraction',.45))):
            return False
        self._artifact_comb_phase_hz=float(best[1])
        self._artifact_comb_support=int(support)
        self._artifact_comb_teeth=int(teeth)
        return True

    def _reject_coherent_comb(self,peaks):
        self.last_coherent_comb_rejected=0
        self.last_comb_event_teeth=0
        if self._artifact_comb_phase_hz is None or not peaks:
            self.last_post_comb_candidates=len(peaks)
            return peaks
        period=max(float(self.cfg.get('artifact_comb_period_hz',400000.0)),25000.0)
        half=max(float(self.cfg.get('artifact_comb_half_width_hz',50000.0)),0.0)
        depmin=max(.5,float(self.cfg.get(
            'artifact_comb_event_min_departure',
            self.cfg.get('novelty_min_departure',1.25))))
        min_teeth=max(2,int(self.cfg.get('artifact_comb_event_min_teeth',2)))
        hot_teeth=set()
        for q in peaks:
            if (self._comb_distance(q.get('freq_hz',0))<=half and
                    float(q.get('temporal_departure',0))>=depmin):
                hot_teeth.add(int(round(
                    (float(q.get('freq_hz',0))-self._artifact_comb_phase_hz)/period)))
        self.last_comb_event_teeth=len(hot_teeth)
        if len(hot_teeth)<min_teeth:
            self.last_post_comb_candidates=len(peaks)
            return peaks
        out=[]
        for q in peaks:
            if self._comb_distance(q.get('freq_hz',0))<=half:
                self.last_coherent_comb_rejected+=1
                continue
            out.append(q)
        self.last_post_comb_candidates=len(out)
        if self.last_coherent_comb_rejected:
            self.last_comb_rejected=True
        return out

    def _artifact_state_path(self):
        override=os.getenv('RFEYE_ARTIFACT_BASELINE')
        if override:return Path(override)
        return Path.home()/'.local'/'state'/'rfeye'/'artifact-baseline.json'

    def _load_artifact_baseline(self):
        if not bool(self.cfg.get('artifact_baseline_persist',True)):return False
        try:
            path=self._artifact_state_path()
            if not path.exists():return False
            data=json.loads(path.read_text())
            if data.get('schema')!='rfeye-artifact-baseline-v1':return False
            stored_profile=int(data.get('detector_profile_version',0))
            current_profile=int(self.cfg.get('detector_profile_version',0))
            # v7 changes only the downstream treatment of the already-correct
            # v6 raster baseline. Reuse a v6 baseline once, then persist it as
            # v7 so OTA installation has comb protection immediately.
            upgrade_v6=(current_profile==7 and stored_profile==6)
            if stored_profile!=current_profile and not upgrade_v6:return False
            if int(data.get('sample_rate',0))!=int(self.cfg.get('sample_rate',0)):return False
            if int(data.get('mobile_band_start_hz',0))!=int(self.cfg.get('mobile_band_start_hz',0)):return False
            if int(data.get('mobile_band_end_hz',0))!=int(self.cfg.get('mobile_band_end_hz',0)):return False
            max_days=max(1.,float(self.cfg.get('artifact_baseline_max_age_days',30.0)))
            if time.time()-float(data.get('saved_at',0))>max_days*86400.:return False
            loaded={}
            for fk,v in (data.get('baseline') or {}).items():
                f=int(fk)
                vals={k:float(v[k]) for k in ('power','duty','span','rf_snr')}
                vals['hits']=int(v.get('hits',0))
                if not all(math.isfinite(vals[k]) for k in ('power','duty','span','rf_snr')):continue
                loaded[f]=vals
            if not loaded:return False
            self._artifact_baseline=loaded
            self._artifact_sweep=max(3,int(self.cfg.get('artifact_calibration_sweeps',5)))
            self._artifact_baseline_loaded=True
            self._update_artifact_comb_profile()
            if upgrade_v6:
                self._save_artifact_baseline()
            return True
        except Exception:
            return False

    def _save_artifact_baseline(self):
        if not self._artifact_baseline or not bool(self.cfg.get('artifact_baseline_persist',True)):return
        try:
            path=self._artifact_state_path();path.parent.mkdir(parents=True,exist_ok=True)
            data={'schema':'rfeye-artifact-baseline-v1',
                  'detector_profile_version':int(self.cfg.get('detector_profile_version',0)),
                  'sample_rate':int(self.cfg.get('sample_rate',0)),
                  'mobile_band_start_hz':int(self.cfg.get('mobile_band_start_hz',0)),
                  'mobile_band_end_hz':int(self.cfg.get('mobile_band_end_hz',0)),
                  'saved_at':time.time(),
                  'baseline':{str(f):dict(v) for f,v in self._artifact_baseline.items()}}
            tmp=path.with_suffix('.tmp');tmp.write_text(json.dumps(data,indent=2,sort_keys=True)+'\n');tmp.replace(path)
        except Exception:
            pass

    def _reject_static_artifacts(self,peaks):
        # Startup learning is deliberately conservative: a carrier is only
        # admitted to the clutter map when it is present often enough AND its
        # RF-SNR, duty and burst span are temporally stable. Any carrier that
        # becomes variable during warm-up is marked tainted and can escape
        # immediately instead of being learned as background.
        self._artifact_sweep+=1
        warm=max(3,int(self.cfg.get('artifact_calibration_sweeps',5)))
        minhits=max(2,int(self.cfg.get('artifact_min_baseline_hits',4)))
        rdelta=max(.5,float(self.cfg.get('artifact_rf_snr_delta_db',5.0)))
        ddelta=max(.02,float(self.cfg.get('artifact_duty_delta',.12)))
        sdelta=max(.5,float(self.cfg.get('artifact_span_delta_db',3.5)))
        max_rstd=max(.1,float(self.cfg.get('artifact_max_rf_snr_std_db',2.0)))
        max_dstd=max(.005,float(self.cfg.get('artifact_max_duty_std',.08)))
        max_sstd=max(.1,float(self.cfg.get('artifact_max_span_std_db',2.5)))
        self.last_static_rejected=False

        by_freq={}
        for q in peaks:
            f=self._carrier_key(q.get('freq_hz',0))
            old=by_freq.get(f)
            if old is None or float(q.get('power_db',-999))>float(old.get('power_db',-999)):
                by_freq[f]=q

        if self._artifact_sweep<=warm:
            for f,q in by_freq.items():
                hist=self._artifact_samples.setdefault(f,[])
                hist.append((float(q.get('power_db',0)),float(q.get('duty',0)),
                             float(q.get('burst_span_db',0)),
                             float(q.get('rf_snr_db',q.get('snr_db',0)))))
                if len(hist)>warm:del hist[:-warm]
                if len(hist)>=2 and f not in self._artifact_tainted:
                    a=np.asarray(hist,dtype=np.float32)
                    if (float(np.std(a[:,3]))>max_rstd or
                        float(np.std(a[:,1]))>max_dstd or
                        float(np.std(a[:,2]))>max_sstd):
                        self._artifact_tainted.add(f)

            if self._artifact_sweep==warm:
                for f,hist in self._artifact_samples.items():
                    if f in self._artifact_tainted or len(hist)<minhits:continue
                    a=np.asarray(hist,dtype=np.float32)
                    rstd=float(np.std(a[:,3]));dstd=float(np.std(a[:,1]));sstd=float(np.std(a[:,2]))
                    if rstd>max_rstd or dstd>max_dstd or sstd>max_sstd:continue
                    self._artifact_baseline[f]={'power':float(np.median(a[:,0])),
                        'duty':float(np.median(a[:,1])),'span':float(np.median(a[:,2])),
                        'rf_snr':float(np.median(a[:,3])),'hits':len(hist),
                        'rf_snr_std':rstd,'duty_std':dstd,'span_std':sstd}
                self._update_artifact_comb_profile()
                self._save_artifact_baseline()

            out=[]
            for q in peaks:
                f=self._carrier_key(q.get('freq_hz',0))
                if f in self._artifact_tainted:
                    q=dict(q);q['warmup_transient']=True
                    out.append(q)
            self.last_static_rejected=len(out)<len(peaks)
            return out

        out=[]
        for q in peaks:
            f=self._carrier_key(q.get('freq_hz',0))
            base=self._artifact_baseline.get(f)
            if base is None:
                out.append(q);continue
            dr=abs(float(q.get('rf_snr_db',q.get('snr_db',0)))-float(base['rf_snr']))
            dd=abs(float(q.get('duty',0))-float(base['duty']))
            ds=abs(float(q.get('burst_span_db',0))-float(base['span']))
            if dr>rdelta or dd>ddelta or ds>sdelta:
                q=dict(q);q['baseline_departure']=max(dr/rdelta,dd/ddelta,ds/sdelta)
                out.append(q);continue
            self.last_static_rejected=True
            # Only tiny EMA tracking is allowed after learning. It follows
            # thermal/AGC drift but cannot quickly absorb a new transmitter.
            base['power']=float(base['power']*.98+float(q.get('power_db',0))*.02)
            base['duty']=float(base['duty']*.98+float(q.get('duty',0))*.02)
            base['span']=float(base['span']*.98+float(q.get('burst_span_db',0))*.02)
            base['rf_snr']=float(base['rf_snr']*.98+float(q.get('rf_snr_db',q.get('snr_db',0)))*.02)
        return out

    def _remember_sites(self,site,now):
        age=float(self.cfg.get('site_pair_memory_s',5.0))
        for q in site:
            f=round(q['freq_hz']); old=self.site_recent.get(f,{}); hits=int(old.get('hits',0))+1 if now-float(old.get('time',0))<=age*1.5 else 1
            self.site_recent[f]={'time':now,'hits':min(hits,8),'quality':float(q.get('site_quality',q.get('level',0)))}
        self.site_recent={f:v for f,v in self.site_recent.items() if now-float(v.get('time',0))<=age}
    def _pair_info(self,target,site,now):
        tol=float(self.cfg.get('duplex_pair_tolerance_hz',1000))
        age=max(.1,float(self.cfg.get('site_pair_memory_s',5.0)))
        min_hits=max(1,int(self.cfg.get('site_pair_min_hits',2)))
        best=0.; current=False; best_age=1e9; best_hits=0
        # A site carrier seen in the current refresh is valid immediately.
        # Memory-only pairing is stricter: it must have been observed on at
        # least min_hits separate site refreshes and decays quickly with age.
        for s in site:
            if abs(float(s['freq_hz'])-float(target))<=tol:
                q=float(s.get('site_quality',s.get('level',0)))
                if q>=best:
                    f=round(float(s['freq_hz'])); meta=self.site_recent.get(f,{})
                    best=q; current=True; best_age=0.; best_hits=max(1,int(meta.get('hits',1)))
        for f,m in self.site_recent.items():
            if abs(float(f)-float(target))<=tol and int(m.get('hits',0))>=min_hits:
                a=max(0.,now-float(m.get('time',0)))
                q=float(m.get('quality',0))*clamp(1-a/age)
                if q>best:
                    best=q; current=False; best_age=a; best_hits=int(m.get('hits',0))
        return clamp(best),current,float(best_age),int(best_hits)

    def _pair_q(self,target,site,now):
        q,current,_age,_hits=self._pair_info(target,site,now)
        return q,current

    def _confidence(self,m,pair):
        dep=float(m.get('temporal_departure',0))
        lo=float(self.cfg.get('novelty_min_departure',1.25))
        hi=max(lo+.1,float(self.cfg.get('novelty_strong_departure',2.0)))
        novelty=clamp((dep-lo)/(hi-lo))
        return clamp(.30*m.get('burst_quality',0)+.12*m.get('duty_quality',0)+
                     .12*m.get('rf_quality',0)+.26*pair+.20*novelty)
    def _hysteresis(self,candidates,now):
        # Evidence is tracked per real +12.5 kHz TETRA raster carrier. Only
        # candidates that already passed the novelty + duplex gates reach this
        # method, so stationary paired spurs cannot preload confirmation hits.
        memory=max(.5,float(self.cfg.get('carrier_memory_s',2.5)))
        window=max(.5,float(self.cfg.get('confirm_window_s',2.2)))
        hold=max(0.,float(self.cfg.get('alert_hold_s',3.0)))
        strong=float(self.cfg.get('strong_hit_confidence',.78))
        minconf=float(self.cfg.get('candidate_min_confidence',.52))
        need=max(2,int(self.cfg.get('confirm_hits',2)))
        strong_dep=float(self.cfg.get('novelty_strong_departure',2.0))
        fresh_age=max(.1,float(self.cfg.get('strong_pair_max_age_s',2.5)))
        fresh_pair=float(self.cfg.get('strong_pair_min_quality',.75))
        memory_hits=max(1,int(self.cfg.get('site_pair_min_hits',2)))

        for q in candidates:
            f=self._carrier_key(q['freq_hz'])
            conf=float(q.get('confidence',0))
            st=self._carrier_state.get(f)
            if st is None or now-float(st.get('last',0))>window:
                st={'hits':0,'last':0.,'score':0.}
            st['hits']=int(st.get('hits',0))+1
            st['last']=now
            st['score']=max(conf,float(st.get('score',0))*.55+conf*.45)
            self._carrier_state[f]=st

        self._carrier_state={f:s for f,s in self._carrier_state.items()
                             if now-float(s.get('last',0))<=memory}
        raw=max([float(q.get('confidence',0)) for q in candidates],default=0.)
        old=float(self.activity_confidence)
        a=float(self.cfg.get('confidence_attack',.58) if raw>=old
                else self.cfg.get('confidence_release',.20))
        new=clamp(old+clamp(a)*(raw-old))

        qualified=[]
        for q in candidates:
            f=self._carrier_key(q['freq_hz']); st=self._carrier_state.get(f,{})
            conf=float(q.get('confidence',0)); hits=int(st.get('hits',0))
            dep=float(q.get('temporal_departure',0))
            pair=float(q.get('pair_quality',0))
            pair_age=float(q.get('pair_age_s',1e9))
            pair_hits=int(q.get('pair_hits',0))
            fresh=pair>=fresh_pair and (
                bool(q.get('paired_now')) or
                (pair_age<=fresh_age and pair_hits>=memory_hits)
            )
            immediate=dep>=strong_dep and conf>=strong and fresh
            repeated=hits>=need and conf>=minconf
            if immediate or repeated:
                q['confirm_reason']='STRONG_FRESH' if immediate else 'REPEATED'
                qualified.append((conf,hits,dep,f))
        if qualified:
            _,hits,_dep,f=max(qualified)
            self.mobile_confirmed=True; self._last_detection_time=now
            self._candidate_freq=f; self._confirm_streak=hits; self._clear_streak=0
        else:
            self._clear_streak+=1
            self._confirm_streak=max([int(s.get('hits',0))
                                      for s in self._carrier_state.values()],default=0)
            if not self.mobile_confirmed or now-self._last_detection_time>hold:
                self.mobile_confirmed=False
                if not self._carrier_state:self._candidate_freq=None
        self.activity_confidence=new
        return new,self.mobile_confirmed
    def _scan_cycle(self):
        _cycle_t=time.perf_counter(); self.last_scan_windows=0; self.last_site_scan_ms=0.
        try:
            _mobile_t=time.perf_counter()
            m,mf,mp,mfloor=self._scan_band(float(self.cfg.get('mobile_band_start_hz',380e6)),float(self.cfg.get('mobile_band_end_hz',385e6)),'MOBILE')
            self.last_mobile_scan_ms=(time.perf_counter()-_mobile_t)*1000.
            limit=max(0,int(self.cfg.get('max_mobile_candidates_per_sweep',12)))
            self.last_broadband_rejected=False
            self.last_static_rejected=False
            self.last_comb_rejected=False
            self.last_raw_mobile_candidates=len(m)
            # Baseline suppression runs first. Keep diagnostic visibility of
            # what survived it even if the busy-band guard later narrows the set.
            m=self._reject_static_artifacts(m)
            self.last_post_artifact_candidates=len(m)
            dbg=sorted(m,key=lambda q:(float(q.get('temporal_departure',0)),
                                       float(q.get('baseline_departure',0)),
                                       float(q.get('burst_quality',0))),reverse=True)
            self.debug_mobile_candidates=[dict(q) for q in dbg[:12]]
            # A learned local hardware comb can produce matching +10 MHz
            # structures in both scans. Suppress only coherent multi-tooth
            # comb motion; isolated carriers remain eligible.
            m=self._reject_coherent_comb(m)
            m=self._apply_broadband_guard(m,limit)
            # Mobile/uplink scanning stays on the fast path. Downlink context is
            # refreshed periodically and kept in memory; forcing a second 5 MHz
            # sweep for every candidate was adding several seconds of latency.
            interval=max(1,int(self.cfg.get('site_scan_interval',3)))
            refresh_site=(not self.site_recent) or self._idle_site_cycle==0
            self._idle_site_cycle=(self._idle_site_cycle+1)%interval
            if refresh_site:
                _site_t=time.perf_counter()
                site,_,_,_=self._scan_band(float(self.cfg.get('site_band_start_hz',390e6)),float(self.cfg.get('site_band_end_hz',395e6)),'SITE')
                self.last_site_scan_ms=(time.perf_counter()-_site_t)*1000.
            else:
                site=[]
            now=time.time(); self._remember_sites(site,now)
            require=bool(self.cfg.get('require_duplex_pair',True))
            require_now=bool(self.cfg.get('require_current_duplex_pair',False))
            minpair=float(self.cfg.get('duplex_pair_min_quality',.40))
            minconf=float(self.cfg.get('candidate_min_confidence',.52))
            min_novel=float(self.cfg.get('novelty_min_departure',1.25))
            self.last_novelty_rejected=0
            self.last_pair_rejected=0
            self.last_confidence_rejected=0
            accepted=[]
            for x in m:
                dep=float(x.get('temporal_departure',0))
                # This is the central false-positive guard in detector profile
                # v6. A stationary/slowly drifting carrier may have excellent
                # burst and duplex scores, but it cannot alert unless it is a
                # material local change relative to its own recent history.
                if dep<min_novel:
                    self.last_novelty_rejected+=1
                    continue
                pq,pnow,page,phits=self._pair_info(
                    round(x['freq_hz']+10000000),site,now)
                if require and (pq<minpair or (require_now and not pnow)):
                    self.last_pair_rejected+=1
                    continue
                q=dict(x)
                q.update(
                    carrier_key_hz=self._carrier_key(x['freq_hz']),
                    paired=pq>=minpair,paired_now=pnow,pair_quality=pq,
                    pair_age_s=page,pair_hits=phits,
                    novelty_quality=clamp((dep-min_novel)/max(
                        .1,float(self.cfg.get('novelty_strong_departure',2.0))-min_novel)),
                )
                q['confidence']=self._confidence(q,pq)
                if q['confidence']>=minconf:
                    accepted.append(q)
                else:
                    self.last_confidence_rejected+=1
            accepted.sort(key=lambda q:(q['confidence'],q.get('burst_quality',0),q.get('rf_snr_db',0)),reverse=True); shown=accepted[:int(self.cfg.get('max_signals',3))]
            _,confirmed=self._hysteresis(shown,now)
            rf=max([p.get('signal_strength',0) for p in shown],default=0)
            sl=max([p.get('signal_strength',0) for p in site],default=max([v.get('quality',0) for v in self.site_recent.values()],default=0))
            idx=np.linspace(0,len(mp)-1,min(240,len(mp))).astype(int) if len(mp) else np.array([],dtype=int)
            with self.lock:
                self.mobile_peaks=accepted;self.site_peaks=site;self.peaks=shown if confirmed else [];self.mobile_level=rf if confirmed else 0.;self.site_level=sl
                self.spectrum_freqs=mf[idx].astype(np.float64) if len(idx) else np.array([]);self.spectrum_db=mp[idx].astype(np.float32) if len(idx) else np.array([])
                self.noise_floor_db=mfloor;self.last_update=now;self.status='LIVE';self.error='';self.demo_active=False;self.last_good_scan=now;self.scan_failures=0
                self.last_cycle_ms=(time.perf_counter()-_cycle_t)*1000.
            return True
        except Exception as e:
            err=str(e)
            if 'timeout' in err.lower():self._recover_sdr_usb()
            with self.lock:
                self.scan_failures+=1; recent=bool(self.last_good_scan and time.time()-self.last_good_scan<8)
                if not recent and self.scan_failures>=3:
                    self.status='NO SDR';self.peaks=[];self.mobile_peaks=[];self.site_peaks=[];self.mobile_level=0.;self.site_level=0.;self.activity_confidence=0.;self.mobile_confirmed=False
                    self._confirm_streak=0;self._clear_streak=0;self._candidate_freq=None
                    self._carrier_state={};self._last_detection_time=0.
                    self.last_broadband_rejected=False;self.last_static_rejected=False;self.last_comb_rejected=False
                    self.spectrum_freqs=np.array([],dtype=np.float32);self.spectrum_db=np.array([],dtype=np.float32)
                self.error=err;self.demo_active=False
            return False
    def _demo_once(self):
        t=time.time(); vals=[.1+.75*max(0,math.sin(t*.7))**8,.06+.5*max(0,math.sin(t*.44+1.7))**10,.04+.85*max(0,math.sin(t*.28+3.1))**12]
        base=float(self.cfg.get('mobile_band_start_hz',380e6)); span=float(self.cfg.get('mobile_band_end_hz',385e6))-base
        peaks=[{'freq_hz':base+span*(.2+i*.3),'power_db':-80+v*30,'snr_db':8+v*30,'rf_snr_db':8+v*24,'signal_strength':v,'level':v,'confidence':min(1,.2+v*.8),'band':'MOBILE','last_seen':time.time()} for i,v in enumerate(vals) if v>.12]
        x=np.linspace(0,1,220);spec=-105+4*np.sin(x*15+t)
        with self.lock:
            self.peaks=peaks[:3];self.mobile_peaks=peaks[:3];self.site_peaks=[];self.mobile_level=max(vals);self.site_level=.45;self.activity_confidence=max([p['confidence'] for p in peaks],default=0);self.mobile_confirmed=bool(peaks)
            self.spectrum_freqs=np.linspace(base,base+span,len(x));self.spectrum_db=spec.astype(np.float32);self.noise_floor_db=-103.;self.last_update=time.time();self.status='DEMO';self.error='';self.demo_active=True
        time.sleep(.08)
