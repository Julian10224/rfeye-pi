"""Compact RF Eye drawing helpers for the CUQI 3.5-inch 320x480 portrait UI."""
BG=(2,3,5); PANEL=(8,9,12); BLUE=(0,152,222); BLUE_BRIGHT=(28,190,255)
WHITE=(224,229,236); DIM=(82,90,100); SEG_OFF=(34,35,31); GREEN=(57,205,91)
YELLOW=(243,192,56); RED=(230,54,54)

SETTINGS_TOP=66
SETTINGS_STEP=48
SETTINGS_HEIGHT=44
SETTINGS_COUNT=7
BRIGHT_SLIDER_X0=118
BRIGHT_SLIDER_X1=298

def _clamp(v, lo=0.0, hi=1.0): return max(lo,min(hi,v))

def draw_gear(app,cx,cy,size=42):
    import math, pygame
    scale=4; side=max(48,int(size*scale)); icon=pygame.Surface((side,side),pygame.SRCALPHA)
    cc=side//2; teeth=10; outer=size*0.48*scale; root=size*0.34*scale; pts=[]
    for i in range(teeth*4):
        a=-math.pi/2+i*math.pi/(teeth*2); r=outer if i%4 in (1,2) else root
        pts.append((cc+int(math.cos(a)*r),cc+int(math.sin(a)*r)))
    pygame.draw.polygon(icon,BLUE_BRIGHT,pts)
    pygame.draw.circle(icon,BLUE_BRIGHT,(cc,cc),int(size*0.30*scale))
    pygame.draw.circle(icon,(0,0,0,0),(cc,cc),int(size*0.115*scale))
    icon=pygame.transform.smoothscale(icon,(int(size),int(size)))
    app.ui.blit(icon,icon.get_rect(center=(int(cx),int(cy))))

def draw_main(app, snap):
    import pygame
    app.ui.fill(BG)
    app._text("RF EYE",160,22,app.font_l,BLUE_BRIGHT,center=True)
    status=snap["status"]; scol=GREEN if status=="LIVE" else BLUE if status=="DEMO" else RED
    pygame.draw.circle(app.ui,scol,(294,22),5); app._gear(34,49,30)
    stxt,scol=("SDR CONNECTED",GREEN) if status=="LIVE" else (("SDR DEMO",BLUE_BRIGHT) if status=="DEMO" else ("SDR NOT CONNECTED",RED))
    app._text(stxt,160,52,app.font_s,scol,center=True)
    peaks=list(snap["peaks"][:3])
    while len(peaks)<3: peaks.append({"level":0.0,"freq_hz":0.0})
    if not hasattr(app,"_last_peak_freqs"):
        app._last_peak_freqs=[381_000_000.0,382_500_000.0,384_000_000.0]
    for col,p in enumerate(peaks):
        x=[16,112,208][col]; level=_clamp(float(p.get("level",0.0))); active=int(round(level*10))
        for i in range(10):
            yy=90+(9-i)*21; color=app._level_color(i,10) if i<active else SEG_OFF
            pygame.draw.rect(app.ui,color,(x,yy,80,17),border_radius=3)
        if app.cfg.get("show_frequency",True):
            freq=float(p.get("freq_hz",0.0) or 0.0)
            if freq>0.0 and level>=0.15:
                app._last_peak_freqs[col]=freq
            shown=app._last_peak_freqs[col]
            app._text(f'{shown/1e6:.3f}',x+40,322,app.font_s,(132,184,210),center=True)
            app._text("MHz",x+40,342,app.font_s,DIM,center=True)
    lv=float(snap.get("mobile_level",0.0))
    if status not in ("LIVE","DEMO"): state,col="NO SDR",RED
    elif lv>0.72: state,col="HIGH",RED
    elif lv>0.43: state,col="MEDIUM",YELLOW
    elif lv>0.15: state,col="LOW",GREEN
    else: state,col="CLEAR",DIM
    pygame.draw.rect(app.ui,PANEL,(0,360,320,120))
    for rect in [(8,374,94,90),(112,374,96,90),(218,374,94,90)]: pygame.draw.rect(app.ui,(17,20,25),rect,border_radius=11)
    app._text("MUTED" if app.cfg.get("muted") else "SOUND",55,405,app.font_m,RED if app.cfg.get("muted") else BLUE_BRIGHT,center=True)
    app._text("tap",55,438,app.font_s,DIM,center=True)
    app._text("SPECTRUM",160,405,app.font_s,WHITE,center=True); app._text("open",160,438,app.font_s,DIM,center=True)
    app._text(state,265,405,app.font_m if len(state)<7 else app.font_s,col,center=True); app._text("status",265,438,app.font_s,DIM,center=True)

def draw_settings(app):
    import pygame, time
    app.ui.fill(BG); pygame.draw.rect(app.ui,(7,11,16),(0,0,320,60))
    app._text("‹",18,26,app.font_xl,BLUE_BRIGHT,center=True); app._text("SETTINGS",48,12,app.font_l,WHITE)
    app._text(f'RF EYE v{app.cfg.get("app_version","")}',50,39,app.font_s,DIM)
    rows=[
        ("Demo","ON" if app.cfg.get("demo_mode") else "OFF","toggle"),
        ("Brightness",f'{int(round(float(app.cfg.get("brightness",1.0))*100))}%',"brightness_slider"),
        ("Record RF",(f'REC {max(0,int(round(float(getattr(app,"rf_record_end",0.0))-time.monotonic())))}s' if bool(getattr(app,"rf_recording",False)) else (getattr(app,"rf_record_message","SAVE") if time.monotonic()<float(getattr(app,"rf_record_message_until",0.0)) else "SAVE")),"action"),
        ("Wi-Fi",app._wifi_text(),"status"),
        ("Update",app.update_message,"action"),
        ("Spectrum","OPEN","action"),
        ("Debug","OPEN","action"),
    ]
    for i,(label,value,kind) in enumerate(rows):
        y=SETTINGS_TOP+i*SETTINGS_STEP
        pygame.draw.rect(app.ui,(9,13,18),(8,y,304,SETTINGS_HEIGHT),border_radius=8)
        if kind=="brightness_slider":
            app._text(label,18,y+4,app.font_s,WHITE)
            surf=app.font_s.render(str(value),True,(150,201,226)); app.ui.blit(surf,(302-surf.get_width(),y+4))
            lo,hi=0.4,1.0; val=max(lo,min(hi,float(app.cfg.get("brightness",1.0)))); x0,x1=BRIGHT_SLIDER_X0,BRIGHT_SLIDER_X1
            n=0.0 if hi<=lo else (val-lo)/(hi-lo); sy=y+29; sx=x0+int(round(n*(x1-x0)))
            pygame.draw.line(app.ui,(38,43,49),(x0,sy),(x1,sy),5); pygame.draw.line(app.ui,BLUE,(x0,sy),(sx,sy),5); pygame.draw.circle(app.ui,WHITE,(sx,sy),7)
            continue
        app._text(label,18,y+13,app.font_s,WHITE)
        if kind=="toggle":
            enabled=value=="ON"; pygame.draw.rect(app.ui,BLUE if enabled else (38,43,49),(252,y+11,50,24),border_radius=12); pygame.draw.circle(app.ui,WHITE,(289 if enabled else 265,y+23),9)
        else:
            col=GREEN if kind=="status" and value=="CONNECTED" else (RED if kind=="status" else BLUE_BRIGHT if kind=="action" else (150,201,226))
            shown=str(value); shown=shown if len(shown)<=13 else shown[:12]+"…"
            surf=app.font_s.render(shown,True,col); app.ui.blit(surf,(302-surf.get_width(),y+13))
    app._text("Made by: Julian",160,463,app.font_s,DIM,center=True)

def draw_debug(app,snap):
    import pygame, time
    app.ui.fill(BG); pygame.draw.rect(app.ui,(7,11,16),(0,0,320,60))
    app._text("‹",18,26,app.font_xl,BLUE_BRIGHT,center=True); app._text("DEBUG",48,10,app.font_l,WHITE)
    app._text("LIVE PERFORMANCE",50,38,app.font_s,DIM)
    age_ms=max(0.0,(time.time()-float(snap.get("last_update",0.0)))*1000.0) if snap.get("last_update") else 0.0
    frame_ms=max(0.001,float(getattr(app,"debug_frame_ms",0.0) or 0.001))
    rows=[("UI refresh",f"{frame_ms:.1f} ms / {1000.0/frame_ms:.1f} FPS"),("Data age",f"{age_ms:.0f} ms"),("Full cycle",f"{float(snap.get('cycle_ms',0)):.0f} ms"),("Mobile sweep",f"{float(snap.get('mobile_scan_ms',0)):.0f} ms"),("Site sweep",f"{float(snap.get('site_scan_ms',0)):.0f} ms"),("Last capture",f"{float(snap.get('capture_ms',0)):.0f} ms"),("Tune windows",str(int(snap.get("scan_windows",0)))),("SDR path",str(snap.get("sdr_path","?"))),("Backend",str(snap.get("status","?")))]
    y=66
    for label,value in rows:
        pygame.draw.rect(app.ui,(9,13,18),(8,y,304,34),border_radius=7); app._text(label,16,y+5,app.font_s,DIM)
        shown=value if len(value)<=20 else value[:19]+"…"; col=GREEN if label=="Backend" and value=="LIVE" else BLUE_BRIGHT
        surf=app.font_s.render(shown,True,col); app.ui.blit(surf,(304-surf.get_width(),y+17)); y+=38
    pygame.draw.rect(app.ui,(12,24,32),(8,412,304,34),border_radius=8)
    app._text("Touch calibration",18,421,app.font_s,WHITE)
    surf=app.font_s.render("CALIBRATE",True,BLUE_BRIGHT); app.ui.blit(surf,(302-surf.get_width(),421))
    app._text("tap top/bottom to return",160,465,app.font_s,DIM,center=True)

def draw_record_confirm(app):
    import pygame
    app.ui.fill(BG)
    app._text("RF RECORDING",160,54,app.font_l,BLUE_BRIGHT,center=True)
    app._text("Weet je het zeker?",160,112,app.font_m,WHITE,center=True)
    app._text("Een opname wordt als testdata opgeslagen.",160,151,app.font_s,DIM,center=True)
    app._text("Start alleen als dit bewust de juiste situatie is.",160,177,app.font_s,DIM,center=True)
    pygame.draw.rect(app.ui,(34,39,46),(12,215,296,112),border_radius=16)
    pygame.draw.rect(app.ui,(12,91,132),(12,350,296,105),border_radius=16)
    app._text("NEE",160,258,app.font_l,WHITE,center=True)
    app._text("Annuleren",160,292,app.font_s,DIM,center=True)
    app._text("JA",160,388,app.font_l,WHITE,center=True)
    app._text("Opname starten",160,421,app.font_s,WHITE,center=True)

def draw_calibration(app):
    import pygame
    from compact_ui_controls import CALIBRATION_TARGETS
    app.ui.fill(BG)
    app._text("TOUCH CALIBRATION",160,22,app.font_l,BLUE_BRIGHT,center=True)
    idx=int(getattr(app,'touch_calibration_index',0)); done=bool(getattr(app,'touch_calibration_complete',False))
    if done:
        ok='FAILED' not in str(getattr(app,'touch_calibration_message',''))
        app._text(getattr(app,'touch_calibration_message','CALIBRATION APPLIED'),160,190,app.font_m,GREEN if ok else RED,center=True)
        app._text("New calibration is active",160,224,app.font_s,WHITE,center=True)
        app._text("Tap anywhere to return",160,270,app.font_s,DIM,center=True)
        return
    app._text(f"Point {idx+1} of {len(CALIBRATION_TARGETS)}",160,54,app.font_s,DIM,center=True)
    app._text("Tap the center of the cross",160,77,app.font_s,WHITE,center=True)
    tx,ty=CALIBRATION_TARGETS[min(idx,len(CALIBRATION_TARGETS)-1)]
    pygame.draw.circle(app.ui,BLUE_BRIGHT,(tx,ty),13,2); pygame.draw.line(app.ui,BLUE_BRIGHT,(tx-18,ty),(tx+18,ty),2); pygame.draw.line(app.ui,BLUE_BRIGHT,(tx,ty-18),(tx,ty+18),2); pygame.draw.circle(app.ui,WHITE,(tx,ty),3)
    app._text("Use a precise fingertip/stylus",160,466,app.font_s,DIM,center=True)

def draw_spectrum(app,snap):
    import pygame
    app.ui.fill(BG); app._text("‹",18,26,app.font_xl,BLUE_BRIGHT,center=True); app._text("SPECTRUM",160,24,app.font_l,BLUE_BRIGHT,center=True)
    plot=pygame.Rect(12,72,296,188); pygame.draw.rect(app.ui,(7,8,10),plot); pygame.draw.rect(app.ui,(45,48,54),plot,1)
    spectrum=snap["spectrum"]
    if len(spectrum)>2:
        pmin=float(snap["noise"])-12; pmax=max(max(float(v) for v in spectrum),pmin+45); pts=[]
        for i,v in enumerate(spectrum):
            x=plot.left+int(i*(plot.width-1)/(len(spectrum)-1)); n=_clamp((float(v)-pmin)/(pmax-pmin)); y=plot.bottom-int(n*(plot.height-1)); pts.append((x,y))
        if len(pts)>1: pygame.draw.lines(app.ui,BLUE_BRIGHT,False,pts,1)
    lf=app.cfg.get("mobile_band_start_hz",app.cfg["scan_start_hz"])/1e6; rf=app.cfg.get("mobile_band_end_hz",app.cfg["scan_end_hz"])/1e6
    app._text(f"{lf:.3f}",12,268,app.font_s,DIM); label=f"{rf:.3f} MHz"; surf=app.font_s.render(label,True,DIM); app.ui.blit(surf,(308-surf.get_width(),268))
    app._text(f'Noise floor {snap["noise"]:.1f} dB',160,300,app.font_m,WHITE,center=True)
    peaks=list(snap["peaks"][:3]); y=332
    if not peaks: app._text("No transient RF activity",160,360,app.font_s,DIM,center=True)
    for i,p in enumerate(peaks):
        pygame.draw.rect(app.ui,(9,13,18),(12,y,296,34),border_radius=6); app._text(f'{i+1}. {p["freq_hz"]/1e6:.5f} MHz',20,y+9,app.font_s,WHITE)
        app._text(f'{int(p["level"]*100)}%',286,y+17,app.font_s,BLUE_BRIGHT,center=True); y+=42
    app._text("tap top/bottom to return",160,463,app.font_s,DIM,center=True)
