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
    def reinit_usb(self):
        self.reinit_calls=getattr(self,"reinit_calls",0)+1
        return {"present":False,"reset":False}
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
            "last_pass_s":72.4,
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


def _check_frame_guard(a):
    """A frame that raises must not be able to take the appliance down.

    Before this, one exception anywhere in a draw path ended the process;
    systemd restarted it half a second later and a fault that repeated every
    frame became a restart loop showing nothing but the compositor's black
    background. Untestable and unreadable at exactly the moment it matters,
    so the fault now stays on screen and in crash.log.
    """
    keep_home = os.environ.get("HOME")
    keep_page = a.page
    real_draw = a.__class__._draw_main

    def explode(self, snap):
        raise RuntimeError("simulated draw failure")

    home = os.path.join(_tmp.name, "guard-home")
    os.makedirs(home, exist_ok=True)
    os.environ["HOME"] = home
    a.page = "main"
    a.frame_error_count = 0
    a.frame_error_logged = ""
    try:
        a.__class__._draw_main = explode
        for _ in range(3):
            a._guarded_frame()          # must not raise
        assert a.frame_error_count == 3, a.frame_error_count
        assert "simulated draw failure" in str(a.frame_error), a.frame_error
        log = Path(home) / ".local" / "state" / "rfeye" / "crash.log"
        assert log.is_file(), "the fault must survive the drive, not just the frame"
        body = log.read_text()
        assert "simulated draw failure" in body
        # The traceback text mentions RuntimeError twice, so count entries
        # by their header instead: a fault that repeats every frame must not
        # write a log line every frame.
        assert body.count("page=main") == 1, "one entry per distinct fault"
    finally:
        a.__class__._draw_main = real_draw
        os.environ.pop("HOME", None)
        if keep_home is not None:
            os.environ["HOME"] = keep_home

    # And it clears itself as soon as drawing works again.
    a._guarded_frame()
    assert a.frame_error is None
    assert a.frame_error_streak == 0

    # The guard must not become a trap. Until 0.9.7 an exception ended the
    # process and systemd started a fresh one, which is what carried a unit
    # through a transient failure at boot. Catching everything removed that,
    # so a fault that never clears now hands the process back deliberately.
    keep_limit = a.cfg.get("frame_error_restart_frames")
    a.cfg["frame_error_restart_frames"] = 4
    a.running = True
    try:
        a.__class__._draw_main = explode
        for _ in range(4):
            a._guarded_frame()
        assert a.frame_error_streak == 4, a.frame_error_streak
        assert a.running is False, "a fault that never clears must be handed back"
    finally:
        a.__class__._draw_main = real_draw
        a.cfg.pop("frame_error_restart_frames", None)
        if keep_limit is not None:
            a.cfg["frame_error_restart_frames"] = keep_limit
    a.running = True
    a._guarded_frame()
    assert a.frame_error is None and a.running is True
    a.page = keep_page


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


def _check_recording_roundtrip(a):
    """Capture -> file -> browser -> replay, the whole way round.

    A recording is the only evidence the unit leaves behind after a drive, so
    it is worth nothing unless every stage still works: the worker has to
    write a v8 file with the dwell verdicts in it, the browser has to list
    that file, and replay has to hand back the verdict that was recorded
    rather than deciding again from the summary numbers.
    """
    import compact_ui_controls as ctl
    from recording_replay import (load_recording, recording_label,
                                  schema_mode, is_replayable, replay_recording)

    out = Path(_tmp.name) / "captures"
    keep_dir = ctl._capture_dir
    keep_page = a.page
    keep_duration = a.cfg.get("rf_record_duration_s")
    real_snapshot = a.backend.snapshot

    def snapshot_with_phy():
        snap = real_snapshot()
        snap["phy"] = [{"freq_hz": 391237500.0, "ok": True, "snr_db": 23.8,
                        "dqpsk_m": 0.71, "role": "DOWNLINK"}]
        return snap

    # Path.home() is not redirectable through HOME on every platform this
    # test runs on, so point the capture directory at the temp tree itself.
    ctl._capture_dir = lambda: out
    a.backend.snapshot = snapshot_with_phy
    try:
        a.cfg["rf_record_duration_s"] = 3.0
        assert ctl._record_rf_sample(a) is True
        assert a.rf_recording is True
        assert ctl._record_rf_sample(a) is None, "one recorder at a time"
        for _ in range(200):
            if not a.rf_recording:
                break
            time.sleep(0.1)
        assert a.rf_recording is False, "the recorder thread has to finish"
        assert str(a.rf_record_message).startswith("SAVED"), a.rf_record_message

        files = sorted(out.glob("*.json"))
        assert len(files) == 1, files
        data = load_recording(files[0])
        assert data["schema"] == "rfeye-rf-series-v8", data["schema"]
        assert data["sample_count"] >= 2, data["sample_count"]
        # The acceptance limits in force at capture time travel with the file;
        # without them a recording cannot be re-judged later.
        assert "phy_min_snr_db" in data["capture_settings"]
        assert "phy_max_centre_error_hz" in data["capture_settings"]
        assert schema_mode(data) == "PHY v8", schema_mode(data)
        assert is_replayable(data) is True
        assert recording_label(data).startswith("20"), recording_label(data)

        entries = ctl._refresh_recordings(a)
        assert len(entries) == 1, entries
        assert entries[0]["path"] == str(files[0]), entries
        assert entries[0]["mode"] == "PHY v8", entries[0]

        _d, mode, results, _alerts = replay_recording(a.cfg, files[0])
        assert mode == "PHY v8", mode
        assert len(results) == data["sample_count"], (len(results),
                                                      data["sample_count"])
        assert results[0]["replayable"] is True
        assert results[0]["phy"] and results[0]["phy"][0]["ok"] is True
        assert results[0]["network_locked"] is True
    finally:
        a.backend.snapshot = real_snapshot
        ctl._capture_dir = keep_dir
        a.page = keep_page
        a.recording_entries = []
        if keep_duration is None:
            a.cfg.pop("rf_record_duration_s", None)
        else:
            a.cfg["rf_record_duration_s"] = keep_duration


def _check_demo_reopens_sdr():
    """Leaving demo mode has to give the radio a clean start.

    Demo used to hold the librtlsdr handle it was not reading and, on the way
    back out, inherit the USB reset counter, its back-off and the run of
    failures from whatever went wrong before demo was switched on. The panel
    then said SDR NOT CONNECTED with no way back except restarting the app.
    The close itself belongs to the scan thread -- closing a handle from the
    UI thread while the scan thread may be inside rtlsdr_read_sync() is the
    race stop() already avoids -- so the toggle only raises a request.
    """
    b = _REAL_BACKEND(dict(appmod.load_config()))
    b.sdr = object()
    b.sdr_path = "CTYPES PERSISTENT"
    b._usb_resets = 3
    b._usb_backoff = 60.0
    b.last_usb_reset = time.time()
    b.scan_failures = 9
    b.last_good_scan = time.time()

    assert b._sdr_reopen is False
    b.set_demo(True)
    assert b._sdr_reopen is True, "demo must not keep a radio it is not reading"
    assert b.sdr is not None, "and must not close it from this thread"
    b._sdr_reopen = False
    b.set_demo(True)
    assert b._sdr_reopen is False, "an unchanged setting asks for nothing"

    b.set_demo(False)
    assert b._sdr_reopen is True
    # What the scan loop does with the request.
    b._reopen_sdr()
    assert b.sdr is None and b.sdr_path == "UNOPENED"
    assert b._usb_resets == 0 and b._usb_backoff == 0.0
    assert b.last_usb_reset == 0.0
    assert b.scan_failures == 0
    assert b.cfg["demo_mode"] is False
    assert b.status == "SCANNING"

    # SCANNING is the scan thread on its way to its first dwell, at boot and
    # for the second after demo goes off. Reporting that as a broken dongle
    # was the first thing seen on leaving demo.
    from compact_ui_draw import _network_line, _sdr_fault
    for state in ("STARTING", "SCANNING"):
        text, _col = _network_line({}, state)
        assert text == "STARTING SCAN", (state, text)
    assert _network_line({}, "NO SDR")[0] == "SDR NOT CONNECTED"
    assert _network_line({"power_warning": "UNDER-VOLTAGE"},
                         "NO SDR")[0] == "SDR LOST - USB POWER LOW"
    # And the debug page separates a dongle that is gone from one that is
    # present and silent, which need opposite actions.
    assert _sdr_fault("SDR not on the USB bus") == "not on bus"
    assert _sdr_fault("librtlsdr read failed: rtlsdr_open failed") == "not on bus"
    assert _sdr_fault("librtlsdr read failed: read timeout") == "read timeout"
    assert _sdr_fault("") == ""


def _check_idle_frame_rate(a):
    """The idle rate is only for a screen nobody is looking at.

    Almost all of this appliance's life is the main page with nobody
    touching it, and drawing it is the largest single load on the Pi. Every
    reason someone might be watching -- a recent touch, another page, a
    notice, a recording, the detector changing its mind -- has to hold the
    active rate, or the saving is bought with response time.
    """
    keep_page = a.page
    keep_wake = a.ui_wake_at
    keep_mode = a.cfg.get("low_power_mode", True)
    keep_notice = a.power_notice_open
    try:
        a.cfg["low_power_mode"] = True
        active = int(a.cfg["low_power_ui_fps"])
        idle = int(a.cfg["low_power_idle_ui_fps"])
        assert 1 <= idle < active, (idle, active)

        a.page = "main"
        a.power_notice_open = False
        a.rf_recording = False
        a.ui_wake_at = time.monotonic() - appmod.UI_WAKE_S - 1.0
        assert a._fps() == idle, a._fps()

        a.ui_wake_at = time.monotonic()
        assert a._fps() == active, "a touch has to bring the rate back at once"

        a.ui_wake_at = time.monotonic() - appmod.UI_WAKE_S - 1.0
        a.page = "settings"
        assert a._fps() == active, "a menu is being read"
        a.page = "main"
        a.power_notice_open = True
        assert a._fps() == active, "a notice is waiting to be dismissed"
        a.power_notice_open = False
        a.rf_recording = True
        assert a._fps() == active, "a recording is counting down"
        a.rf_recording = False
        assert a._fps() == idle

        # An alert must not have to wait out an idle frame: the change in the
        # snapshot is itself the wake-up.
        a.ui_signature = None
        a._frame()
        assert a._fps() == active, "a change on screen wakes the loop"

        # Max power is unchanged by any of this.
        a.cfg["low_power_mode"] = False
        assert a._fps() == int(a.cfg["ui_fps"])
    finally:
        a.cfg["low_power_mode"] = keep_mode
        a.page = keep_page
        a.ui_wake_at = keep_wake
        a.power_notice_open = keep_notice
        a.rf_recording = False


def _check_dim_overlay_cached(a):
    """Dimming must be cheap, because it is not a saving.

    This panel has no backlight device -- /sys/class/backlight is empty and
    the overlay's led-gpios line sits unclaimed as an input -- so brightness
    is a black surface over the picture and the lamp burns the same either
    way. A setting that saves nothing must at least not cost a full-screen
    SRCALPHA allocation on every frame, which is what it did until 0.9.15.
    """
    keep = a.cfg.get("brightness", 1.0)
    try:
        a.cfg["brightness"] = 0.6
        a._apply_brightness()
        first = a._dim_overlay
        assert first is not None, "a dimmed screen still needs its overlay"
        a._apply_brightness()
        assert a._dim_overlay is first, "the overlay is built once, not per frame"
        # A new brightness has to produce a new overlay, not the stale one.
        a.cfg["brightness"] = 0.4
        a._apply_brightness()
        second = a._dim_overlay
        assert second is not first, "a changed setting has to be visible"
        assert a._dim_alpha == int((1.0 - 0.4) * 220), a._dim_alpha
        # Full brightness draws nothing at all.
        a.cfg["brightness"] = 1.0
        a._dim_overlay = None
        a._apply_brightness()
        assert a._dim_overlay is None, "no overlay when there is nothing to dim"
    finally:
        a.cfg["brightness"] = keep
        a._dim_overlay = None
        a._dim_alpha = -1


def _check_driver_selection():
    """The library is picked on what it supports, not on load order.

    Debian's librtlsdr 2.0.2 carries no "Blog V4L" code path, so on that
    dongle it never switches the tuner input or the upconverter GPIO and the
    whole UHF band reads as noise -- a fault with no symptom except an empty
    band. Measured on the reference unit, same dongle and air, alternating
    rounds at 390.7375 MHz: snr 0.5-0.8 dB / duty 0.00 / FAIL on the distro
    build, snr 11.7-13.4 / duty 1.00 / TETRA on the RTL-SDR Blog build. So
    which one gets loaded may not be left to the loader cache.
    """
    import ctypes.util

    d = Path(_tmp.name) / "libs"
    d.mkdir(exist_ok=True)
    plain = d / "librtlsdr-plain.so"
    blog = d / "librtlsdr-blog.so"
    plain.write_bytes(b"ELF Blog V4 RTLSDRBlog no newer model here")
    blog.write_bytes(b"ELF Blog V4 Blog V4L RTLSDRBlog")

    assert sdr_backend._library_knows_model(str(blog)) is True
    assert sdr_backend._library_knows_model(str(plain)) is False
    # A name that is not a path, or a file that is not there, must not throw.
    assert sdr_backend._library_knows_model("librtlsdr.so.0") is False
    assert sdr_backend._library_knows_model(str(d / "absent.so")) is False

    keep = os.environ.get("RFEYE_RTLSDR_LIB")
    keep_find = ctypes.util.find_library
    try:
        os.environ["RFEYE_RTLSDR_LIB"] = str(blog)
        first = sdr_backend.rtlsdr_library_candidates()[0]
        assert first == str(blog), first
        # /usr/local comes before whatever the loader cache answers, because
        # both are installed and the order there is not ours to rely on.
        ctypes.util.find_library = lambda n: "/usr/lib/librtlsdr.so.0"
        order = sdr_backend.rtlsdr_library_candidates()
        assert order.index("/usr/local/lib/librtlsdr.so.0") <             order.index("/usr/lib/librtlsdr.so.0"), order
        assert len(order) == len(set(order)), "no candidate is offered twice"
    finally:
        ctypes.util.find_library = keep_find
        if keep is None:
            os.environ.pop("RFEYE_RTLSDR_LIB", None)
        else:
            os.environ["RFEYE_RTLSDR_LIB"] = keep


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
    # Derived from the layout constants, not written out: the settings rows
    # have moved before and a hard-coded y silently taps the wrong feature.
    from compact_ui_draw import SETTINGS_TOP, SETTINGS_STEP, SETTINGS_HEIGHT
    _ROWS = ["demo_mode", "brightness", "low_power", "record_rf", "recordings",
             "wifi", "update", "debug"]
    def _row_y(name):
        return SETTINGS_TOP + _ROWS.index(name) * SETTINGS_STEP + SETTINGS_HEIGHT // 2
    # Brightness slider: both ends have to be reachable with a finger inside
    # the enclosure. At X1=298 the 100% end sat 22px from the glass, the
    # bezel stopped the finger first and the row topped out around 95%, so
    # the track is now centred with a margin on both sides.
    from compact_ui_draw import BRIGHT_SLIDER_X0, BRIGHT_SLIDER_X1
    assert BRIGHT_SLIDER_X0 >= 40, BRIGHT_SLIDER_X0
    assert BRIGHT_SLIDER_X1 <= 280, BRIGHT_SLIDER_X1
    assert BRIGHT_SLIDER_X0 + BRIGHT_SLIDER_X1 == 320, "the track stays centred"
    keep_brightness = a.cfg.get("brightness", 1.0)
    time.sleep(0.15)
    a._tap(BRIGHT_SLIDER_X1, _row_y("brightness"))
    assert abs(float(a.cfg["brightness"]) - 1.0) < 1e-6, a.cfg["brightness"]
    time.sleep(0.15)
    a._tap(BRIGHT_SLIDER_X0, _row_y("brightness"))
    assert abs(float(a.cfg["brightness"]) - 0.4) < 1e-6, a.cfg["brightness"]
    a.cfg["brightness"] = keep_brightness

    # Power mode: the row flips the setting, the frame rate follows it live,
    # and the same tap goes looking for the SDR again -- the reason to reach
    # for this setting is a dongle that has dropped off a marginal supply, so
    # needing a reboot to find out whether it helped would make it useless.
    assert a.cfg.get("low_power_mode") is True, "eco is the shipped default"
    assert a._fps() == int(a.cfg["low_power_ui_fps"])
    time.sleep(0.15)
    a._tap(40, _row_y("low_power"))
    assert a.cfg["low_power_mode"] is False
    assert a._fps() == int(a.cfg["ui_fps"])
    # The switch reports back on the panel; by now the USB check may already
    # have replaced the acknowledgement with its own result, and either is fine.
    assert str(a.low_power_message or "").strip(), "the switch has to say something"
    assert float(a.low_power_message_until) > time.monotonic()
    time.sleep(0.15)
    a._tap(40, _row_y("low_power"))
    assert a.cfg["low_power_mode"] is True
    for _ in range(50):
        if getattr(a.backend, "reinit_calls", 0) >= 2:
            break
        time.sleep(0.05)
    assert getattr(a.backend, "reinit_calls", 0) >= 2, "each switch re-checks USB"
    # A live under-voltage takes max power back off, even after the notice has
    # already been seen and dismissed once: it is a change the user asked for
    # and did not make, so it has to be said out loud.
    a.cfg["low_power_mode"] = False
    a.power_notice_done = True
    a.power_notice_open = False
    a._power_notice_update({"power_warning": "UNDER-VOLTAGE",
                            "power_detail": "core 1.2563V, throttled 0xd0005"})
    assert a.cfg["low_power_mode"] is True, "a sagging rail drops max power"
    assert a.power_notice_open is True, "and says so, dismissed or not"
    assert any("Max power" in line for line in a.power_notice_lines), a.power_notice_lines
    a.power_notice_open = False
    a.power_notice_done = True
    # With the load already backed off there is nothing left to revert, so a
    # dismissed notice stays dismissed.
    a._power_notice_update({"power_warning": "UNDER-VOLTAGE"})
    assert a.power_notice_open is False
    # Leave the notice as the later check expects to find it.
    a.power_notice_done = False
    a.power_notice_lines = []

    a.page = "settings"

    time.sleep(0.15)
    a._tap(40, _row_y("record_rf"))
    assert a.page=="record_confirm", a.page
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
    _check_frame_guard(a)
    _check_recording_roundtrip(a)
    _check_idle_frame_rate(a)
    _check_dim_overlay_cached(a)
    _check_demo_reopens_sdr()
    _check_driver_selection()

    a.running=False
    a.backend.stop()
    a.buzzer.close()
    pygame.quit()
    _tmp.cleanup()
    print("RF Eye headless app self-test: OK")


if __name__=="__main__":
    main()
