"""RF Eye SDR backend -- detector profile v8.

What changed and why
--------------------
Profiles up to v7 alerted on wideband energy statistics: channel power, duty
cycle, burst span, and how much those had moved since the previous sweep.
Those features cannot distinguish a C2000 handset from any other bursty RF,
so no threshold on them was ever going to work.  Field recordings showed the
consequence plainly: the duplex-pair gate passed 100% of the 390-395 MHz
channels it looked at, the novelty gate rejected nothing during busy sweeps,
and alerts landed on three neighbouring 25 kHz raster points at once -- which
one TETRA carrier physically cannot do.

v8 replaces that with a two-stage design:

  1. Lock the network.  Verify base station downlink carriers in 390-395 MHz
     against the real ETSI EN 300 392-2 waveform (see ``tetra_phy``).  Base
     stations transmit continuously, so they can be verified thoroughly and
     repeatedly.  No verified base station means no C2000 coverage here, and
     the device stays silent instead of guessing.

  2. Watch the partners.  TETRA duplex spacing in this band is 10 MHz, so
     each verified downlink names exactly one uplink channel where handsets on
     that site transmit.  Those channels are dwelled on and verified with the
     same physical-layer tests.  Only a verified TETRA uplink transmission
     raises an alert.

Every alert therefore rests on demodulated evidence -- pi/4-DQPSK at exactly
18000 symbols/s, a 25 kHz RRC channel shape, and 14.1667 ms TDMA slot timing
-- rather than on a level crossing a threshold.

The artefact baseline, coherent-comb rejector, temporal novelty gate and
duplex-pair scoring of v6/v7 are gone.  They were all attempts to subtract
false positives after the fact, and with a real waveform test they are not
only unnecessary but harmful: each one could also suppress a genuine
detection.
"""
import math, shutil, subprocess, threading, time, ctypes, ctypes.util
import numpy as np

import tetra_phy
from tetra_phy import Channelizer, PhyResult, clamp
from tetra_detector import SiteRegistry, UplinkAlarm, plan_dwell, raster_snap

try:
    from rtlsdr import RtlSdr
except Exception:
    RtlSdr = None


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
        self.rate_changed=False

    def _check(self, rc, what):
        if rc != 0: raise RuntimeError(f'{what} failed {rc}')

    def configure(self, sr, ppm, gain):
        self.rate_changed=False
        if self.sample_rate != int(sr):
            self._check(self.lib.rtlsdr_set_sample_rate(self.dev,int(sr)),'set_sample_rate')
            self.sample_rate=int(sr); self.rate_changed=True
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

    def read_complex(self, count, chunk=131072, abort=None):
        """Read ``count`` complex samples in USB-sized chunks.

        A v8 verification dwell is a quarter of a million samples, far more
        than one bulk transfer should carry.  Chunking also gives the scan
        thread somewhere to notice a shutdown request, instead of sitting
        inside one very long blocking read.
        """
        remaining=int(count)
        parts=[]
        buf=(ctypes.c_ubyte*(int(chunk)*2))()
        got=ctypes.c_int()
        while remaining>0:
            if abort is not None and abort():
                raise RuntimeError('capture aborted')
            take=min(remaining,int(chunk))
            nbytes=take*2
            self._check(self.lib.rtlsdr_read_sync(self.dev,buf,nbytes,ctypes.byref(got)),'read_sync')
            if got.value < nbytes:
                raise RuntimeError(f'short read {got.value}/{nbytes}')
            raw=np.ctypeslib.as_array(buf)[:nbytes].astype(np.float32)
            parts.append(((raw[0::2]-127.5)+1j*(raw[1::2]-127.5)).astype(np.complex64))
            remaining-=take
        return parts[0] if len(parts)==1 else np.concatenate(parts)

    def close(self):
        if self.dev:
            try: self.lib.rtlsdr_close(self.dev)
            finally: self.dev=None


class SDRBackend:
    """Passive C2000 activity monitor. No identity, content or distance."""

    def __init__(self,cfg):
        self.cfg=cfg; self.lock=threading.Lock(); self.running=False; self.thread=None
        self.status='STARTING'; self.error=''
        self.peaks=[]; self.mobile_peaks=[]; self.site_peaks=[]
        self.mobile_level=0.; self.site_level=0.
        self.activity_confidence=0.; self.mobile_confirmed=False
        self.spectrum_freqs=np.array([],dtype=np.float64)
        self.spectrum_db=np.array([],dtype=np.float32)
        self.noise_floor_db=-100.; self.last_update=0.; self.demo_active=False
        self._demo_forced=bool(cfg.get('demo_mode',False))
        self.last_good_scan=0.; self.scan_failures=0; self.last_usb_reset=0.
        self.sdr=None; self.sdr_path='UNOPENED'
        self.last_cycle_ms=0.; self.last_survey_ms=0.; self.last_dwell_ms=0.
        self.last_verify_ms=0.; self.last_capture_ms=0.; self.last_scan_windows=0

        self.sites=SiteRegistry(cfg)
        self.alarm=UplinkAlarm(cfg)
        self.detector_state='SEARCHING'
        self.survey_shortlist=[]
        self._survey_at=0.; self._survey_idx=0
        self._site_verify_at=0.; self._watch_idx=0; self._display_cycle=0
        self.last_phy=[]
        self.dwell_centre_hz=0.; self.dwell_channels=[]
        self.last_dwell_role=''
        self.last_iq=None; self.last_iq_meta={}

    # -- lifecycle ---------------------------------------------------------
    def set_demo(self,v):
        v=bool(v)
        with self.lock:
            self._demo_forced=v; self.cfg['demo_mode']=v
            if not v:
                self.peaks=[]; self.mobile_peaks=[]; self.site_peaks=[]
                self.mobile_level=0.; self.site_level=0.
                self.activity_confidence=0.; self.mobile_confirmed=False
                self.demo_active=False; self.alarm.reset()
                self.detector_state='SEARCHING'; self.last_phy=[]
                self.survey_shortlist=[]; self._survey_at=0.
                self.status='SCANNING'; self.error=''; self.last_update=time.time()

    def start(self):
        if self.running:return
        self.running=True
        self.thread=threading.Thread(target=self._run,daemon=True); self.thread.start()

    def stop(self):
        self.running=False
        t=self.thread
        # Never close a librtlsdr handle from the UI/service thread while the
        # scan thread may still be inside rtlsdr_read_sync(). That race can
        # wedge the RTL-SDR Blog V4 on the USB bus during restart/shutdown.
        if t and t is not threading.current_thread():
            t.join(timeout=max(3.0,float(self.cfg.get('sdr_stop_join_s',8.0))))
        if not t or not t.is_alive():
            self._close_direct_sdr()
        try: self.sites.save()
        except Exception: pass

    def _run(self):
        try:
            while self.running:
                with self.lock: demo=self._demo_forced
                if demo:
                    self._demo_once(); continue
                if not self._scan_cycle():
                    if self.cfg.get('auto_demo_if_no_sdr',False): self._demo_once()
                    else: time.sleep(.5)
        finally:
            # Close from the same worker that performs synchronous USB reads.
            self._close_direct_sdr()

    def snapshot(self):
        with self.lock:
            return {
                'status':self.status,'error':self.error,
                'detector_state':self.detector_state,
                'peaks':[dict(p) for p in self.peaks],
                'mobile_peaks':[dict(p) for p in self.mobile_peaks],
                'site_peaks':[dict(p) for p in self.site_peaks],
                'mobile_level':float(self.mobile_level),
                'site_level':float(self.site_level),
                'activity_confidence':float(self.activity_confidence),
                'mobile_confirmed':bool(self.mobile_confirmed),
                'freqs':self.spectrum_freqs.copy(),'spectrum':self.spectrum_db.copy(),
                'noise':float(self.noise_floor_db),
                'last_update':float(self.last_update),'demo':bool(self.demo_active),
                'network_locked':bool(self.site_peaks),
                'site_locked_count':len(self.site_peaks),
                'site_candidate_count':len(self.sites.candidates()),
                'site_state_loaded':bool(self.sites.loaded_from_disk),
                'watch_freqs':[float(f) for f in self.dwell_channels],
                'dwell_centre_hz':float(self.dwell_centre_hz),
                'dwell_role':str(self.last_dwell_role),
                'survey_shortlist':[float(f) for f in self.survey_shortlist],
                'phy':[dict(p) for p in self.last_phy],
                'confirm_streak':int(self.alarm.streak),
                'clear_streak':0 if self.mobile_confirmed else 1,
                'cycle_ms':float(self.last_cycle_ms),
                'survey_ms':float(self.last_survey_ms),
                'dwell_ms':float(self.last_dwell_ms),
                'verify_ms':float(self.last_verify_ms),
                'capture_ms':float(self.last_capture_ms),
                # Old key names kept so existing debug and recording consumers
                # keep working across the profile change.
                'mobile_scan_ms':float(self.last_dwell_ms),
                'site_scan_ms':float(self.last_survey_ms),
                'scan_windows':int(self.last_scan_windows),
                'sdr_path':str(self.sdr_path),
            }

    # -- SDR plumbing ------------------------------------------------------
    def _recover_sdr_usb(self):
        now=time.time()
        if now-self.last_usb_reset<5: return False
        self.last_usb_reset=now; self._close_direct_sdr()
        exe=shutil.which('usbreset')
        if not exe: return False
        try:
            cp=subprocess.run([exe,'0bda:2838'],capture_output=True,text=True,timeout=6)
            if cp.returncode==0:
                time.sleep(1.5); return True
        except Exception: pass
        return False

    def _close_direct_sdr(self):
        dev=self.sdr
        self.sdr=None; self.sdr_path='UNOPENED'
        if dev is not None:
            try: dev.close()
            except Exception: pass

    def _samples(self,center,sr,count):
        """Tune and read, settling for the PLL and any sample-rate change."""
        try:
            if self.sdr is None:
                self.sdr=_PersistentRTL(int(self.cfg.get('sdr_device_index',0)))
                self.sdr_path='CTYPES PERSISTENT'
            self.sdr.configure(int(sr),int(self.cfg.get('ppm',0)),self.cfg.get('gain','auto'))
            self.sdr.tune(int(center))
            self.sdr.reset()
            # A sample-rate change reprograms the tuner's IF filter as well as
            # the decimator, so it needs noticeably longer to settle than a
            # plain retune. Reading unsettled samples into a 0.9 s dwell would
            # corrupt the very timing measurements the dwell exists for.
            settle=0.030 if self.sdr.rate_changed else 0.006
            self.sdr.read_complex(max(4096,int(sr*settle)),
                                  abort=lambda: not self.running)
            return self.sdr.read_complex(int(count),abort=lambda: not self.running)
        except Exception as e:
            self._close_direct_sdr()
            raise RuntimeError('librtlsdr read failed: '+str(e))

    def _capture_spectrum(self,center,sr,n,blocks,percentile=None):
        """Wideband survey capture; returns (freqs, power_db)."""
        t0=time.perf_counter()
        iq=self._samples(center,sr,n*blocks)
        self.last_capture_ms=(time.perf_counter()-t0)*1000.
        rows=min(blocks,len(iq)//n)
        if rows<1: raise RuntimeError('no complete FFT blocks')
        m=np.asarray(iq[:rows*n],dtype=np.complex64).reshape(rows,n).copy()
        m-=np.mean(m,axis=1,keepdims=True)
        win=np.hanning(n).astype(np.float32)
        stack=20*np.log10(np.abs(np.fft.fftshift(np.fft.fft(m*win,axis=1),axes=1))+1e-12)
        psd=np.mean(stack,axis=0) if percentile is None else np.percentile(
            stack,float(percentile),axis=0)
        # Blank the tuner DC spike so it cannot masquerade as a carrier.
        mid=len(psd)//2; lo=max(0,mid-2); hi=min(len(psd),mid+3)
        if lo>0 and hi<len(psd):
            side=np.r_[psd[max(0,lo-8):lo],psd[hi:min(len(psd),hi+8)]]
            psd[lo:hi]=float(np.median(side))
        return np.fft.fftshift(np.fft.fftfreq(n,1./sr))+center,psd

    def _sweep(self,a,b,percentile=None):
        """Sweep a band at the wide sample rate; returns (freqs, power_db)."""
        sr=int(self.cfg.get('sample_rate',2048000)); n=int(self.cfg.get('fft_size',1024))
        ms=float(self.cfg.get('survey_capture_ms',48.0))
        blocks=max(8,int(math.ceil(sr*ms/1000/n)))
        usable=sr*.68/2; step=sr*.68*.9
        fs=[]; ps=[]; c=a+usable
        while True:
            f,p=self._capture_spectrum(c,sr,n,blocks,percentile)
            self.last_scan_windows+=1
            m=(f>=a)&(f<=b)&(np.abs(f-c)<=usable)
            if np.any(m): fs.append(f[m]); ps.append(p[m])
            if c+usable>=b: break
            c+=step
        if not fs: return np.array([]),np.array([])
        f=np.concatenate(fs); p=np.concatenate(ps)
        o=np.argsort(f)
        return f[o],p[o]

    # -- stage 1: find downlink carriers worth verifying -------------------
    def _survey_downlink(self):
        """Shortlist 390-395 MHz raster channels carrying steady energy.

        This is only a shortlist.  It is deliberately loose: its job is to keep
        the expensive verification dwells pointed somewhere plausible, never to
        decide anything.  Everything it proposes still has to pass the full
        waveform test before it counts as a C2000 base station.
        """
        a=float(self.cfg.get('site_band_start_hz',390e6))
        b=float(self.cfg.get('site_band_end_hz',395e6))
        f,p=self._sweep(a,b)
        if not len(f): return []
        spacing=float(self.cfg.get('tetra_channel_spacing_hz',25000.))
        half=float(self.cfg.get('tetra_channel_half_width_hz',9000.))
        chans=np.arange(a+float(self.cfg.get('tetra_raster_offset_hz',12500.)),b,spacing)
        levels=[]
        for ch in chans:
            m=np.abs(f-ch)<=half
            if int(np.count_nonzero(m))>=3:
                levels.append((float(ch),float(np.median(p[m]))))
        if not levels: return []
        floor=float(np.percentile(np.array([v for _,v in levels]),30))
        minsnr=float(self.cfg.get('survey_min_snr_db',6.0))
        keep=[(c,v) for c,v in levels if v-floor>=minsnr]
        keep.sort(key=lambda x:x[1],reverse=True)
        return [c for c,_ in keep[:max(1,int(self.cfg.get('survey_max_candidates',12)))]]

    # -- stage 2: verify a group of channels in one narrowband dwell -------
    def _verify(self,freqs,role):
        """Dwell on ``freqs`` and run the full TETRA test on each.

        All channels of one dwell share a single capture and a single forward
        FFT, so watching a whole site's uplink list costs barely more than
        watching one channel of it.
        """
        sr=int(self.cfg.get('phy_sample_rate',288000))
        n=1<<int(self.cfg.get('phy_dwell_log2',18))
        centre,members=plan_dwell(
            freqs,sr,
            max_offset_hz=float(self.cfg.get('phy_max_offset_hz',100000.)),
            spacing_hz=float(self.cfg.get('tetra_channel_spacing_hz',25000.)))
        if centre is None or not members:
            return []
        t0=time.perf_counter()
        iq=self._samples(centre,sr,n)
        self.last_dwell_ms=(time.perf_counter()-t0)*1000.
        self.last_capture_ms=self.last_dwell_ms
        self.last_scan_windows+=1

        t1=time.perf_counter()
        ch=Channelizer(iq,sr)
        out=[]
        for freq,offset in members:
            try:
                out.append(tetra_phy.analyse(
                    ch,offset,role=role,limits=self.cfg,freq_hz=freq,
                    decim=int(self.cfg.get('phy_decimation',8)),
                    timing_phases=int(self.cfg.get('phy_timing_phases',8))))
            except Exception as e:
                r=PhyResult(freq_hz=float(freq),role=role)
                r.reason='ERROR:'+str(e)[:60]
                out.append(r)
        self.last_verify_ms=(time.perf_counter()-t1)*1000.
        self.dwell_centre_hz=float(centre)
        self.dwell_channels=[f for f,_ in members]
        self.last_dwell_role=role
        if bool(self.cfg.get('keep_last_iq',True)):
            # Held so a confirmed alert can be written out as raw IQ and
            # re-checked offline. This is the evidence trail that recordings
            # of derived statistics could never provide.
            self.last_iq=iq
            self.last_iq_meta={'centre_hz':float(centre),'sample_rate':int(sr),
                               'samples':int(len(iq)),'role':role,
                               'captured_at':time.time()}
        return out

    def _raster(self,freq,band_start):
        return float(raster_snap(
            freq,band_start,
            float(self.cfg.get('tetra_raster_offset_hz',12500.)),
            float(self.cfg.get('tetra_channel_spacing_hz',25000.))))

    # -- orchestration -----------------------------------------------------
    def _site_work(self,now):
        """Acquire a C2000 network lock, or keep an existing one fresh."""
        band=float(self.cfg.get('site_band_start_hz',390e6))
        # While nothing is locked the device has no way to detect anything at
        # all, so it retries the survey noticeably sooner than it refreshes an
        # established lock.
        interval=(max(5.,float(self.cfg.get('survey_interval_s',60.0)))
                  if self.sites.locked(now)
                  else max(5.,float(self.cfg.get('survey_idle_interval_s',15.0))))
        pending=self.sites.candidates(now)
        if not self.survey_shortlist and not pending:
            if not self._survey_at or now-self._survey_at>=interval:
                t0=time.perf_counter()
                self.survey_shortlist=self._survey_downlink()
                self.last_survey_ms=(time.perf_counter()-t0)*1000.
                self._survey_at=now

        locked=self.sites.locked(now)
        targets=[c['freq_hz'] for c in pending]+list(self.survey_shortlist)
        if locked:
            # Re-prove locked carriers in rotation, so moving out of coverage
            # eventually drops the lock instead of leaving a stale one.
            targets=[locked[self._survey_idx%len(locked)]['freq_hz']]+targets
        if not targets:
            return []

        seen=set(); ordered=[]
        for f in targets:
            k=int(round(self._raster(f,band)))
            if k not in seen:
                seen.add(k); ordered.append(float(k))
        start=self._survey_idx%len(ordered)
        batch=ordered[start:]+ordered[:start]
        self._survey_idx=(self._survey_idx+1)%len(ordered)

        results=self._verify(batch,'DOWNLINK')
        for r in results:
            self.sites.observe(r.freq_hz,r.ok,r.quality,now)
        done={int(round(r.freq_hz)) for r in results}
        self.survey_shortlist=[f for f in self.survey_shortlist
                               if int(round(self._raster(f,band))) not in done]
        self.sites.save()
        return results

    def _watch_work(self,now):
        """Dwell on the uplink partners of the locked base stations."""
        partners=sorted(self.sites.uplink_partners(now))
        if not partners:
            return []
        start=self._watch_idx%len(partners)
        rotated=partners[start:]+partners[:start]
        results=self._verify(rotated,'UPLINK')
        covered=max(1,len(self.dwell_channels))
        self._watch_idx=(self._watch_idx+covered)%len(partners)
        return results

    def _scan_cycle(self):
        t0=time.perf_counter(); self.last_scan_windows=0
        try:
            now=time.time()
            site_results=[]; watch_results=[]
            self.dwell_channels=[]
            if self.sites.locked(now):
                watch_results=self._watch_work(now)
                # Site upkeep runs on its own slow schedule so it never gets in
                # the way of the uplink watch.
                if now-self._site_verify_at>=max(30.,float(
                        self.cfg.get('site_reverify_s',300.0))):
                    site_results=self._site_work(now)
                    self._site_verify_at=now
            else:
                site_results=self._site_work(now)

            now=time.time()
            verified=[r for r in watch_results if r.ok]
            confirmed,level,peaks=self.alarm.update(verified,now)
            locked=self.sites.locked(now)

            # Display spectrum. The 380-385 MHz view is what the user expects
            # to see, but sweeping it is pure display cost, so it runs on its
            # own slow schedule and never gates a detection.
            every=max(1,int(self.cfg.get('display_sweep_interval',4)))
            self._display_cycle=(self._display_cycle+1)%every
            sf=sd=None
            if self._display_cycle==0 or not len(self.spectrum_freqs):
                t1=time.perf_counter()
                sf,sd=self._sweep(float(self.cfg.get('mobile_band_start_hz',380e6)),
                                  float(self.cfg.get('mobile_band_end_hz',385e6)),
                                  percentile=float(self.cfg.get('mobile_percentile',95.)))
                self.last_survey_ms=(time.perf_counter()-t1)*1000.

            # With no lock and an exhausted shortlist there is nothing to
            # verify until the next survey is due. Without this the scan
            # thread spins at full speed doing no work, which is what the
            # first live run on real hardware showed it doing.
            if not site_results and not watch_results:
                wait=max(0.25,min(2.0,float(self.cfg.get(
                    'survey_idle_interval_s',15.0))-(now-self._survey_at)))
                time.sleep(wait)

            state='ALERT' if confirmed else ('LOCKED' if locked else 'SEARCHING')
            shown=[self._peak_row(r) for r in peaks[:int(self.cfg.get('max_signals',3))]]
            phy_rows=[r.as_dict() for r in (watch_results+site_results)[:12]]

            with self.lock:
                self.detector_state=state
                self.peaks=shown if confirmed else []
                self.mobile_peaks=[self._peak_row(r) for r in verified]
                self.site_peaks=[dict(s) for s in locked]
                self.mobile_level=float(level) if confirmed else 0.
                self.site_level=float(max([s['quality'] for s in locked],default=0.))
                self.activity_confidence=float(level)
                self.mobile_confirmed=bool(confirmed)
                self.last_phy=phy_rows
                if sf is not None and len(sf):
                    idx=np.linspace(0,len(sf)-1,min(240,len(sf))).astype(int)
                    self.spectrum_freqs=sf[idx].astype(np.float64)
                    self.spectrum_db=sd[idx].astype(np.float32)
                    self.noise_floor_db=float(np.percentile(sd,40))
                self.last_update=now; self.status='LIVE'; self.error=''
                self.demo_active=False; self.last_good_scan=now; self.scan_failures=0
                self.last_cycle_ms=(time.perf_counter()-t0)*1000.
            return True
        except Exception as e:
            err=str(e)
            if 'timeout' in err.lower() or 'read failed' in err.lower():
                self._recover_sdr_usb()
            with self.lock:
                self.scan_failures+=1
                recent=bool(self.last_good_scan and time.time()-self.last_good_scan<8)
                if not recent and self.scan_failures>=3:
                    self.status='NO SDR'; self.detector_state='NO SDR'
                    self.peaks=[]; self.mobile_peaks=[]; self.site_peaks=[]
                    self.mobile_level=0.; self.site_level=0.
                    self.activity_confidence=0.; self.mobile_confirmed=False
                    self.alarm.reset()
                    self.spectrum_freqs=np.array([],dtype=np.float64)
                    self.spectrum_db=np.array([],dtype=np.float32)
                self.error=err; self.demo_active=False
            return False

    @staticmethod
    def _peak_row(r):
        """PhyResult -> the peak dict the UI and recordings consume."""
        return {'freq_hz':float(r.freq_hz),'level':float(r.quality),
                'signal_strength':float(r.quality),'confidence':float(r.quality),
                'quality':float(r.quality),'snr_db':float(r.snr_db),
                'rf_snr_db':float(r.snr_db),'duty':float(r.duty),
                'band':'MOBILE' if r.role=='UPLINK' else 'SITE',
                'role':str(r.role),'reason':str(r.reason),
                'dqpsk_m':float(r.dqpsk_m),
                'dqpsk_phase_spread':float(r.dqpsk_phase_spread),
                'dqpsk_selectivity':float(r.dqpsk_selectivity),
                'frame_line_ratio':float(r.frame_line_ratio),
                'slot_quantisation':float(r.slot_quantisation),
                'burst_ms_median':float(r.burst_ms_median),
                'occupied_bw_hz':float(r.occupied_bw_hz),
                'boundary_reject_db':float(r.boundary_reject_db),
                'last_seen':time.time()}

    def _demo_once(self):
        t=time.time()
        vals=[.1+.75*max(0,math.sin(t*.7))**8,.06+.5*max(0,math.sin(t*.44+1.7))**10,
              .04+.85*max(0,math.sin(t*.28+3.1))**12]
        base=float(self.cfg.get('mobile_band_start_hz',380e6))
        span=float(self.cfg.get('mobile_band_end_hz',385e6))-base
        peaks=[{'freq_hz':base+span*(.2+i*.3),'snr_db':8+v*30,'rf_snr_db':8+v*24,
                'signal_strength':v,'level':v,'quality':v,'confidence':min(1,.2+v*.8),
                'band':'MOBILE','role':'UPLINK','reason':'DEMO','last_seen':t}
               for i,v in enumerate(vals) if v>.12]
        x=np.linspace(0,1,220); spec=-105+4*np.sin(x*15+t)
        with self.lock:
            self.peaks=peaks[:3]; self.mobile_peaks=peaks[:3]
            self.site_peaks=[{'freq_hz':base+10e6+span*.5,'quality':.45,'hits':3}]
            self.mobile_level=max(vals); self.site_level=.45
            self.activity_confidence=max([p['confidence'] for p in peaks],default=0)
            self.mobile_confirmed=bool(peaks)
            self.detector_state='DEMO'
            self.spectrum_freqs=np.linspace(base,base+span,len(x))
            self.spectrum_db=spec.astype(np.float32)
            self.noise_floor_db=-103.; self.last_update=time.time()
            self.status='DEMO'; self.error=''; self.demo_active=True
        time.sleep(.08)
