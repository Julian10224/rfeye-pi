import math, os, shutil, signal, subprocess, threading, time, ctypes, ctypes.util
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
        self._artifact_sweep=0; self._artifact_samples={}; self._artifact_baseline={}
        self.last_broadband_rejected=False; self.last_comb_rejected=False
    def set_demo(self,v):
        v=bool(v)
        with self.lock:
            self._demo_forced=v; self.cfg['demo_mode']=v
            if not v:
                self.demo_active=False; self.peaks=[]; self.mobile_peaks=[]; self.site_peaks=[]
                self.mobile_level=0.; self.site_level=0.; self.activity_confidence=0.; self.mobile_confirmed=False
                self._confirm_streak=0; self._clear_streak=0; self._candidate_freq=None
                self._carrier_state={}; self._last_detection_time=0.
                self._artifact_sweep=0; self._artifact_samples={}; self._artifact_baseline={}
                self.last_broadband_rejected=False; self.last_comb_rejected=False
                self.status='SCANNING'; self.error=''; self.last_update=time.time()
    def start(self):
        if self.running:return
        self.running=True; self.thread=threading.Thread(target=self._run,daemon=True); self.thread.start()
    def stop(self):
        self.running=False
        if self.thread:self.thread.join(timeout=2.)
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
            'broadband_rejected':bool(self.last_broadband_rejected),'comb_rejected':bool(self.last_comb_rejected),
            'static_rejected':bool(self.last_comb_rejected),
            'artifact_calibrating':bool(self._artifact_sweep<int(self.cfg.get('artifact_calibration_sweeps',5))),
            'artifact_sweep':int(self._artifact_sweep),'artifact_baseline_count':len(self._artifact_baseline),
            'sdr_path':str(getattr(self,'sdr_path','?'))}
    def _run(self):
        while self.running:
            with self.lock: demo=self._demo_forced
            if demo:self._demo_once();continue
            if not self._scan_cycle():
                if self.cfg.get('auto_demo_if_no_sdr',False):self._demo_once()
                else:time.sleep(.5)
    def _centers_for(self,a,b,sr):
        usable=sr*.68; half=usable/2; step=usable*.9; out=[]; c=a+half
        while True:
            x=a+round((c-a)/25000.)*25000.
            if out and x<=out[-1]:x=out[-1]+25000.
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
            if proc.returncode!=0 or len(out)<n*2:
                msg=err.decode('utf-8','ignore').strip()[-240:]
                raise RuntimeError(msg or f'rtl_sdr exit {proc.returncode}')
            raw=np.frombuffer(out,dtype=np.uint8).astype(np.float32)
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
    def _scan_band(self,a,b,label):
        sr=int(self.cfg.get('sample_rate',2048000)); n=int(self.cfg.get('fft_size',1024)); base=int(self.cfg.get('fft_blocks',8))
        ms=float(self.cfg.get('mobile_capture_ms' if label=='MOBILE' else 'site_capture_ms',64.)); blocks=max(base,math.ceil(sr*ms/1000/n))
        usable=sr*.68/2; half=float(self.cfg.get('tetra_channel_half_width_hz',9000)); gate=float(self.cfg.get('burst_gate_db',6))
        channels=np.arange(a+12500.,b,25000.,dtype=np.float64); obs={}; fs=[]; ps=[]
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
            gate=float(self.cfg.get('burst_gate_db',6.0))
            dlo=float(self.cfg.get('min_burst_duty',.035)); dhi=float(self.cfg.get('max_burst_duty',.65))
            plo=float(self.cfg.get('preferred_burst_duty_min',.06)); phi=float(self.cfg.get('preferred_burst_duty_max',.45))
            minsnr=float(self.cfg.get('mobile_min_rf_snr_db',5.0))
            for q0 in vals:
                if not dlo<=q0['duty']<=dhi:continue
                q=dict(q0); snr=q['power_db']-ch_floor; span=float(q['burst_span_db'])
                burst_q=clamp((span-gate+2.0)/14.0)
                rf_q=clamp((snr-minsnr+4.0)/16.0)
                if burst_q<=0.0 or rf_q<=0.0:continue
                q.update(rf_snr_db=float(snr),burst_quality=burst_q,
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
        return peaks[:max(32,int(self.cfg.get('max_signals',3))*10)],f,p,floor

    def _reject_static_artifacts(self,peaks):
        # Learn the stationary RF environment during the first few fast sweeps.
        # A fixed receiver/Pi spur is present sweep after sweep with almost the
        # same level, duty and burst span. A new mobile burst is either on a new
        # carrier or changes those metrics enough to escape the baseline.
        self._artifact_sweep+=1
        warm=max(3,int(self.cfg.get('artifact_calibration_sweeps',5)))
        minhits=max(2,int(self.cfg.get('artifact_min_baseline_hits',4)))
        rdelta=max(.5,float(self.cfg.get('artifact_rf_snr_delta_db',5.0)))
        ddelta=max(.02,float(self.cfg.get('artifact_duty_delta',.12)))
        sdelta=max(.5,float(self.cfg.get('artifact_span_delta_db',3.5)))
        self.last_comb_rejected=False

        by_freq={}
        for q in peaks:
            f=int(round(float(q.get('freq_hz',0))/25000.)*25000)
            # If adjacent raster bins describe one broad receiver spur, retain
            # the strongest representative for baseline statistics.
            old=by_freq.get(f)
            if old is None or float(q.get('power_db',-999))>float(old.get('power_db',-999)):
                by_freq[f]=q

        if self._artifact_sweep<=warm:
            for f,q in by_freq.items():
                hist=self._artifact_samples.setdefault(f,[])
                hist.append((float(q.get('power_db',0)),float(q.get('duty',0)),float(q.get('burst_span_db',0)),float(q.get('rf_snr_db',q.get('snr_db',0)))))
                if len(hist)>warm:del hist[:-warm]
            if self._artifact_sweep==warm:
                for f,hist in self._artifact_samples.items():
                    if len(hist)<minhits:continue
                    a=np.asarray(hist,dtype=np.float32)
                    self._artifact_baseline[f]={'power':float(np.median(a[:,0])),
                        'duty':float(np.median(a[:,1])),'span':float(np.median(a[:,2])),
                        'rf_snr':float(np.median(a[:,3])),'hits':len(hist)}
            # Suppress alerts only during the short startup calibration. Normal
            # scan cadence still runs so the baseline costs no extra SDR passes.
            self.last_comb_rejected=bool(peaks)
            return []

        out=[]
        for q in peaks:
            f=int(round(float(q.get('freq_hz',0))/25000.)*25000)
            base=self._artifact_baseline.get(f)
            if base is None:
                out.append(q);continue
            dr=abs(float(q.get('rf_snr_db',q.get('snr_db',0)))-float(base['rf_snr']))
            dd=abs(float(q.get('duty',0))-float(base['duty']))
            ds=abs(float(q.get('burst_span_db',0))-float(base['span']))
            if dr>rdelta or dd>ddelta or ds>sdelta:
                # Significant shape/SNR departure from the stationary baseline:
                # preserve it even if AGC changed the absolute power everywhere.
                q=dict(q);q['baseline_departure']=max(dr/rdelta,dd/ddelta,ds/sdelta)
                out.append(q);continue
            self.last_comb_rejected=True
            # Slowly follow harmless thermal/AGC drift without learning a new
            # transmitter into the baseline.
            base['power']=float(base['power']*.98+float(q.get('power_db',0))*.02)
            base['duty']=float(base['duty']*.98+float(q.get('duty',0))*.02)
            base['span']=float(base['span']*.98+float(q.get('burst_span_db',0))*.02)
            base['rf_snr']=float(base['rf_snr']*.98+float(q.get('rf_snr_db',q.get('snr_db',0)))*.02)
        return out

    def _remember_sites(self,site,now):
        age=float(self.cfg.get('site_pair_memory_s',4))
        for q in site:
            f=round(q['freq_hz']); old=self.site_recent.get(f,{}); hits=int(old.get('hits',0))+1 if now-float(old.get('time',0))<=age*1.5 else 1
            self.site_recent[f]={'time':now,'hits':min(hits,8),'quality':float(q.get('site_quality',q.get('level',0)))}
        self.site_recent={f:v for f,v in self.site_recent.items() if now-float(v.get('time',0))<=age}
    def _pair_q(self,target,site,now):
        tol=float(self.cfg.get('duplex_pair_tolerance_hz',1000)); age=max(.1,float(self.cfg.get('site_pair_memory_s',4))); hits=int(self.cfg.get('site_pair_min_hits',1)); best=0.; current=False
        for s in site:
            if abs(s['freq_hz']-target)<=tol:best=max(best,float(s.get('site_quality',s.get('level',0))));current=True
        for f,m in self.site_recent.items():
            if abs(f-target)<=tol and int(m.get('hits',0))>=hits:
                best=max(best,float(m.get('quality',0))*clamp(1-(now-float(m.get('time',0)))/age))
        return clamp(best),current
    def _confidence(self,m,pair):return clamp(.42*m.get('burst_quality',0)+.16*m.get('duty_quality',0)+.14*m.get('rf_quality',0)+.28*pair)
    def _hysteresis(self,candidates,now):
        # Track evidence per 25 kHz carrier instead of tying the whole alert to
        # whichever carrier happens to rank first in one sweep. This prevents
        # ranking jitter between several active carriers from resetting a valid
        # detection every cycle.
        memory=max(.5,float(self.cfg.get('carrier_memory_s',2.5)))
        window=max(.5,float(self.cfg.get('confirm_window_s',2.2)))
        hold=max(0.,float(self.cfg.get('alert_hold_s',3.0)))
        strong=float(self.cfg.get('strong_hit_confidence',.72))
        minconf=float(self.cfg.get('candidate_min_confidence',.48))
        need=max(1,int(self.cfg.get('confirm_hits',2)))

        for q in candidates:
            f=int(round(float(q['freq_hz'])/25000.)*25000)
            conf=float(q.get('confidence',0))
            st=self._carrier_state.get(f)
            if st is None or now-float(st.get('last',0))>window:
                st={'hits':0,'last':0.,'score':0.}
            st['hits']=int(st.get('hits',0))+1; st['last']=now
            st['score']=max(conf,float(st.get('score',0))*.55+conf*.45)
            self._carrier_state[f]=st

        self._carrier_state={f:s for f,s in self._carrier_state.items() if now-float(s.get('last',0))<=memory}
        raw=max([float(q.get('confidence',0)) for q in candidates],default=0.)
        old=float(self.activity_confidence)
        a=float(self.cfg.get('confidence_attack',.58) if raw>=old else self.cfg.get('confidence_release',.20))
        new=clamp(old+clamp(a)*(raw-old))

        qualified=[]
        for q in candidates:
            f=int(round(float(q['freq_hz'])/25000.)*25000); st=self._carrier_state.get(f,{})
            conf=float(q.get('confidence',0)); hits=int(st.get('hits',0))
            if conf>=strong or (hits>=need and conf>=minconf):
                qualified.append((conf,hits,f))
        if qualified:
            _,hits,f=max(qualified)
            self.mobile_confirmed=True; self._last_detection_time=now
            self._candidate_freq=f; self._confirm_streak=hits; self._clear_streak=0
        else:
            self._clear_streak+=1
            self._confirm_streak=max([int(s.get('hits',0)) for s in self._carrier_state.values()],default=0)
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
            self.last_comb_rejected=False
            # Baseline suppression must run before the broadband guard. With
            # fully soft scoring the raw candidate set intentionally contains
            # weak carriers too; persistent stationary ones are removed here.
            m=self._reject_static_artifacts(m)
            if limit and len(m)>limit:
                self.last_broadband_rejected=True
                # A large set of *new* simultaneous carriers is more consistent
                # with display/USB broadband interference than a nearby mobile.
                m=[]
            # Mobile/uplink scanning stays on the fast path. Downlink context is
            # refreshed periodically and kept in memory; forcing a second 5 MHz
            # sweep for every candidate was adding several seconds of latency.
            interval=max(1,int(self.cfg.get('site_scan_interval',6)))
            refresh_site=(not self.site_recent) or self._idle_site_cycle==0
            self._idle_site_cycle=(self._idle_site_cycle+1)%interval
            if refresh_site:
                _site_t=time.perf_counter()
                site,_,_,_=self._scan_band(float(self.cfg.get('site_band_start_hz',390e6)),float(self.cfg.get('site_band_end_hz',395e6)),'SITE')
                self.last_site_scan_ms=(time.perf_counter()-_site_t)*1000.
            else:
                site=[]
            now=time.time(); self._remember_sites(site,now)
            require=bool(self.cfg.get('require_duplex_pair',True)); require_now=bool(self.cfg.get('require_current_duplex_pair',False)); minpair=float(self.cfg.get('duplex_pair_min_quality',.28)); minconf=float(self.cfg.get('candidate_min_confidence',.48)); accepted=[]
            for x in m:
                pq,pnow=self._pair_q(round(x['freq_hz']+10000000),site,now)
                if require and (pq<minpair or (require_now and not pnow)):continue
                q=dict(x);q.update(paired=pq>=minpair,paired_now=pnow,pair_quality=pq,confidence=self._confidence(x,pq))
                if q['confidence']>=minconf:accepted.append(q)
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
                    self._carrier_state={};self._last_detection_time=0.;self.last_broadband_rejected=False
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
