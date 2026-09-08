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
from pathlib import Path

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
        self._usb_resets=0; self._usb_backoff=0.
        self._power_checked=0.; self._power_flag=''
        self._power_history=''; self._power_detail=''
        self.sdr=None; self.sdr_path='UNOPENED'
        self.last_cycle_ms=0.; self.last_survey_ms=0.; self.last_dwell_ms=0.
        self.last_verify_ms=0.; self.last_capture_ms=0.; self.last_scan_windows=0

        self.sites=SiteRegistry(cfg)
        self.alarm=UplinkAlarm(cfg)
        self.detector_state='SEARCHING'
        self.survey_shortlist=[]
        self._survey_at=0.; self._survey_idx=0
        self._site_queue=[]; self._sweep_cursor=0.; self._alt_cycle=False
        self._survey_scores=[]
        self._pass_best=None; self._pass_started=0.; self._pass_index=0
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
                self._site_queue=[]; self._sweep_cursor=0.
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

    def reinit_usb(self):
        """Drop the SDR handle and look for the device again, from scratch.

        The useful moment for this is right after someone has changed
        something physical -- a cable, a port, the supply -- and wants the
        appliance to look again without a reboot. It closes the librtlsdr
        handle, clears the failure counters and back-off that would otherwise
        make the next attempt wait, and resets the device over USB if it is
        actually on the bus. A device that is not enumerated cannot be reset
        by anyone, so that case is reported rather than papered over.
        """
        self._close_direct_sdr()
        self._usb_resets = 0
        self._usb_backoff = 0.
        self.last_usb_reset = 0.
        self._power_checked = 0.
        present = self._sdr_present()
        did_reset = self._recover_sdr_usb() if present else False
        with self.lock:
            self.scan_failures = 0
            self.error = '' if present else 'SDR not on the USB bus'
        return {'present': bool(present), 'reset': bool(did_reset)}

    def _low_power_pause(self):
        """Idle between cycles so the CPU can clock back down.

        Without this the detector hands the governor a continuous FFT load and
        a Pi 3 B+ never leaves 1.4 GHz. The pause costs time to lock and buys
        supply headroom, which on a marginal 5 V rail is what decides whether
        the RTL-SDR stays on the bus at all.
        """
        if not bool(self.cfg.get('low_power_mode', False)):
            return
        pause = max(0., float(self.cfg.get('low_power_scan_pause_s', 1.5)))
        end = time.time() + pause
        while self.running and time.time() < end:
            time.sleep(min(0.25, max(0.01, end - time.time())))

    def _run(self):
        try:
            while self.running:
                with self.lock: demo=self._demo_forced
                if demo:
                    self._demo_once(); continue
                if not self._scan_cycle():
                    if self.cfg.get('auto_demo_if_no_sdr',False): self._demo_once()
                    else: time.sleep(.5)
                self._low_power_pause()
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
                'search_best':dict(self._pass_best) if self._pass_best else {},
                'site_queue_remaining':len(self._site_queue),
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
                'power_warning':str(self._power_flag),
                'power_history':str(self._power_history),
                'power_detail':str(self._power_detail),
            }

    # -- SDR plumbing ------------------------------------------------------
    def _sdr_present(self):
        """Is the RTL-SDR actually enumerated on the USB bus right now?"""
        try:
            for vid in Path('/sys/bus/usb/devices').glob('*/idVendor'):
                if vid.read_text().strip().lower() != '0bda':
                    continue
                pid = vid.parent / 'idProduct'
                if pid.exists() and pid.read_text().strip().lower() == '2838':
                    return True
        except Exception:
            # If the USB tree cannot be read, do not let this block recovery.
            return True
        return False

    def _power_warning(self):
        """Report a Pi supply problem, cached because vcgencmd is not free.

        This exists because a sagging 5 V rail and a broken SDR look identical
        on screen but need completely different fixes. On the reference unit an
        under-voltage dip dropped the dongle off the USB bus entirely, and
        without this the display simply said the SDR was not connected.

        Only bit 0 -- "the 5 V rail is below about 4.63 V *right now*" -- is
        reported as a warning. Bit 16 says it happened at some point since
        boot and never clears again, so driving the display from it meant a
        single dip during power-up left the warning on screen for the rest of
        the session, over the top of a perfectly healthy scan. That history
        is still worth keeping, but it belongs on the debug page.
        """
        now=time.time()
        if now-self._power_checked<10.0:
            return self._power_flag
        self._power_checked=now
        self._power_flag=''; self._power_history=''; self._power_detail=''
        exe=shutil.which('vcgencmd')
        if not exe:
            return ''
        try:
            cp=subprocess.run([exe,'get_throttled'],capture_output=True,
                              text=True,timeout=3)
            word=cp.stdout.strip().split('=')[-1]
            bits=int(word,16)
            if bits & 0x1:
                self._power_flag='UNDER-VOLTAGE'
            if bits & 0x10000:
                self._power_history='UNDER-VOLTAGE EARLIER'
            # A Pi 3 has no ADC on the 5 V input, so the only voltage it can
            # actually measure is the SoC core rail. Report it as what it is
            # rather than dressing it up as a supply reading: the number that
            # matters, 4.63 V, is a threshold the firmware compares against
            # and never hands out.
            core=''
            try:
                cv=subprocess.run([exe,'measure_volts','core'],
                                  capture_output=True,text=True,timeout=3)
                core=cv.stdout.strip().split('=')[-1]
            except Exception:
                pass
            bits_txt='throttled '+word
            self._power_detail=(('core %s, ' % core) if core else '')+bits_txt
        except Exception:
            pass
        return self._power_flag

    def _recover_sdr_usb(self):
        """Force-re-enumerate a wedged RTL-SDR -- and only a wedged one.

        A USB reset is a blunt instrument, and using it as a general-purpose
        error response makes things worse. Two rules follow from watching it
        misfire on real hardware:

        Never reset a device that is not on the bus. When ``rtlsdr_open``
        fails there is nothing to reset, and hammering the port every few
        seconds stops a device that is trying to re-enumerate from ever
        finishing. That is how a momentary supply dip turned into a dongle
        that stayed gone until the Pi was rebooted.

        Back off, and give up. Repeated resets that do not help are not worth
        the disruption they cause; the counter clears as soon as a scan
        succeeds again.
        """
        now=time.time()
        if now-self.last_usb_reset < max(5.0,self._usb_backoff):
            return False
        if not self._sdr_present():
            # Absent, not wedged. Wait for it to come back on its own.
            self.last_usb_reset=now
            return False
        if self._usb_resets >= max(1,int(self.cfg.get('usb_reset_max_attempts',3))):
            return False
        self.last_usb_reset=now
        self._usb_resets+=1
        self._usb_backoff=min(120.0,15.0*(2**(self._usb_resets-1)))
        self._close_direct_sdr()
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
    def _downlink_raster(self):
        """Every ETSI raster channel in the downlink band, low to high."""
        a=float(self.cfg.get('site_band_start_hz',390e6))
        b=float(self.cfg.get('site_band_end_hz',395e6))
        step=max(1.,float(self.cfg.get('tetra_channel_spacing_hz',25000.)))
        off=float(self.cfg.get('tetra_raster_offset_hz',12500.))
        return [float(x) for x in np.arange(a+off,b,step)]

    def _survey_downlink(self):
        """Rank downlink raster channels by how much they look like a carrier.

        Ranking by raw power alone does not survive contact with real
        hardware: an RTL-SDR's internal spur comb is stronger than a distant
        base station, so the strongest channels in this band are routinely
        the receiver's own artefacts.  Measured on the reference unit, the
        top ten channels sat on an 800 kHz grid -- a comb, not a network.

        So the score also asks whether the energy is *shaped* like a 25 kHz
        TETRA carrier: flat right across the channel.  A spur is a narrow
        line and is punished for it.  This only reorders work, it can never
        admit anything: every channel still has to pass the full waveform
        test before it counts.
        """
        a=float(self.cfg.get('site_band_start_hz',390e6))
        b=float(self.cfg.get('site_band_end_hz',395e6))
        f,p=self._sweep(a,b)
        if not len(f): return []
        half=float(self.cfg.get('tetra_channel_half_width_hz',9000.))
        rows=[]
        for ch in self._downlink_raster():
            m=np.abs(f-ch)<=half
            if int(np.count_nonzero(m))<3: continue
            seg=p[m]
            level=float(np.median(seg))
            # A flat 25 kHz carrier barely differs from its own median; a
            # narrow spur towers over it.
            peak=float(np.max(seg))-level
            rows.append((ch,level,peak))
        if not rows: return []
        floor=float(np.percentile(np.array([r[1] for r in rows]),30))
        w=float(self.cfg.get('survey_flatness_weight',0.5))
        tol=float(self.cfg.get('survey_flatness_tolerance_db',6.0))
        minsnr=float(self.cfg.get('survey_min_snr_db',6.0))
        scored=[]
        for ch,level,peak in rows:
            snr=level-floor
            if snr<minsnr: continue
            scored.append((ch,snr-w*max(0.,peak-tol)))
        scored.sort(key=lambda x:x[1],reverse=True)
        # Kept whole. The shortlist below is only what the UI shows; the
        # verification queue is built from the full ranking, so a carrier the
        # survey rates 20th is reached on the fifth dwell instead of waiting
        # out the systematic remainder of the band.
        self._survey_scores=[(float(ch),float(sc)) for ch,sc in scored]
        limit=max(1,int(self.cfg.get('survey_max_candidates',12)))
        return [ch for ch,_ in scored[:limit]]

    def _refill_site_queue(self,now):
        """Rebuild the downlink verification queue for one full band pass.

        Every channel the survey could score goes first, strongest-looking
        first, and then *every* remaining raster channel.  Both halves matter.
        Ordering by survey score is what makes acquisition quick: one dwell
        covers four channels and takes about 1.1 s, so a 200-channel band is
        68 s end to end and the position of a real carrier in the queue is
        the whole time-to-lock budget.  Keeping the remainder is what makes
        it safe: on the reference unit the survey's top twelve were entirely
        spur-comb teeth, and an earlier design that queued only those never
        handed a genuine but weaker C2000 carrier to the verifier at all.

        A full pass is 50 dwells, about 70 seconds, and it only runs while no
        network is locked.
        """
        t0=time.perf_counter()
        ranked=self._survey_downlink()
        self.last_survey_ms=(time.perf_counter()-t0)*1000.
        self.survey_shortlist=list(ranked)
        self._log_pass(now)
        scored=[ch for ch,_ in self._survey_scores]
        seen={int(round(x)) for x in scored}
        rest=[x for x in self._downlink_raster() if int(round(x)) not in seen]
        # Continue the systematic pass where the previous one stopped, so
        # repeated passes do not keep re-checking the bottom of the band.
        if self._sweep_cursor:
            after=[x for x in rest if x>self._sweep_cursor]
            rest=after+[x for x in rest if x<=self._sweep_cursor]
        self._site_queue=scored+rest
        self._survey_at=now
        self._pass_best=None; self._pass_started=now; self._pass_index+=1

    def _note_pass_best(self,results):
        """Keep the most promising channel seen during this band pass.

        Without this a unit that searches for half an hour can say only that
        it found nothing, which is indistinguishable between "no C2000 here",
        "antenna fell off" and "one acceptance limit is too tight". The best
        channel and the check it failed on separate those three.
        """
        for r in results:
            if self._pass_best is None or r.snr_db>self._pass_best['snr_db']:
                self._pass_best={'freq_hz':float(r.freq_hz),
                                 'snr_db':float(r.snr_db),
                                 'ok':bool(r.ok),
                                 'fail':','.join(r.failed())}

    def _log_pass(self,now):
        """Append one line per completed band pass to the search log."""
        best=self._pass_best
        if not best or not self._pass_started:
            return
        try:
            # Next to the site state, so it follows RFEYE_SITE_STATE and a
            # test run never appends to the unit's own log.
            path=self.sites._path().with_name('search.log')
            path.parent.mkdir(parents=True,exist_ok=True)
            if path.exists() and path.stat().st_size>262144:
                keep=path.read_text().splitlines()[-800:]
                path.write_text('\n'.join(keep)+'\n')
            line=('%s pass=%d %.0fs best=%.4fMHz snr=%.1f %s locked=%d cand=%d\n'%(
                time.strftime('%Y-%m-%dT%H:%M:%S',time.localtime(now)),
                self._pass_index,now-self._pass_started,
                best['freq_hz']/1e6,best['snr_db'],
                'TETRA' if best['ok'] else ('fail:'+(best['fail'] or '?')),
                len(self.sites.locked(now)),len(self.sites.candidates(now))))
            with path.open('a') as fh:
                fh.write(line)
        except Exception:
            pass

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
        locked=self.sites.locked(now)

        # Half-verified carriers come first: they already passed the waveform
        # test at least once, so they are the cheapest route to a lock.
        priority=[c['freq_hz'] for c in self.sites.candidates(now)]

        # Re-proving a locked carrier is on its own timer, not queued behind
        # the band pass. Waiting for the pass to finish meant a locked site
        # went unchecked for the hundreds of rounds a full sweep takes, so a
        # receiver that had stopped hearing anything -- an unplugged antenna,
        # a drive out of coverage -- kept reporting a locked network. The
        # timer keeps it rare enough not to starve the pass, which is what
        # putting it behind the queue was trying to achieve.
        reverify=max(0.,float(self.cfg.get('site_reverify_s',60.0)))
        if locked and now-self._site_verify_at>=reverify:
            priority.insert(0,locked[self._survey_idx%len(locked)]['freq_hz'])
            self._survey_idx+=1
            self._site_verify_at=now

        if not priority and not self._site_queue:
            if locked:
                return []
            interval=max(5.,float(self.cfg.get('survey_idle_interval_s',15.0)))
            if self._survey_at and now-self._survey_at<interval:
                return []
            self._refill_site_queue(now)

        # The queue rides along behind the priority list, so a dwell aimed at
        # a candidate also sweeps up whichever queued neighbours fit the same
        # capture for free.
        targets=priority+list(self._site_queue)
        if not targets:
            return []

        seen=set(); ordered=[]
        for x in targets:
            k=int(round(self._raster(x,band)))
            if k not in seen:
                seen.add(k); ordered.append(float(k))

        locked_keys={int(round(x['freq_hz'])) for x in locked}
        results=self._verify(ordered,'DOWNLINK')
        self._note_pass_best(results)
        for r in results:
            # A carrier that failed only because there was nothing to hear is
            # idle, not disproved -- traffic carriers are idle most of the time.
            silent=(not r.ok and r.failed()==['snr'])
            self.sites.observe(r.freq_hz,r.ok,r.quality,now,silent=silent)

        # Only a round that actually re-tested a locked carrier can say
        # anything about whether the site is still audible. A band-pass dwell
        # across empty spectrum proves nothing and must not count.
        retested=[r for r in results if int(round(r.freq_hz)) in locked_keys]
        if retested:
            if self.sites.note_round(any(r.ok for r in retested),now):
                self.alarm.reset()

        covered={int(round(x)) for x in self.dwell_channels}
        if covered:
            self._sweep_cursor=max(float(x) for x in self.dwell_channels)
        self._site_queue=[x for x in self._site_queue
                          if int(round(x)) not in covered]
        self.survey_shortlist=[x for x in self.survey_shortlist
                               if int(round(x)) not in covered]
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
                # A TETRA site runs several carriers and a handset can be on
                # any of them, so an unfinished band pass is finished even
                # after the first lock -- stopping early would leave real
                # uplink channels unwatched. It runs on alternate cycles so
                # the uplink watch keeps priority.
                self._alt_cycle=not self._alt_cycle
                # Alternate cycles carry the band pass; a due re-proof gets a
                # turn whenever it comes round. _site_work owns the timer, so
                # a pass dwell can no longer postpone the next re-proof.
                due=(now-self._site_verify_at
                     >= max(0.,float(self.cfg.get('site_reverify_s',60.0))))
                if (self._site_queue and self._alt_cycle) or due:
                    site_results=self._site_work(now)
            else:
                site_results=self._site_work(now)

            now=time.time()
            verified=[r for r in watch_results if r.ok]
            watched=[r.freq_hz for r in watch_results]
            confirmed,level,peaks=self.alarm.update(verified,watched,now)
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
            if not site_results and not watch_results and not self._site_queue:
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
                self._usb_resets=0; self._usb_backoff=0.
                self.last_cycle_ms=(time.perf_counter()-t0)*1000.
            return True
        except Exception as e:
            err=str(e)
            low=err.lower()
            # Only a device that is present but has stopped answering is worth
            # resetting. 'open failed' means it is not on the bus at all, and
            # every _samples() failure is wrapped as 'read failed', so matching
            # that text resets on absence too -- which is precisely what kept
            # the dongle from coming back.
            if ('timeout' in low or 'short read' in low) and 'open failed' not in low:
                self._recover_sdr_usb()
            power=self._power_warning() or self._power_history
            if power:
                err=power+': '+err
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
