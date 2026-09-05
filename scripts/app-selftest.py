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
            "status":"LIVE","error":"","peaks":[],"mobile_peaks":[],"site_peaks":[],
            "mobile_level":0.0,"site_level":0.4,"activity_confidence":0.0,
            "mobile_confirmed":False,"freqs":[],"spectrum":[],"noise":-100.0,
            "last_update":time.time(),"demo":False,"cycle_ms":1000.0,
            "mobile_scan_ms":800.0,"site_scan_ms":0.0,"capture_ms":60.0,
            "scan_windows":3,"confirm_streak":0,"clear_streak":0,
            "broadband_rejected":False,"static_rejected":False,"comb_rejected":False,
            "artifact_calibrating":False,"artifact_sweep":5,
            "artifact_baseline_count":27,"artifact_baseline_loaded":True,
            "artifact_tainted_count":0,"raw_mobile_candidate_count":0,
            "post_artifact_candidate_count":0,"post_comb_candidate_count":0,
            "coherent_comb_rejected_count":0,"comb_event_teeth":0,
            "comb_profile_support":23,"comb_profile_teeth":8,
            "comb_profile_phase_hz":380387500.0,"broadband_kept_count":0,
            "novelty_rejected_count":0,"pair_rejected_count":0,
            "confidence_rejected_count":0,"debug_mobile_candidates":[],
            "sdr_path":"TEST",
        }


class FakeBuzzer:
    def __init__(self,*args,**kwargs): pass
    def close(self): pass
    def off(self): pass
    def beep_pattern(self,*args,**kwargs): pass


sdr_backend.SDRBackend=FakeBackend
buzzer.GPIOBuzzer=FakeBuzzer

import app as appmod
from config import load_config
appmod.GPIOBuzzer=FakeBuzzer


def main():
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

    a.running=False
    a.backend.stop()
    a.buzzer.close()
    pygame.quit()
    _tmp.cleanup()
    print("RF Eye headless app self-test: OK")


if __name__=="__main__":
    main()
