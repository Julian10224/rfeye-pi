from pathlib import Path
import json
import os
import subprocess
import threading
import time


def _kiosk_guard_worker():
    """Keep Raspberry Pi desktop chrome out of the RF Eye appliance session."""
    uid = str(os.getuid())
    while True:
        try:
            own_uid = os.getuid()
            for proc_dir in Path("/proc").glob("[0-9]*"):
                try:
                    if proc_dir.stat().st_uid != own_uid:
                        continue
                    argv = [x for x in (proc_dir / "cmdline").read_bytes().split(b"\0") if x]
                    if argv[-2:] in ([b"/usr/bin/lwrespawn", b"/usr/bin/wf-panel-pi"], [b"/usr/bin/lwrespawn", b"/usr/bin/pcmanfm-pi"]):
                        os.kill(int(proc_dir.name), 15)
                except (FileNotFoundError, ProcessLookupError, PermissionError, ValueError):
                    pass
        except Exception:
            pass
        for process_name in ("wf-panel-pi", "pcmanfm-pi", "lxpanel", "squeekboard"):
            try:
                subprocess.run(
                    ["pkill", "-u", uid, "-x", process_name],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    check=False,
                    timeout=2,
                )
            except Exception:
                pass
        time.sleep(3.0)


def _start_kiosk_guard():
    if not os.environ.get("WAYLAND_DISPLAY"):
        return
    threading.Thread(target=_kiosk_guard_worker,name="rfeye-kiosk-guard",daemon=True).start()


_start_kiosk_guard()

try:
    from wifi_patch import install_app_patch
    install_app_patch()
except Exception:
    pass

try:
    from compact_display_patch import install_app_patch as install_compact_display_patch
    install_compact_display_patch()
except Exception:
    pass

DEFAULTS = {
    "detector_profile_version": 7,
    "ui_width": 480,
    "ui_height": 800,
    "physical_width": 800,
    "physical_height": 480,
    "rotation": "cw",
    "fullscreen": True,
    "scan_start_hz": 380_000_000,
    "scan_end_hz": 395_000_000,
    "sample_rate": 2_048_000,
    "sdr_device_index": 0,
    "allow_cli_sdr_fallback": True,
    "fft_size": 1024,
    "fft_blocks": 8,
    "mobile_capture_ms": 64.0,
    "site_capture_ms": 36.0,
    "site_scan_interval": 3,
    "sdr_stop_join_s": 8.0,
    "carrier_memory_s": 2.5,
    "confirm_window_s": 2.2,
    "alert_hold_s": 3.0,
    "strong_hit_confidence": 0.78,
    "artifact_calibration_sweeps": 5,
    "artifact_min_baseline_hits": 4,
    "artifact_rf_snr_delta_db": 5.0,
    "artifact_duty_delta": 0.12,
    "artifact_span_delta_db": 3.5,
    "artifact_max_rf_snr_std_db": 2.0,
    "artifact_max_duty_std": 0.08,
    "artifact_max_span_std_db": 2.5,
    "artifact_baseline_persist": True,
    "artifact_baseline_max_age_days": 30.0,
    "artifact_comb_period_hz": 400_000.0,
    "artifact_comb_half_width_hz": 50_000.0,
    "artifact_comb_min_baseline_support": 8,
    "artifact_comb_min_baseline_teeth": 4,
    "artifact_comb_min_baseline_fraction": 0.45,
    "artifact_comb_event_min_departure": 1.25,
    "artifact_comb_event_min_teeth": 2,
    "temporal_baseline_alpha": 0.08,
    "temporal_state_max_age_s": 30.0,
    "temporal_rf_snr_scale_db": 4.0,
    "temporal_duty_scale": 0.10,
    "temporal_span_scale_db": 3.0,
    "broadband_temporal_min_departure": 1.25,
    "broadband_dynamic_keep_max": 6,
    "mobile_percentile": 95.0,
    "tetra_channel_spacing_hz": 25_000.0,
    "tetra_raster_offset_hz": 12_500.0,
    "tetra_channel_half_width_hz": 9000.0,
    "burst_gate_db": 6.0,
    "min_burst_duty": 0.035,
    "max_burst_duty": 0.65,
    "preferred_burst_duty_min": 0.06,
    "preferred_burst_duty_max": 0.45,
    "mobile_min_rf_snr_db": 5.0,
    "site_min_snr_db": 5.0,
    "site_burst_snr_db": 8.0,
    "site_pair_memory_s": 5.0,
    "site_pair_min_hits": 2,
    "site_max_candidates": 64,
    "duplex_pair_tolerance_hz": 1000.0,
    "duplex_pair_min_quality": 0.40,
    "novelty_min_departure": 1.25,
    "novelty_strong_departure": 2.0,
    "strong_pair_max_age_s": 2.5,
    "strong_pair_min_quality": 0.75,
    "require_duplex_pair": True,
    "require_current_duplex_pair": False,
    "max_mobile_candidates_per_sweep": 12,
    "candidate_min_confidence": 0.52,
    "confidence_attack": 0.58,
    "confidence_release": 0.20,
    "confidence_confirm": 0.62,
    "confidence_clear": 0.30,
    "ui_fps": 20,
    "gain": "auto",
    "ppm": 0,
    "rf_record_duration_s": 15.0,
    "confirm_hits": 2,
    "clear_hits": 2,
    "mobile_band_start_hz": 380_000_000,
    "mobile_band_end_hz": 385_000_000,
    "site_band_start_hz": 390_000_000,
    "site_band_end_hz": 395_000_000,
    "max_signals": 3,
    "muted": False,
    "startup_chime": True,
    "demo_mode": False,
    "auto_demo_if_no_sdr": False,
    "audio_mode": "adaptive",
    "touch_calibration_affine": [0.0, -0.08831672203765227, 342.6688815060908, -0.12914532218926936, 0.0, 508.31598813696417],
    "buzzer_gpio": 26,
    "buzzer_model": "TMB12A03",
    "buzzer_passive": False,
    "buzzer_active_high": True,
    "buzzer_green_ms": 95,
    "buzzer_yellow_ms": 85,
    "buzzer_red_ms": 75,
    "buzzer_green_gap_ms": 0,
    "buzzer_yellow_gap_ms": 70,
    "buzzer_red_gap_ms": 55,
    "brightness": 1.0,
    "show_frequency": True,
    "show_brand_text": True,
    "touch_invert_x": False,
    "touch_invert_y": False,
    "app_version": "0.7.37",
    "update_manifest_url": "https://raw.githubusercontent.com/Julian10224/rfeye-pi/main/update/manifest.json",
    "title": "RF EYE",
}


def _config_path():
    env = os.getenv("RFEYE_CONFIG")
    if env:
        return Path(env)
    if os.geteuid() == 0:
        return Path("/var/lib/rfeye/config.json")
    return Path.home() / ".config" / "rfeye" / "config.json"


def load_config():
    cfg = dict(DEFAULTS)
    p = _config_path()
    saved = {}
    try:
        if p.exists():
            saved = json.loads(p.read_text())
            cfg.update(saved)
    except Exception:
        saved = {}
    # RF Eye hardware profile is fixed for this branch too. Migrate stale
    # persisted GPIO18 settings when an existing unit receives the OTA update.
    cfg["buzzer_gpio"] = 26
    cfg["buzzer_model"] = "TMB12A03"
    cfg["buzzer_passive"] = False
    cfg["buzzer_active_high"] = True
    cfg["audio_mode"] = "adaptive"
    cfg["app_version"] = DEFAULTS["app_version"]
    cfg["update_manifest_url"] = DEFAULTS["update_manifest_url"]
    # Detector profile v7 keeps the corrected +12.5 kHz TETRA raster and
    # v6 novelty/duplex gates, then adds a learned coherent-comb rejector for
    # the Pi/RTL-SDR hardware pattern seen during real driving tests.
    if int(saved.get("detector_profile_version", 0) or 0) < 7:
        for key in (
            "mobile_capture_ms", "site_capture_ms", "site_scan_interval",
            "carrier_memory_s", "confirm_window_s", "alert_hold_s",
            "strong_hit_confidence", "artifact_calibration_sweeps",
            "artifact_min_baseline_hits", "artifact_rf_snr_delta_db",
            "artifact_duty_delta", "artifact_span_delta_db",
            "artifact_max_rf_snr_std_db", "artifact_max_duty_std",
            "artifact_max_span_std_db", "artifact_baseline_persist",
            "artifact_baseline_max_age_days",
            "artifact_comb_period_hz", "artifact_comb_half_width_hz",
            "artifact_comb_min_baseline_support", "artifact_comb_min_baseline_teeth",
            "artifact_comb_min_baseline_fraction", "artifact_comb_event_min_departure",
            "artifact_comb_event_min_teeth",
            "temporal_baseline_alpha", "temporal_state_max_age_s",
            "temporal_rf_snr_scale_db", "temporal_duty_scale",
            "temporal_span_scale_db", "broadband_temporal_min_departure",
            "broadband_dynamic_keep_max",
            "tetra_channel_spacing_hz", "tetra_raster_offset_hz",
            "site_pair_memory_s", "site_pair_min_hits", "site_max_candidates",
            "duplex_pair_min_quality", "novelty_min_departure",
            "novelty_strong_departure", "strong_pair_max_age_s",
            "strong_pair_min_quality", "candidate_min_confidence",
            "require_current_duplex_pair",
        ):
            cfg[key] = DEFAULTS[key]
    cfg["detector_profile_version"] = 7
    for obsolete in (
        "threshold_db", "threshold_min_db", "threshold_max_db",
        "threshold_step_db", "threshold_soft_margin_db", "min_burst_span_db",
        "buzzer_duration_ms", "buzzer_high_hz", "buzzer_low_hz",
    ):
        cfg.pop(obsolete, None)
    return cfg


def save_config(cfg):
    p = _config_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps(cfg, indent=2) + "\n"
    try:
        if p.exists() and p.read_text() == text:
            return
    except Exception:
        pass
    tmp = p.with_name(p.name + ".tmp")
    tmp.write_text(text)
    tmp.replace(p)
