from pathlib import Path
import json
import os

DEFAULTS = {
    "ui_width": 480,
    "ui_height": 800,
    "physical_width": 800,
    "physical_height": 480,
    "rotation": "cw",
    "fullscreen": True,

    "scan_start_hz": 380_000_000,
    "scan_end_hz": 395_000_000,

    "sample_rate": 2_048_000,
    "fft_size": 1024,
    "fft_blocks": 8,
    "mobile_capture_ms": 72.0,
    "site_capture_ms": 72.0,
    "mobile_percentile": 95.0,
    "tetra_channel_half_width_hz": 9000.0,
    "burst_gate_db": 6.0,
    "min_burst_span_db": 9.0,
    "min_burst_duty": 0.035,
    "max_burst_duty": 0.65,
    "site_min_snr_db": 5.0,
    "site_burst_snr_db": 8.0,
    "site_pair_memory_s": 4.0,
    "require_duplex_pair": True,
    "ui_fps": 20,

    "gain": "auto",
    "ppm": 0,
    "threshold_db": 10.0,
    "confirm_hits": 2,
    "clear_hits": 2,
    "mobile_band_start_hz": 380_000_000,
    "mobile_band_end_hz": 385_000_000,
    "site_band_start_hz": 390_000_000,
    "site_band_end_hz": 395_000_000,
    "max_signals": 3,

    "muted": False,
    "demo_mode": False,
    "auto_demo_if_no_sdr": False,
    "audio_mode": "adaptive",

    # GPIO buzzer
    "buzzer_gpio": 18,
    "buzzer_passive": True,
    "buzzer_active_high": True,
    "buzzer_low_hz": 900,
    "buzzer_high_hz": 1500,
    "buzzer_duration_ms": 85,
    "brightness": 1.0,
    "show_frequency": True,
    "show_brand_text": True,

    "touch_invert_x": False,
    "touch_invert_y": False,

    "app_version": "0.7.8",
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
    try:
        if p.exists():
            cfg.update(json.loads(p.read_text()))
    except Exception:
        pass
    # Release metadata belongs to the installed code, never to stale user settings.
    cfg["app_version"] = DEFAULTS["app_version"]
    cfg["update_manifest_url"] = DEFAULTS["update_manifest_url"]
    return cfg

def save_config(cfg):
    p = _config_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(cfg, indent=2))
