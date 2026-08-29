"""Native 320x480 portrait profile for the CUQI/MHS35 3.5-inch panel."""
from __future__ import annotations
import builtins, os


def _patch_app_class(cls):
    from compact_ui_controls import tap
    from compact_ui_draw import draw_main, draw_settings, draw_spectrum, draw_debug, draw_gear
    from compact_wifi_ui import draw as draw_wifi, draw_keyboard, key_at, rows
    from compact_touch import install as install_touch, drain as drain_touch
    old_init=cls.__init__
    old_events=cls._events

    def init(self,*args,**kwargs):
        # Force the compact geometry BEFORE App.__init__.  The startup splash is
        # drawn inside the original init, so correcting dimensions afterwards
        # is too late and can produce a partially off-screen second splash.
        cfg=args[0] if args else kwargs.get('cfg')
        if isinstance(cfg,dict):
            cfg.update({
                'ui_width':320,'ui_height':480,
                'physical_width':480,'physical_height':320,
                'display_profile':'cuqi35',
            })
        old_init(self,*args,**kwargs)
        import pygame
        self.display_profile='cuqi35'
        self.font_s=pygame.font.Font(None,17); self.font_m=pygame.font.Font(None,21)
        self.font_l=pygame.font.Font(None,28); self.font_xl=pygame.font.Font(None,38)
        # The old PNG is not used on this display; the compact UI draws a
        # supersampled vector gear so there is no libpng failure or blur.
        self.settings_icon=None
        install_touch(self)

    def events(self):
        import pygame
        # The XPT2046 is read directly from evdev. SDL/Wayland also synthesizes
        # pointer events for the same tap in unrotated SPI coordinates. Feeding
        # both paths caused a single tap to activate unrelated controls.
        blocked={pygame.MOUSEMOTION,pygame.MOUSEBUTTONDOWN,pygame.MOUSEBUTTONUP,
                 pygame.FINGERDOWN,pygame.FINGERMOTION,pygame.FINGERUP}
        saved=[e for e in pygame.event.get() if e.type not in blocked]
        for e in saved:
            pygame.event.post(e)
        old_events(self)
        drain_touch(self)

    def present(self):
        import pygame
        out=pygame.transform.rotate(self.ui,90 if self.cfg.get('rotation','cw')=='ccw' else -90)
        target=(int(self.pw),int(self.ph))
        if out.get_size()!=target: out=pygame.transform.smoothscale(out,target)
        self.screen.blit(out,(0,0))

    def physical_to_ui(self,px,py):
        pw=max(2,int(self.pw)); ph=max(2,int(self.ph))
        nx=max(0,min(1,float(px)/(pw-1))); ny=max(0,min(1,float(py)/(ph-1)))
        if self.cfg.get('rotation','cw')=='ccw': ux=(1-ny)*(self.uw-1); uy=nx*(self.uh-1)
        else: ux=ny*(self.uw-1); uy=(1-nx)*(self.uh-1)
        ux=max(0,min(self.uw-1,int(round(ux)))); uy=max(0,min(self.uh-1,int(round(uy))))
        if self.cfg.get('touch_invert_x',False): ux=self.uw-1-ux
        if self.cfg.get('touch_invert_y',False): uy=self.uh-1-uy
        return ux,uy

    cls.__init__=init; cls._events=events; cls._present_rotated=present
    cls._physical_to_ui=physical_to_ui; cls._tap=tap; cls._gear=draw_gear
    cls._draw_main=draw_main; cls._draw_settings=draw_settings
    cls._draw_spectrum=draw_spectrum; cls._draw_debug=draw_debug
    cls._compact_wifi_rows=rows; cls._wifi_key_at=key_at
    cls._draw_wifi_keyboard=draw_keyboard; cls._draw_wifi=draw_wifi
    return cls


def install_app_patch():
    if os.getenv('RFEYE_DISPLAY_PROFILE','').lower()!='cuqi35': return
    original=builtins.__build_class__
    if getattr(original,'_rfeye_compact_display_patch',False): return
    def wrapper(func,name,*bases,**kwargs):
        cls=original(func,name,*bases,**kwargs)
        if name=='App' and getattr(cls,'__module__','') in {'__main__','app'}:
            _patch_app_class(cls)
            if builtins.__build_class__ is wrapper: builtins.__build_class__=original
        return cls
    wrapper._rfeye_compact_display_patch=True
    builtins.__build_class__=wrapper
