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
    "detector_profile_version": 11,
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

    # Verification dwell. 2.016 MS/s is exactly 112x the 18000 baud TETRA
    # symbol rate, so decimating by 56 gives the same 36 kS/s channel baseband
    # the tests were calibrated on. 2^20 samples is 0.520 s, about 9 TDMA
    # frames.
    #
    # Until profile 10 this was 288 kS/s, and at that rate the RTL2832U hands
    # back real carriers from 1.15 MHz away as if they were on the channel
    # being measured. Measured on rfeye, 14 September 2026: 390.0375 and
    # 390.6125 passed the full TETRA test at 16-18 dB at 288 kS/s, and were
    # plain noise at 250 kS/s, 1.008 MS/s, 2.016 MS/s and in rtl_power -- they
    # are 391.1875 and 391.7625, 1150 kHz higher. So units locked phantom
    # carriers and watched their non-existent uplinks. At 2.016 MS/s the
    # dongle's own filter is in use, and one dwell spans +-600 kHz: a whole
    # site's uplinks in one or two dwells instead of one channel per dwell.
    "phy_sample_rate": 2_016_000,
    "phy_dwell_log2": 20,
    "phy_decimation": 56,
    "phy_max_offset_hz": 600_000.0,
    "phy_timing_phases": 8,
    # Cheap level check before the waveform test. A swept band is nearly
    # all empty -- 205 of 208 channels on the field unit were flat noise --
    # and each was paid for at full price. One prefix sum over the capture
    # levels every channel at once. The screen reads high by construction
    # (a mean in linear power against a whole-capture noise floor), so it
    # can only drop what the real test would drop; measured against it,
    # -0.6 to +0.5 dB, and the margin below covers that several times over.
    "phy_screen": True,
    "phy_screen_margin_db": 2.0,

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

    # Sensitive detection. The alarm fires on a short uplink
    # control/registration burst -- what a moving mobile keys up crossing
    # cells even when nobody is talking -- and not only on a held voice call.
    # It confirms on a single verified hit. This is what a car driving past
    # emits most of the time; the trade is more false alarms, since a lone
    # freak pass now alerts. Every hit still had to pass the full 25 kHz shape
    # and pi/4-DQPSK / 18 kbaud / selectivity tests, so noise, the base
    # station bleeding in under overload, and other digital systems are still
    # rejected.
    #
    # Since 0.9.33 this is not a setting. It shipped as a toggle in 0.9.29 and
    # there was never a reason to reach for the other position: strict mode
    # only answers "is someone holding a call", which is not the question the
    # device is for, and a unit left on strict is a unit that cannot see the
    # thing it was built to see. load_config() forces it on, so a unit that
    # saved OFF gets it back on the update rather than staying quietly deaf.
    # The strict path stays in tetra_phy for the regression tests, which
    # measure both.
    "uplink_sensitive": True,
    # Sweep the whole 380-385 MHz uplink band, not only the duplex partners of
    # the locked downlinks. Until 0.9.35 the watch list was exactly those
    # partners -- on the reference unit two channels of the two hundred the
    # band holds, so 99% of it was never examined and a vehicle registered on
    # any other carrier, or on a neighbouring site, was invisible however close
    # it drove past. In the Netherlands this band carries nothing but
    # emergency-services terminals, so a channel needs no introduction from a
    # locked downlink to be worth testing.
    "uplink_sweep_band": True,
    # Capture on its own thread so the radio fills the next buffer while
    # the last one is being measured. Strictly alternating, the field
    # unit spent 0.70 s capturing and 0.93 s analysing per cycle, so it
    # listened to 380-385 MHz 11% of the time. Set false to go back to
    # one thread if a unit ever misbehaves; nothing else changes.
    "scan_pipeline": True,
    # The share of the time the dongle may stream. This is the knob that
    # decides current draw: an RTL-SDR pulls its several hundred mA while
    # it is streaming, so spending less means streaming less of the time.
    # 0.9.37 moved it without naming it -- capture on its own thread with
    # a 0.10 s pause read about 88% of the time against 37% before, and a
    # field unit threw the dongle off the bus 169 s after boot with the
    # under-voltage flag set. ECO is the shipped default; Max power lifts
    # the limit; the power ladder below caps either.
    "sdr_duty_eco": 0.55,
    # With nothing heard recently the band is swept cooler: there is no
    # track to follow, so the extra current buys nothing.
    "sdr_duty_idle": 0.45,
    "sdr_duty_max_power": 1.0,
    "sdr_duty_window_s": 20.0,
    # The power ladder (0.10.2): the most the radio may stream, learned from
    # the supply. A fresh under-voltage or a dongle that drops off the bus
    # takes it straight to power_ladder_drop_to; every power_ladder_step_s of
    # trouble-free listening climbs one step; and the step it failed on stays
    # off limits for power_ladder_retry_s, doubling each time it fails there
    # again, so it settles below what this supply can carry instead of
    # climbing back into the same failure. 1.0 means "whatever the mode
    # allows". Remembered across reboots in power-state.json.
    "power_ladder": [0.22, 0.30, 0.35, 0.40, 0.45, 0.55, 0.70, 0.85, 1.0],
    "power_ladder_drop_to": 0.30,
    "power_ladder_step_s": 300.0,
    "power_ladder_retry_s": 1800.0,
    "power_ladder_debounce_s": 30.0,
    "power_ladder_persist": True,
    # The kernel's under-voltage alarm is a file read, so it is watched every
    # couple of seconds; power.log gets a line per event and per minute.
    "power_poll_s": 2.0,
    "power_log_heartbeat_s": 60.0,
    # Capture and analysis only overlap above this radio share. Below it one
    # thread already reaches the limit, and the overlap is only a higher peak
    # current -- exactly what a rail that has just sagged cannot use.
    "scan_pipeline_min_duty": 0.40,
    "low_power_min_pause_s": 0.0,
    # How many per-channel results a snapshot carries. One dwell measures
    # up to 48 channels; at 12 a recording kept a quarter of what the
    # receiver saw, which is how a drive past a vehicle could be analysed
    # afterwards and answer nothing.
    "snapshot_phy_rows": 64,
    # One cycle in this many goes to the 390-395 MHz band pass once a
    # site is locked. It was one in two, measured at 9 of 22 cycles on a
    # unit that already held six carriers -- half the receiver's time
    # re-proving a lock that no longer gates the alarm.
    "site_pass_share": 5,
    # How many verified hits a channel *outside* the locked partners needs.
    # One hit is enough on a partner, where the site lock corroborates it; two
    # orders of magnitude more channels are swept now, so elsewhere the second
    # hit is required again. The follow-up parks the next dwells back on a
    # window that just produced a hit, so that second look costs about a second
    # rather than a whole sweep.
    "uplink_unknown_confirm_dwells": 2,
    # 100 is a peak hold over the dwell's ~146 spectrum rows. 98 kept the top
    # three of them, and a single 14 ms TETRA slot is four -- so the level of
    # the shortest real transmission was averaged away before any test saw it.
    # The bias of a peak hold lands on the noise reference too, so an empty
    # channel still measures 0.6 dB. See Channelizer.psd.
    "phy_uplink_sensitive_percentile": 100.0,

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
    #
    # 300 s dated from the 288 kS/s dwell, when a pass itself took 300 s. At
    # 2.016 MS/s a pass is about nine dwells, 30 s, so waiting five minutes
    # after the first lock before looking for the site's other carriers was
    # pure delay -- measured on rfeye, a second carrier locked only on the
    # pass after next. Driving, that is kilometres of unwatched uplinks. At
    # 120 s a pass takes about a quarter of the cycles, and each uplink
    # channel is still examined every ~3 s while one runs.
    "site_rescan_s": 120.0,
    # How long silence everywhere at once stops being an idle carrier and
    # starts being a deaf receiver. Collective on purpose: see
    # SiteRegistry.heard_recently.
    "site_silence_window_s": 180.0,
    "site_state_persist": True,

    # Uplink alarm smoothing. Everything reaching this has already passed the
    # full waveform test, so these only stop the display flickering between
    # transmissions.
    "uplink_confirm_dwells": 2,
    "uplink_confirm_visits": 4,
    # The same window for a channel with no locked partner. Wider,
    # because something that reports in every few seconds is silent on
    # most looks and its two hits land several visits apart.
    "uplink_confirm_visits_unknown": 10,
    "uplink_state_max_age_s": 90.0,
    "uplink_alert_hold_s": 12.0,
    # After a verified uplink hit, how many dwells go straight back to that
    # window so the second confirmation comes while the handset is still
    # keyed up, instead of whenever the watch rotation next comes round.
    # How long a verified hit keeps the next dwells on its own window, in
    # dwells and in seconds, whichever ends first. Three dwells was about
    # 2.4 s, which is shorter than the gap between two transmissions from
    # a terminal that reports in periodically -- so the second hit an
    # unknown channel needs could fall just outside the window that
    # exists to catch it.
    "uplink_follow_dwells": 8,
    "uplink_follow_s": 7.0,
    # How long a channel that produced verified TETRA stays in the fast
    # rotation. A hit on a channel with no locked partner used to be the
    # only one it ever got -- the sweep came back about once every fifteen
    # dwells, so the second hit the two-hit rule wants never arrived.
    # How a track is followed once it exists: fast while it still has to
    # prove itself, then at the rate it actually transmits. A confirmed
    # track with a rhythm is revisited at about half its own period --
    # measuring a channel more often than it ever says anything is pure
    # current.
    "track_revisit_fast_s": 0.8,
    "track_revisit_s": 2.5,
    "track_revisit_cool_s": 3.0,
    # How long after its last transmission a channel keeps its place in
    # the fast rotation. A vehicle reporting in every few seconds is
    # between transmissions most of the time; dropping it back to the
    # band sweep the moment it goes quiet is how one hit used to be the
    # only one a channel ever got.
    "track_hot_s": 60.0,
    # When a signal counts as here, fading, or gone.
    "track_active_s": 2.0,
    "track_fading_s": 5.0,
    "track_forget_s": 90.0,
    "track_forget_quiet_s": 20.0,
    # The bar is the RF level between these two, not the waveform score.
    # How many dwells in a row a track may take before the partner and
    # band lanes get one. Without it a channel that never stops
    # transmitting is due every round and nothing else is measured.
    "uplink_track_run": 2,
    "ui_level_weak_db": 8.0,
    "ui_level_strong_db": 26.0,

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
    # A recording logs what the detector sees for this long. An uplink control
    # burst is a fraction of a second and each uplink channel comes round only
    # every few seconds, so a short window rarely catches one -- 15 s never did
    # across fifteen field recordings. A minute gives a burst about four times
    # the chance of falling inside a visited dwell. The worker clamps to 120 s.
    "rf_record_duration_s": 60.0,
    "ui_fps": 20,
    # Low power mode. The detector keeps a Pi 3 B+ at its full 1.4 GHz around
    # the clock, which on a marginal supply is the difference between the
    # RTL-SDR enumerating and not. This trades time-to-lock for headroom, and
    # it is the user's call, so it lives in the settings menu.
    "low_power_mode": True,
    "low_power_ui_fps": 8,
    "low_power_idle_ui_fps": 3,
    "power_notice_timeout_s": 30.0,
    "low_power_scan_pause_s": 1.5,
    # Once a network is locked the uplink watch is the job, and a TETRA slot
    # is 14.2 ms. Idling for a second and a half between cycles then costs
    # detections rather than saving anything worth having.
    "low_power_locked_pause_s": 0.15,
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
    "app_version": "0.10.2",
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
    # Sensitive detection stopped being a setting in 0.9.33. A unit that had
    # the old Gevoelig toggle switched off would otherwise carry that through
    # the update and go on ignoring exactly the short control bursts this
    # release exists to catch, with nothing on screen to say so.
    cfg["uplink_sensitive"] = True
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
    #
    # Profile 10 moves the verification dwell from 288 kS/s to 2.016 MS/s. The
    # MHS35 reference config of every earlier install wrote the old dwell out
    # explicitly, so without this reset exactly those units would keep the
    # rate that locks phantom carriers.
    #
    # Profile 11 is the short-burst release. The claim in save_config() that
    # the fossil "cannot form again" was wrong for one path: install-cuqi35.sh
    # copies config/reference-config-cuqi35.json into place verbatim, detector
    # constants and all, so every unit installed from 0.9.29 onwards holds an
    # explicit phy_uplink_sensitive_percentile of 98 -- which would survive
    # this update and keep peak hold switched off on exactly the units that
    # need it. The site state is rebuilt too: locks made under the old
    # occupancy measurement are not evidence for the new one, and re-locking
    # costs about a minute.
    if int(saved.get("detector_profile_version", 0) or 0) < 11:
        for key in list(DEFAULTS):
            if is_detector_key(key):
                cfg[key] = DEFAULTS[key]
    cfg["detector_profile_version"] = 11
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
