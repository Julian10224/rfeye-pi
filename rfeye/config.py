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
    "preferred_burst_duty_min": 0.06,
    "preferred_burst_duty_max": 0.45,
    "mobile_min_rf_snr_db": 5.0,
    "site_min_snr_db": 5.0,
    "site_burst_snr_db": 8.0,
    "site_pair_memory_s": 4.0,
    "site_pair_min_hits": 1,
    "duplex_pair_tolerance_hz": 1000.0,
    "duplex_pair_min_quality": 0.28,
    "require_duplex_pair": True,
    "candidate_min_confidence": 0.48,
    "confidence_attack": 0.58,
    "confidence_release": 0.20,
    "confidence_confirm": 0.62,
    "confidence_clear": 0.30,
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
    "app_version": "0.7.21",
    "update_manifest_url": "https://raw.githubusercontent.com/Julian10224/rfeye-pi/display-cuqi-35-portrait/update/manifest.json",
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
    cfg["buzzer_gpio"] = 26
    cfg["buzzer_model"] = "TMB12A03"
    cfg["buzzer_passive"] = False
    cfg["buzzer_active_high"] = True
    cfg["app_version"] = DEFAULTS["app_version"]
    cfg["update_manifest_url"] = DEFAULTS["update_manifest_url"]
    return cfg


def save_config(cfg):
    p = _config_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(cfg, indent=2))
