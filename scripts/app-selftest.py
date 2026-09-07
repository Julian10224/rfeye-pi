#!/usr/bin/env python3
"""Headless RF Eye application/UI regression test."""
from __future__ import annotations

import os
import tempfile
import time
from pathlib import Path

os.environ.setdefault("SDL_VIDEODRIVER","dummy")
os.environ["RFEYE_DISPLAY_PROFILE"]="cuqi35"

_tmp=tempfile.TemporaryDirectory(prefix="rfeye-app-test-")
os.environ["HOME"]=_tmp.name
os.environ["RFEYE_CONFIG"]=str(Path(_tmp.name)/"config.json")

import pygame
import sdr_backend
import buzzer


class FakeBackend:
    def __init__(self,cfg): self.cfg=cfg
    def start(self): pass
    def stop(self): pass
    def set_demo(self,value): self.cfg["demo_mode"]=bool(value)
    def snapshot(self):
        return {
            "status":"LIVE","error":"","detector_state":"LOCKED",
            "peaks":[],"mobile_peaks":[],
            "site_peaks":[{"freq_hz":391237500.0,"quality":0.62,"hits":4}],
            "mobile_level":0.0,"site_level":0.62,"activity_confidence":0.0,
            "mobile_confirmed":False,"freqs":[],"spectrum":[],"noise":-100.0,
            "last_update":time.time(),"demo":False,"cycle_ms":1400.0,
            "mobile_scan_ms":950.0,"site_scan_ms":320.0,"capture_ms":910.0,
            "dwell_ms":910.0,"verify_ms":260.0,"survey_ms":320.0,
            "scan_windows":1,"confirm_streak":0,"clear_streak":1,
            "network_locked":True,"site_locked_count":1,"site_candidate_count":0,
            "site_state_loaded":True,"watch_freqs":[381237500.0],
            "dwell_centre_hz":381287500.0,"dwell_role":"UPLINK",
            "survey_shortlist":[],"phy":[],
            "search_best":{"freq_hz":391237500.0,"snr_db":10.6,"ok":False,
                           "fail":"bandwidth,boundary"},
            "sdr_path":"TEST","power_warning":"","power_history":"",
            "power_detail":"",
        }


class FakeBuzzer:
    def __init__(self,*args,**kwargs): pass
    def close(self): pass
    def off(self): pass
    def beep_pattern(self,*args,**kwargs): pass


# Kept before the stub goes in: the supply-flag test needs the real backend,
# and everything else in this file needs it out of the way.
_REAL_BACKEND=sdr_backend.SDRBackend
sdr_backend.SDRBackend=FakeBackend
buzzer.GPIOBuzzer=FakeBuzzer

import app as appmod
from config import load_config
appmod.GPIOBuzzer=FakeBuzzer


def _check_config_migration():
    """A saved config must never freeze the calibrated detector limits.

    Persisting them looked harmless and was not: a unit first installed on
    0.9.0 kept that release's acceptance limits through 0.9.1 and 0.9.2,
    because every later release also reported detector profile 8 and the
    reset never fired. The source said 0.15 while the device ran 0.80.
    """
    import importlib, json, tempfile
    import config as cfgmod

    original = os.environ.get("RFEYE_CONFIG")
    try:
        for name, saved in (
            ("pre-v8 unit", {"detector_profile_version": 7,
                             "novelty_min_departure": 9.9, "muted": True}),
            ("0.9.0 fossil", {"detector_profile_version": 8,
                              "phy_downlink_min_duty": 0.8,
                              "phy_min_dqpsk_selectivity": 1.6,
                              "brightness": 0.42}),
            ("fresh install", {}),
        ):
            with tempfile.TemporaryDirectory() as d:
                path = os.path.join(d, "config.json")
                with open(path, "w") as fh:
                    json.dump(saved, fh)
                os.environ["RFEYE_CONFIG"] = path
                importlib.reload(cfgmod)
                cfg = cfgmod.load_config()
                for key, want in cfgmod.DEFAULTS.items():
                    if cfgmod.is_detector_key(key):
                        assert cfg[key] == want, (name, key, cfg[key], want)
                if "muted" in saved:
                    assert cfg["muted"] is True, name
                if "brightness" in saved:
                    assert cfg["brightness"] == 0.42, name
                # Saving must not write the defaults back as if chosen.
                cfgmod.save_config(cfg)
                with open(path) as fh:
                    on_disk = json.load(fh)
                frozen = [k for k in on_disk
                          if cfgmod.is_detector_key(k)
                          and on_disk[k] == cfgmod.DEFAULTS.get(k)]
                assert not frozen, (name, frozen)
                # A deliberate override still has to survive.
                cfg["phy_min_dqpsk_m"] = 0.31
                cfgmod.save_config(cfg)
                with open(path) as fh:
                    assert json.load(fh)["phy_min_dqpsk_m"] == 0.31, name
    finally:
        if original is None:
            os.environ.pop("RFEYE_CONFIG", None)
        else:
            os.environ["RFEYE_CONFIG"] = original
        importlib.reload(cfgmod)


def _check_display_profile_fallback():
    """An updated unit must still be able to pick its own panel layout.

    ``RFEYE_DISPLAY_PROFILE`` comes from the systemd user unit, and an
    application-only OTA update is not allowed to rewrite that unit. A unit
    installed before the variable existed therefore kept drawing the 480x800
    layout on a 480x320 panel -- the top-left corner of a much larger screen,
    which reads as a black display and cannot be recovered from the touchscreen.
    """
    import json as _json
    import compact_display_patch as cdp

    keep_profile = os.environ.get("RFEYE_DISPLAY_PROFILE")
    keep_config = os.environ.get("RFEYE_CONFIG")
    with tempfile.TemporaryDirectory() as d:
        path = os.path.join(d, "config.json")
        os.environ["RFEYE_CONFIG"] = path
        try:
            # An explicit environment value still decides, in both directions.
            os.environ["RFEYE_DISPLAY_PROFILE"] = "cuqi35"
            Path(path).write_text(_json.dumps({"display_profile": "elecrow"}))
            assert cdp.wants_compact_profile() is True
            os.environ["RFEYE_DISPLAY_PROFILE"] = "elecrow"
            Path(path).write_text(_json.dumps({"display_profile": "cuqi35"}))
            assert cdp.wants_compact_profile() is False

            # With nothing in the environment the saved config decides.
            os.environ.pop("RFEYE_DISPLAY_PROFILE", None)
            assert cdp.wants_compact_profile() is True
            Path(path).write_text(_json.dumps({"display_profile": "elecrow"}))
            assert cdp.wants_compact_profile() is False

            # No config at all must not raise and must not claim the panel.
            os.remove(path)
            assert cdp.wants_compact_profile() is False
            Path(path).write_text("{ not json")
            assert cdp.wants_compact_profile() is False
        finally:
            os.environ.pop("RFEYE_DISPLAY_PROFILE", None)
            if keep_profile is not None:
                os.environ["RFEYE_DISPLAY_PROFILE"] = keep_profile
            os.environ.pop("RFEYE_CONFIG", None)
            if keep_config is not None:
                os.environ["RFEYE_CONFIG"] = keep_config


def _check_power_flags():
    """Only a live under-voltage is a warning; the since-boot bit is history.

    Bit 16 of ``get_throttled`` latches at the first dip and never clears, so
    treating it as a warning meant one brown-out at power-up put "USB POWER
    TOO LOW" on the display for the rest of the session.
    """
    import shutil as _shutil
    import subprocess as _subprocess

    class _CP:
        def __init__(self, out): self.stdout = out

    def make(word):
        def run(cmd, **kw):
            if cmd[1] == "get_throttled":
                return _CP("throttled=%s\n" % word)
            return _CP("volt=1.2563V\n")
        return run

    real_which, real_run = sdr_backend.shutil.which, sdr_backend.subprocess.run
    sdr_backend.shutil.which = lambda name: "/usr/bin/vcgencmd"
    try:
        b = _REAL_BACKEND(dict(appmod.load_config()))
        for word, flag, history in (("0x0", "", ""),
                                    ("0x50000", "", "UNDER-VOLTAGE EARLIER"),
                                    ("0x50005", "UNDER-VOLTAGE", "UNDER-VOLTAGE EARLIER"),
                                    ("0x1", "UNDER-VOLTAGE", "")):
            sdr_backend.subprocess.run = make(word)
            b._power_checked = 0.0
            assert b._power_warning() == flag, (word, b._power_flag)
            assert b._power_history == history, (word, b._power_history)
            assert (word in b._power_detail) if word != "0x0" else True
            assert b.snapshot()["power_warning"] == flag
    finally:
        sdr_backend.shutil.which = real_which
        sdr_backend.subprocess.run = real_run


def main():
    _check_config_migration()

    assert appmod._split_nmcli_terse("*:Home:88:WPA2",4)==["*","Home","88","WPA2"]
    assert appmod._split_nmcli_terse(r":Cafe\:Guest:72:WPA2",4)==["","Cafe:Guest","72","WPA2"]
    assert appmod._split_nmcli_terse(r":Back\\Slash:55:WPA3",4)==["","Back\\Slash","55","WPA3"]

    a=appmod.App(load_config(),fullscreen=False)
    assert (a.uw,a.uh,a.pw,a.ph)==(320,480,480,320)
    assert a._tap.__module__=="compact_ui_controls"
    assert a._draw_main.__module__=="compact_ui_draw"
    assert a._wifi_connect.__module__=="wifi_patch"
    assert a._wifi_key_at.__module__=="compact_wifi_ui"

    a._wifi_text=lambda:"CONNECTED"
    snap=a.backend.snapshot()

    a.recording_entries=[]
    a.recording_selected={
        "label":"2026-09-05 06:55:51","mode":"EXACT v7","samples":13,
        "duration":15.0,"path":str(Path(_tmp.name)/"missing.json"),
    }
    a.recording_replay_mode="EXACT v7"
    a.recording_replay_total=13
    a.recording_replay_index=5
    a.recording_replay_alerts=0
    a.recording_replay_running=True
    a.recording_replay_snapshot={
        "spectrum":[-100.0,-90.0,-95.0],"noise":-105.0,"peaks":[],
    }

    drawers={
        "main":lambda:a._draw_main(snap),
        "settings":a._draw_settings,
        "spectrum":lambda:a._draw_spectrum(snap),
        "debug":lambda:a._draw_debug(snap),
        "record_confirm":a._draw_record_confirm,
        "recordings":a._draw_recordings,
        "recording_detail":a._draw_recording_detail,
        "recording_delete_confirm":a._draw_recording_delete_confirm,
        "recording_replay":a._draw_recording_replay,
        "wifi":a._draw_wifi,
    }
    for page,draw in drawers.items():
        a.page=page
        draw()
        assert a.ui.get_size()==(320,480),page

    # Compact touch route: main -> settings -> record confirmation -> cancel.
    a.page="main"
    a._last_compact_tap=0.0
    a._tap(30,40)
    assert a.page=="settings"
    a._last_compact_tap=0.0
    a._tap(40,66+2*48+20)
    assert a.page=="record_confirm"
    a.record_confirm_opened=time.monotonic()-1.0
    a._last_compact_tap=0.0
    a._tap(160,250)
    assert a.page=="settings"

    # Keyboard SHIFT and escaped SSID handling stay available in compact mode.
    a.wifi_shift=False
    assert a._wifi_key_at(90,405) is None
    assert a.wifi_shift is True

    # A stale replay worker must not resurrect/overwrite newer replay state.
    from compact_ui_controls import _recording_replay_worker
    replay_file=Path(_tmp.name)/"replay.json"
    replay_file.write_text('{"schema":"rfeye-rf-series-v7","samples":[]}\n')
    a.recording_replay_generation=2
    a.recording_replay_running=False
    a.recording_replay_error="newer state"
    _recording_replay_worker(a,str(replay_file),1)
    assert a.recording_replay_running is False
    assert a.recording_replay_error=="newer state"
    _recording_replay_worker(a,str(Path(_tmp.name)/"missing-replay.json"),1)
    assert a.recording_replay_error=="newer state"

    # The supply notice is a one-shot overlay, not a mode. It has to arm on a
    # live under-voltage, swallow every tap while it is up, disappear on its
    # own button and never come back in the same session -- and none of that
    # may touch the scan thread, which is the whole point of the change.
    assert a.power_notice_open is False and a.power_notice_done is False
    a._power_notice_update({"power_history":"UNDER-VOLTAGE EARLIER"})
    assert a.power_notice_open is False, "sticky since-boot bit must not raise it"
    a._power_notice_update({"power_warning":"UNDER-VOLTAGE",
                            "power_detail":"core 1.2563V, throttled 0x50005"})
    assert a.power_notice_open is True
    assert any("0x50005" in line for line in a.power_notice_lines)
    btn=a._power_notice_button()
    a.page="main"
    assert a._power_notice_tap(2,2) is True, "taps outside the button are swallowed"
    assert a.power_notice_open is True and a.page=="main"
    assert a._power_notice_tap(btn.centerx,btn.centery) is True
    assert a.power_notice_open is False and a.power_notice_done is True
    a._power_notice_update({"power_warning":"UNDER-VOLTAGE"})
    assert a.power_notice_open is False, "at most one notice per session"
    assert a._power_notice_tap(btn.centerx,btn.centery) is False
    _check_power_flags()
    _check_display_profile_fallback()

    a.running=False
    a.backend.stop()
    a.buzzer.close()
    pygame.quit()
    _tmp.cleanup()
    print("RF Eye headless app self-test: OK")


if __name__=="__main__":
    main()
