"""Native 320x480 portrait profile for the CUQI 3.5-inch 480x320 panel."""
from __future__ import annotations
import builtins, os


def _patch_app_class(cls):
    from compact_ui_controls import tap
    from compact_ui_draw import (draw_main, draw_settings, draw_spectrum, draw_debug,
                                 draw_calibration, draw_record_confirm, draw_recordings,
                                 draw_recording_detail, draw_recording_delete_confirm,
                                 draw_recording_replay, draw_gear)
    from compact_wifi_ui import draw as draw_wifi, draw_keyboard, key_at, rows
    from compact_touch import install as install_touch, drain as drain_touch
    old_init=cls.__init__; old_events=cls._events
    def init(self,*args,**kwargs):
        old_init(self,*args,**kwargs)
        import pygame
        self.display_profile="cuqi35"
        # Enforce native CUQI geometry even when an older config still contains
        # the original Elecrow 800x480 / 480x800 dimensions.
        if (self.uw,self.uh,self.pw,self.ph)!=(320,480,480,320):
            self.uw,self.uh,self.pw,self.ph=320,480,480,320
            self.screen=pygame.display.set_mode((480,320),pygame.NOFRAME)
            self.ui=pygame.Surface((320,480))
        self.font_s=pygame.font.Font(None,17); self.font_m=pygame.font.Font(None,21)
        self.font_l=pygame.font.Font(None,28); self.font_xl=pygame.font.Font(None,38)
        self.settings_icon=None
        self.touch_calibration_samples=[]; self.touch_calibration_index=0; self.touch_calibration_complete=False
        self.recording_entries=None; self.recording_offset=0; self.recording_selected=None
        self.recording_replay_running=False; self.recording_replay_finished=False
        self.recording_replay_stop=False; self.recording_replay_generation=0; self.recording_replay_snapshot=None
        self.recording_replay_index=0; self.recording_replay_total=0; self.recording_replay_alerts=0
        self.recording_replay_mode=""; self.recording_replay_error=""
        install_touch(self)
    def events(self):
        import pygame
        # XPT2046 is read directly from evdev.  SDL/Wayland also synthesizes
        # mouse/finger events for the same resistive tap, but its coordinates
        # are in the unrotated SPI output space.  Feeding both paths caused one
        # tap to hit two unrelated controls (often Spectrum), so keep only
        # non-pointer SDL events and use the calibrated direct touch path.
        blocked={pygame.MOUSEMOTION,pygame.MOUSEBUTTONDOWN,pygame.MOUSEBUTTONUP,
                 pygame.FINGERDOWN,pygame.FINGERMOTION,pygame.FINGERUP}
        saved=[e for e in pygame.event.get() if e.type not in blocked]
        for e in saved:
            pygame.event.post(e)
        old_events(self)
        drain_touch(self)
    def present(self):
        import pygame
        out=pygame.transform.rotate(self.ui,90 if self.cfg.get("rotation","cw")=="ccw" else -90)
        target=(int(self.pw),int(self.ph))
        if out.get_size()!=target: out=pygame.transform.smoothscale(out,target)
        self.screen.blit(out,(0,0))
    def physical_to_ui(self,px,py):
        pw=max(2,int(self.pw)); ph=max(2,int(self.ph)); nx=max(0,min(1,float(px)/(pw-1))); ny=max(0,min(1,float(py)/(ph-1)))
        if self.cfg.get("rotation","cw")=="ccw": ux=(1-ny)*(self.uw-1); uy=nx*(self.uh-1)
        else: ux=ny*(self.uw-1); uy=(1-nx)*(self.uh-1)
        ux=max(0,min(self.uw-1,int(round(ux)))); uy=max(0,min(self.uh-1,int(round(uy))))
        if self.cfg.get("touch_invert_x",False): ux=self.uw-1-ux
        if self.cfg.get("touch_invert_y",False): uy=self.uh-1-uy
        return ux,uy
    cls.__init__=init; cls._events=events; cls._present_rotated=present; cls._physical_to_ui=physical_to_ui; cls._tap=tap; cls._gear=draw_gear
    cls._draw_main=draw_main; cls._draw_settings=draw_settings; cls._draw_spectrum=draw_spectrum; cls._draw_debug=draw_debug; cls._draw_calibration=draw_calibration; cls._draw_record_confirm=draw_record_confirm
    cls._draw_recordings=draw_recordings; cls._draw_recording_detail=draw_recording_detail
    cls._draw_recording_delete_confirm=draw_recording_delete_confirm; cls._draw_recording_replay=draw_recording_replay
    cls._compact_wifi_rows=rows; cls._wifi_key_at=key_at; cls._draw_wifi_keyboard=draw_keyboard; cls._draw_wifi=draw_wifi
    return cls


def install_app_patch():
    if os.getenv("RFEYE_DISPLAY_PROFILE","").lower()!="cuqi35": return
    original=builtins.__build_class__
    if getattr(original,"_rfeye_compact_display_patch",False): return
    def wrapper(func,name,*bases,**kwargs):
        cls=original(func,name,*bases,**kwargs)
        if name=="App" and getattr(cls,"__module__","") in {"__main__","app"}:
            _patch_app_class(cls)
            if builtins.__build_class__ is wrapper: builtins.__build_class__=original
        return cls
    wrapper._rfeye_compact_display_patch=True; builtins.__build_class__=wrapper
