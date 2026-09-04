"""Touch controls for the RF Eye 320x480 compact display profile."""
import json
import threading
import time
from datetime import datetime
from pathlib import Path



def _save(app):
    from config import save_config
    save_config(app.cfg)


def _plain(value):
    if hasattr(value, "tolist"):
        return value.tolist()
    if isinstance(value, dict):
        return {str(k): _plain(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_plain(v) for v in value]
    if hasattr(value, "item"):
        try: return value.item()
        except Exception: pass
    return value


def _record_payload_snapshot(app):
    snap=app.backend.snapshot()
    return {
        "captured_at":datetime.now().astimezone().isoformat(timespec="milliseconds"),
        "source_last_update":float(snap.get("last_update",0.0) or 0.0),
        "detector":{k:_plain(snap.get(k)) for k in (
            "status","activity_confidence","mobile_confirmed","noise",
            "mobile_level","site_level","peaks","mobile_peaks","site_peaks",
            "broadband_rejected","static_rejected","artifact_calibrating",
            "artifact_sweep","artifact_baseline_count","artifact_baseline_loaded",
            "artifact_tainted_count","raw_mobile_candidate_count",
            "post_artifact_candidate_count","broadband_kept_count",
            "debug_mobile_candidates","confirm_streak","clear_streak",
            "cycle_ms","mobile_scan_ms","site_scan_ms","capture_ms","scan_windows","sdr_path")},
        "spectrum":{
            "freq_hz":_plain(snap.get("freqs",[])),
            "power_db":_plain(snap.get("spectrum",[])),
        },
    }


def _record_rf_worker(app, duration):
    started=datetime.now().astimezone(); end_mono=time.monotonic()+duration
    samples=[]; last_update=None
    try:
        while time.monotonic() < end_mono:
            item=_record_payload_snapshot(app)
            stamp=item.get("source_last_update",0.0)
            if stamp != last_update:
                samples.append(item); last_update=stamp
            time.sleep(0.12)
        item=_record_payload_snapshot(app)
        stamp=item.get("source_last_update",0.0)
        if stamp != last_update:
            samples.append(item)
        ended=datetime.now().astimezone(); cfg=app.cfg
        data={
            "schema":"rfeye-rf-series-v5",
            "recorded_from":started.isoformat(timespec="milliseconds"),
            "recorded_to":ended.isoformat(timespec="milliseconds"),
            "requested_duration_s":float(duration),
            "sample_count":len(samples),
            "capture_settings":{k:_plain(cfg.get(k)) for k in (
                "sample_rate","fft_size","mobile_capture_ms","site_capture_ms","site_scan_interval",
                "gain","ppm","detector_profile_version","mobile_band_start_hz","mobile_band_end_hz",
                "site_band_start_hz","site_band_end_hz","artifact_calibration_sweeps","artifact_min_baseline_hits",
                "artifact_rf_snr_delta_db","artifact_duty_delta","artifact_span_delta_db",
                "artifact_max_rf_snr_std_db","artifact_max_duty_std","artifact_max_span_std_db",
                "artifact_baseline_persist","artifact_baseline_max_age_days",
                "temporal_baseline_alpha","temporal_state_max_age_s",
                "temporal_rf_snr_scale_db","temporal_duty_scale","temporal_span_scale_db",
                "broadband_temporal_min_departure","broadband_dynamic_keep_max",
                "site_pair_memory_s","require_duplex_pair","require_current_duplex_pair",
                "candidate_min_confidence","strong_hit_confidence","confirm_hits","clear_hits")},
            "samples":samples,
        }
        out=Path.home()/".local"/"share"/"rfeye"/"captures"
        out.mkdir(parents=True,exist_ok=True)
        name=started.strftime("rf-series-%Y%m%d-%H%M%S-%f")[:-3]+".json"
        path=out/name
        path.write_text(json.dumps(data,indent=2,allow_nan=False)+"\n")
        app.last_rf_record_path=str(path)
        app.rf_record_message=f"SAVED {len(samples)}"
        app.rf_record_message_until=time.monotonic()+3.0
    except Exception:
        app.rf_record_message="ERROR"; app.rf_record_message_until=time.monotonic()+3.0
    finally:
        app.rf_recording=False; app.rf_record_end=0.0


def _record_rf_sample(app):
    if bool(getattr(app,"rf_recording",False)):
        return None
    duration=max(3.0,min(120.0,float(app.cfg.get("rf_record_duration_s",15.0))))
    app.rf_recording=True
    app.rf_record_end=time.monotonic()+duration
    app.rf_record_message="REC"; app.rf_record_message_until=app.rf_record_end
    threading.Thread(target=_record_rf_worker,args=(app,duration),name="rfeye-rf-recorder",daemon=True).start()
    return True


CALIBRATION_TARGETS=[(28,28),(292,28),(292,452),(28,452),(160,240)]

def _solve3(a,b):
    m=[list(map(float,a[i]))+[float(b[i])] for i in range(3)]
    for col in range(3):
        pivot=max(range(col,3),key=lambda r:abs(m[r][col]))
        if abs(m[pivot][col])<1e-12: raise ValueError('singular calibration')
        m[col],m[pivot]=m[pivot],m[col]
        div=m[col][col]; m[col]=[v/div for v in m[col]]
        for r in range(3):
            if r==col: continue
            f=m[r][col]; m[r]=[m[r][c]-f*m[col][c] for c in range(4)]
    return [m[i][3] for i in range(3)]

def _fit_affine(samples):
    rows=[[float(rx),float(ry),1.0] for rx,ry,_x,_y in samples]
    ata=[[sum(r[i]*r[j] for r in rows) for j in range(3)] for i in range(3)]
    bx=[sum(rows[n][i]*float(samples[n][2]) for n in range(len(rows))) for i in range(3)]
    by=[sum(rows[n][i]*float(samples[n][3]) for n in range(len(rows))) for i in range(3)]
    return _solve3(ata,bx)+_solve3(ata,by)

def start_calibration(app, return_page='settings'):
    app.touch_calibration_samples=[]
    app.touch_calibration_index=0
    app.touch_calibration_complete=False
    app.touch_calibration_return_page=return_page
    app.touch_calibration_last=0.0
    app.page='calibration'

def capture_calibration(app,raw_x,raw_y):
    now=time.monotonic()
    if now-float(getattr(app,'touch_calibration_last',0.0))<0.18: return
    app.touch_calibration_last=now
    if bool(getattr(app,'touch_calibration_complete',False)):
        app.page=getattr(app,'touch_calibration_return_page','settings'); return
    idx=int(getattr(app,'touch_calibration_index',0))
    if idx>=len(CALIBRATION_TARGETS): return
    tx,ty=CALIBRATION_TARGETS[idx]
    app.touch_calibration_samples.append((int(raw_x),int(raw_y),tx,ty))
    idx+=1; app.touch_calibration_index=idx
    if idx==len(CALIBRATION_TARGETS):
        try:
            coeffs=_fit_affine(app.touch_calibration_samples)
            app.cfg['touch_calibration_affine']=[round(float(v),12) for v in coeffs]
            _save(app)
            app.touch_calibration_complete=True
            app.touch_calibration_message='CALIBRATION APPLIED'
        except Exception:
            app.touch_calibration_complete=True
            app.touch_calibration_message='CALIBRATION FAILED'

def tap(app, x, y):
    now=time.monotonic()
    if now-float(getattr(app,"_last_compact_tap",0.0)) < 0.12: return
    app._last_compact_tap=now
    if app.page == "main":
        if x <= 105 and y <= 115:
            app.page = "settings"
        elif y >= 326 and x < 145:
            app._toggle_mute()
        elif y >= 326 and 104 <= x <= 248:
            app.page = "spectrum"
        return

    if app.page == "settings":
        from compact_ui_draw import SETTINGS_TOP, SETTINGS_STEP, SETTINGS_COUNT, BRIGHT_SLIDER_X0, BRIGHT_SLIDER_X1
        if y < 62:
            app.page = "main"
            return
        idx = int((y - SETTINGS_TOP) / SETTINGS_STEP)
        keys = ["demo_mode", "brightness", "record_rf", "wifi",
                "update", "spectrum", "debug"]
        if not 0 <= idx < min(SETTINGS_COUNT, len(keys)): return
        key = keys[idx]
        if key == "demo_mode":
            app._toggle_demo(); app.page = "main"
        elif key == "brightness":
            if x >= BRIGHT_SLIDER_X0-10:
                lo,hi,step=0.4,1.0,0.05
                n=max(0.0,min(1.0,(float(x)-BRIGHT_SLIDER_X0)/max(1.0,BRIGHT_SLIDER_X1-BRIGHT_SLIDER_X0)))
                v=round((lo+n*(hi-lo))/step)*step; app.cfg["brightness"]=max(lo,min(hi,v)); _save(app)
        elif key == "record_rf":
            if not bool(getattr(app,"rf_recording",False)):
                app.record_confirm_opened=time.monotonic(); app.page="record_confirm"
        elif key == "wifi": app.page = "wifi"; app._wifi_scan()
        elif key == "update": app._update_action()
        elif key == "spectrum": app.page = "spectrum"
        elif key == "debug": app.page = "debug"
        return

    if app.page == "record_confirm":
        # Ignore the opening tap for a moment; this prevents one physical press
        # from both opening and confirming the dialog on noisy resistive touch.
        if now-float(getattr(app,"record_confirm_opened",0.0)) < 0.35: return
        # Fail-safe layout: NEE is the large upper button and is the default for
        # every tap outside the deliberately separated lower JA button.  This is
        # robust against horizontal calibration error on the resistive panel.
        if 350 <= y <= 455:
            _record_rf_sample(app)
            app.page="settings"
        elif 205 <= y < 350 or y < 80:
            app.page="settings"
        return

    if app.page == "wifi":
        if app.wifi_details:
            if y < 64 or y > 430:
                app.wifi_details = None
            return
        if y < 62:
            app.page = "settings"; app.wifi_selected = None; app.wifi_password = ""
            app.wifi_shift = False; app.wifi_show_password = False
            return
        if app.wifi_selected:
            if 270 <= x <= 312 and 130 <= y <= 168:
                app.wifi_show_password = not bool(app.wifi_show_password); return
            key = app._wifi_key_at(x, y)
            if key:
                if key == "BACK": app.wifi_password = app.wifi_password[:-1]
                elif key == "SPACE" and len(app.wifi_password) < 63: app.wifi_password += " "
                elif key == "ENTER": app._wifi_connect()
                elif len(app.wifi_password) < 63: app.wifi_password += key
                return
            if 178 <= y <= 218: app._wifi_connect()
            elif 224 <= y <= 258:
                app.wifi_selected = None; app.wifi_password = ""
                app.wifi_shift = False; app.wifi_show_password = False
            return
        if y >= 434:
            app._wifi_scan(); return
        idx = int((y - 94) / 38)
        if 0 <= idx < min(9, len(app.wifi_networks)):
            ssid, _sig, _sec, active = app.wifi_networks[idx]
            if active:
                app.wifi_details = app._wifi_details_for_connected(ssid); app.wifi_selected = None
            else:
                app.wifi_selected = ssid; app.wifi_password = ""
                app.wifi_shift = False; app.wifi_show_password = False
                app.wifi_message = "ENTER PASSWORD"
        return

    if app.page == "spectrum" and (y < 62 or y > 448):
        app.page = "main"
        return

    if app.page == "debug":
        if 390 <= y <= 458:
            start_calibration(app,'debug'); return
        if y < 62 or y > 450:
            app.page = "settings"
