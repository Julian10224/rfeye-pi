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
    "detector_profile_version": 9,
    "ui_width": 480,
    "ui_height": 800,
    "physical_width": 800,
    "physical_height": 480,
    "rotation": "cw",
    "fullscreen": True,
    "scan_start_hz": 380_000_000,
    "scan_end_hz": 395_000_000,
    "sdr_device_index": 0,

    # Wideband survey. Only ever used to shortlist downlink carriers worth
    # verifying, and to draw the spectrum page. It decides nothing.
    "sample_rate": 2_048_000,
    "fft_size": 1024,
    "survey_capture_ms": 48.0,
    "survey_interval_s": 60.0,
    "survey_idle_interval_s": 15.0,
    "survey_min_snr_db": 6.0,
    "survey_flatness_weight": 0.5,
    "survey_flatness_tolerance_db": 6.0,
    "survey_max_candidates": 12,
    "mobile_percentile": 95.0,

    # Narrowband verification dwell. 288 kS/s is 28.8 MHz / 100 exactly, and
    # exactly 16x the 18000 baud TETRA symbol rate, so channel decimation is
    # exact and the symbol clock needs no resampling. 2^18 samples is 0.910 s,
    # about 16 TDMA frames, enough to measure the 17.647 Hz frame line.
    "phy_sample_rate": 288_000,
    "phy_dwell_log2": 18,
    "phy_decimation": 8,
    "phy_max_offset_hz": 100_000.0,
    "phy_timing_phases": 8,

    # TETRA acceptance limits. These mirror tetra_phy.LIMITS and are the
    # values calibrated by scripts/tetra-phy-selftest.py. Loosening any of
    # them trades false alarms back in; they are exposed so a unit can be
    # tuned in the field without editing code, not because they are guesses.
    "phy_min_snr_db": 8.0,
    "phy_min_occupied_bw_hz": 14000.0,
    "phy_max_occupied_bw_hz": 30000.0,
    "phy_min_boundary_reject_db": 6.0,
    "phy_max_flatness_db": 14.0,
    "phy_max_centre_error_hz": 8000.0,
    "phy_min_dqpsk_m": 0.20,
    "phy_min_dqpsk_phase_spread": 0.50,
    "phy_min_dqpsk_selectivity": 1.35,
    "phy_downlink_min_duty": 0.15,
    "phy_uplink_min_duty": 0.04,
    "phy_uplink_max_duty": 0.85,
    "phy_uplink_min_frame_ratio": 3.0,
    "phy_uplink_min_slot_quantisation": 0.45,
    "phy_uplink_min_bursts": 3,

    # C2000 network lock. Slow to acquire, slower to lose.
    "duplex_split_hz": 10_000_000.0,
    "site_lock_hits": 3,
    "site_unlock_misses": 4,
    "site_lock_stale_s": 600.0,
    "site_lock_max_age_days": 21.0,
    "site_forget_s": 1800.0,
    "site_reverify_s": 60.0,
    # How often the whole band is swept again once a network is locked. A site
    # runs several carriers and each one carries its own uplink partner, so a
    # single locked carrier watches a single uplink channel.
    "site_rescan_s": 300.0,
    "site_lost_rounds": 3,
    "site_state_persist": True,

    # Uplink alarm smoothing. Everything reaching this has already passed the
    # full waveform test, so these only stop the display flickering between
    # transmissions.
    "uplink_confirm_dwells": 2,
    "uplink_confirm_visits": 4,
    "uplink_state_max_age_s": 90.0,
    "uplink_alert_hold_s": 12.0,

    "tetra_channel_spacing_hz": 25_000.0,
    "tetra_raster_offset_hz": 12_500.0,
    "tetra_channel_half_width_hz": 9000.0,

    "sdr_stop_join_s": 8.0,
    "usb_reset_max_attempts": 3,
    "allow_cli_sdr_fallback": False,
    "keep_last_iq": True,
    "rf_record_iq": True,
    "mobile_band_start_hz": 380_000_000,
    "mobile_band_end_hz": 385_000_000,
    "site_band_start_hz": 390_000_000,
    "site_band_end_hz": 395_000_000,
    "max_signals": 3,
    "rf_record_duration_s": 15.0,
    "ui_fps": 20,
    # Low power mode. The detector keeps a Pi 3 B+ at its full 1.4 GHz around
    # the clock, which on a marginal supply is the difference between the
    # RTL-SDR enumerating and not. This trades time-to-lock for headroom, and
    # it is the user's call, so it lives in the settings menu.
    "low_power_mode": True,
    "low_power_ui_fps": 8,
    "low_power_scan_pause_s": 1.5,
    # Once a network is locked the uplink watch is the job, and a TETRA slot
    # is 14.2 ms. Idling for a second and a half between cycles then costs
    # detections rather than saving anything worth having.
    "low_power_locked_pause_s": 0.25,
    "gain": "auto",
    "ppm": 0,
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
    "app_version": "0.9.12",
    "update_manifest_url": "https://raw.githubusercontent.com/Julian10224/rfeye-pi/main/update/manifest.json",
    "title": "RF EYE",
}


# Keys that are calibrated physics, not user preferences. The acceptance
# limits come from ETSI constants and from measurements against real C2000
# carriers; a saved copy of them is not a setting the user chose, it is a
# snapshot of whatever release happened to write the file first.
#
# Persisting them by default froze them: a unit installed on 0.9.0 kept
# phy_downlink_min_duty = 0.80 and phy_min_dqpsk_selectivity = 1.6 through
# 0.9.1 and 0.9.2, so both of those releases' corrections were inert on it
# while the source code said otherwise. They are therefore only written when
# they actually differ from the shipped default -- a deliberate field
# override survives, an accidental fossil does not.
_DETECTOR_PREFIXES = ("phy_", "site_", "survey_", "uplink_")
_DETECTOR_KEYS = (
    "sample_rate", "fft_size", "duplex_split_hz",
    "mobile_band_start_hz", "mobile_band_end_hz",
    "mobile_percentile",
    "tetra_channel_spacing_hz", "tetra_raster_offset_hz",
    "tetra_channel_half_width_hz", "allow_cli_sdr_fallback",
    "usb_reset_max_attempts", "phy_timing_phases",
)


def is_detector_key(key):
    """True for a calibrated detector constant rather than a user setting."""
    return key.startswith(_DETECTOR_PREFIXES) or key in _DETECTOR_KEYS


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
    # Detector profile v8 replaces energy-statistics detection with real
    # ETSI EN 300 392-2 waveform verification. Every v6/v7 tuning key below
    # controlled a gate that no longer exists, so a unit upgrading from an
    # older profile must not carry its saved values forward -- they would
    # either do nothing or, where a name was reused, mean something else.
    #
    # Profile 9 additionally clears detector constants that earlier releases
    # persisted verbatim. Without this a unit first installed on 0.9.0 keeps
    # that release's acceptance limits for ever, because every later release
    # also reports profile 8 and the reset above never fires. save_config()
    # no longer writes these keys unless they differ from the default, so this
    # particular fossil cannot form again.
    if int(saved.get("detector_profile_version", 0) or 0) < 9:
        for key in list(DEFAULTS):
            if is_detector_key(key):
                cfg[key] = DEFAULTS[key]
    cfg["detector_profile_version"] = 9
    for obsolete in (
        # pre-v7 leftovers
        "threshold_db", "threshold_min_db", "threshold_max_db",
        "threshold_step_db", "threshold_soft_margin_db", "min_burst_span_db",
        "buzzer_duration_ms", "buzzer_high_hz", "buzzer_low_hz",
        # v6/v7 energy-statistics detector, removed in v8
        "artifact_calibration_sweeps", "artifact_min_baseline_hits",
        "artifact_rf_snr_delta_db", "artifact_duty_delta",
        "artifact_span_delta_db", "artifact_max_rf_snr_std_db",
        "artifact_max_duty_std", "artifact_max_span_std_db",
        "artifact_baseline_persist", "artifact_baseline_max_age_days",
        "artifact_comb_period_hz", "artifact_comb_half_width_hz",
        "artifact_comb_min_baseline_support", "artifact_comb_min_baseline_teeth",
        "artifact_comb_min_baseline_fraction", "artifact_comb_event_min_departure",
        "artifact_comb_event_min_teeth",
        "temporal_baseline_alpha", "temporal_state_max_age_s",
        "temporal_rf_snr_scale_db", "temporal_duty_scale",
        "temporal_span_scale_db", "broadband_temporal_min_departure",
        "broadband_dynamic_keep_max", "novelty_min_departure",
        "novelty_strong_departure", "max_mobile_candidates_per_sweep",
        "mobile_capture_ms", "site_capture_ms", "site_scan_interval",
        "fft_blocks", "burst_gate_db", "min_burst_duty", "max_burst_duty",
        "preferred_burst_duty_min", "preferred_burst_duty_max",
        "mobile_min_rf_snr_db", "site_min_snr_db", "site_burst_snr_db",
        "site_pair_memory_s", "site_pair_min_hits", "site_max_candidates",
        "duplex_pair_tolerance_hz", "duplex_pair_min_quality",
        "require_duplex_pair", "require_current_duplex_pair",
        "strong_pair_max_age_s", "strong_pair_min_quality",
        "strong_hit_confidence", "candidate_min_confidence",
        "confidence_attack", "confidence_release",
        "confidence_confirm", "confidence_clear",
        "uplink_confirm_window_s",
        "carrier_memory_s", "confirm_window_s", "alert_hold_s",
        "confirm_hits", "clear_hits",
    ):
        cfg.pop(obsolete, None)
    return cfg


def save_config(cfg):
    p = _config_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    # Write detector constants only where a value genuinely differs from the
    # shipped default. Anything equal to the default is left out so the next
    # release's calibration is picked up instead of being overridden by a
    # stale copy of its own former self.
    out = {k: v for k, v in cfg.items()
           if not (is_detector_key(k) and k in DEFAULTS and v == DEFAULTS[k])}
    text = json.dumps(out, indent=2) + "\n"
    try:
        if p.exists() and p.read_text() == text:
            return
    except Exception:
        pass
    tmp = p.with_name(p.name + ".tmp")
    tmp.write_text(text)
    tmp.replace(p)
