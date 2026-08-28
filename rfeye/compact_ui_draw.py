"""Compact RF Eye drawing helpers for the CUQI 3.5-inch 320x480 portrait UI."""
BG=(2,3,5); PANEL=(8,9,12); BLUE=(0,152,222); BLUE_BRIGHT=(28,190,255)
WHITE=(224,229,236); DIM=(82,90,100); SEG_OFF=(34,35,31); GREEN=(57,205,91)
YELLOW=(243,192,56); RED=(230,54,54)

SETTINGS_TOP=66
SETTINGS_STEP=39
SETTINGS_HEIGHT=34
SETTINGS_COUNT=10

def _clamp(v, lo=0.0, hi=1.0): return max(lo,min(hi,v))

def draw_main(app, snap):
    import pygame
    app.ui.fill(BG)
    app._text("RF EYE",160,22,app.font_l,BLUE_BRIGHT,center=True)
    status=snap["status"]; scol=GREEN if status=="LIVE" else BLUE if status=="DEMO" else RED
    pygame.draw.circle(app.ui,scol,(294,22),5); app._gear(286,49,28)
    stxt,scol=("SDR CONNECTED",GREEN) if status=="LIVE" else (("SDR DEMO",BLUE_BRIGHT) if status=="DEMO" else ("SDR NOT CONNECTED",RED))
    app._text(stxt,160,52,app.font_s,scol,center=True)
    peaks=list(snap["peaks"][:3])
    while len(peaks)<3: peaks.append({"level":0.0,"freq_hz":0.0})
    for col,p in enumerate(peaks):
        x=[16,112,208][col]; level=_clamp(float(p.get("level",0.0))); active=int(round(level*10))
        for i in range(10):
            yy=88+(9-i)*19; color=app._level_color(i,10) if i<active else SEG_OFF
            pygame.draw.rect(app.ui,color,(x,yy,80,15),border_radius=2)
        if app.cfg.get("show_frequency",True):
            label=f'{p["freq_hz"]/1e6:.3f}' if p.get("freq_hz",0) else "---.---"
            app._text(label,x+40,285,app.font_s,(132,184,210) if p.get("freq_hz",0) else DIM,center=True)
            app._text("MHz",x+40,302,app.font_s,DIM,center=True)
    lv=float(snap.get("mobile_level",0.0))
    if status not in ("LIVE","DEMO"): state,col="NO SDR",RED
    elif lv>0.72: state,col="HIGH",RED
    elif lv>0.43: state,col="MEDIUM",YELLOW
    elif lv>0.15: state,col="LOW",GREEN
    else: state,col="CLEAR",DIM
    pygame.draw.rect(app.ui,PANEL,(0,326,320,154))
    for rect in [(8,344,94,82),(112,344,96,82),(218,344,94,82)]: pygame.draw.rect(app.ui,(17,20,25),rect,border_radius=10)
    app._text("MUTED" if app.cfg.get("muted") else "SOUND",55,373,app.font_m,RED if app.cfg.get("muted") else BLUE_BRIGHT,center=True)
    app._text("tap",55,401,app.font_s,DIM,center=True)
    app._text("SPECTRUM",160,373,app.font_s,WHITE,center=True); app._text("open",160,401,app.font_s,DIM,center=True)
    app._text(state,265,373,app.font_m if len(state)<7 else app.font_s,col,center=True); app._text("status",265,401,app.font_s,DIM,center=True)
    app._text(f'Noise {snap["noise"]:.0f} dB',160,454,app.font_s,DIM,center=True)

def draw_settings(app):
    import pygame
    app.ui.fill(BG); pygame.draw.rect(app.ui,(7,11,16),(0,0,320,60))
    app._text("‹",18,26,app.font_xl,BLUE_BRIGHT,center=True); app._text("SETTINGS",48,12,app.font_l,WHITE)
    app._text(f'RF EYE v{app.cfg.get("app_version","")}',50,39,app.font_s,DIM)
    rows=[
        ("Sound","MUTED" if app.cfg.get("muted") else "ON","toggle"),
        ("Demo","ON" if app.cfg.get("demo_mode") else "OFF","toggle"),
        ("Sensitivity",f'{app.cfg.get("threshold_db",12):.0f} dB',"value"),
        ("Audio mode",app.cfg.get("audio_mode","adaptive").upper(),"value"),
        ("Brightness",f'{int(app.cfg.get("brightness",1.0)*100)}%',"value"),
        ("Freq labels","ON" if app.cfg.get("show_frequency") else "OFF","toggle"),
        ("Wi-Fi",app._wifi_text(),"status"),
        ("Update",app.update_message,"action"),
        ("Spectrum","OPEN","action"),
        ("Debug","OPEN","action"),
    ]
    for i,(label,value,kind) in enumerate(rows):
        y=SETTINGS_TOP+i*SETTINGS_STEP
        pygame.draw.rect(app.ui,(9,13,18),(8,y,304,SETTINGS_HEIGHT),border_radius=7)
        app._text(label,18,y+9,app.font_s,WHITE)
        if kind=="toggle":
            enabled=value=="ON"; pygame.draw.rect(app.ui,BLUE if enabled else (38,43,49),(264,y+7,38,20),border_radius=10)
            pygame.draw.circle(app.ui,WHITE,(292 if enabled else 274,y+17),7)
        else:
            col=GREEN if kind=="status" and value=="CONNECTED" else (RED if kind=="status" else BLUE_BRIGHT if kind=="action" else (150,201,226))
            shown=str(value); shown=shown if len(shown)<=13 else shown[:12]+"…"
            surf=app.font_s.render(shown,True,col); app.ui.blit(surf,(302-surf.get_width(),y+9))

def draw_debug(app,snap):
    import pygame, time
    app.ui.fill(BG); pygame.draw.rect(app.ui,(7,11,16),(0,0,320,60))
    app._text("‹",18,26,app.font_xl,BLUE_BRIGHT,center=True); app._text("DEBUG",48,10,app.font_l,WHITE)
    app._text("LIVE PERFORMANCE",50,38,app.font_s,DIM)
    age_ms=max(0.0,(time.time()-float(snap.get("last_update",0.0)))*1000.0) if snap.get("last_update") else 0.0
    frame_ms=max(0.001,float(getattr(app,"debug_frame_ms",0.0) or 0.001))
    rows=[
        ("UI refresh",f"{frame_ms:.1f} ms / {1000.0/frame_ms:.1f} FPS"),
        ("Data age",f"{age_ms:.0f} ms"),
        ("Full cycle",f"{float(snap.get('cycle_ms',0)):.0f} ms"),
        ("Mobile sweep",f"{float(snap.get('mobile_scan_ms',0)):.0f} ms"),
        ("Site sweep",f"{float(snap.get('site_scan_ms',0)):.0f} ms"),
        ("Last capture",f"{float(snap.get('capture_ms',0)):.0f} ms"),
        ("Tune windows",str(int(snap.get("scan_windows",0)))),
        ("SDR path",str(snap.get("sdr_path","?"))),
        ("Backend",str(snap.get("status","?"))),
    ]
    y=66
    for label,value in rows:
        pygame.draw.rect(app.ui,(9,13,18),(8,y,304,34),border_radius=7)
        app._text(label,16,y+5,app.font_s,DIM)
        shown=value if len(value)<=20 else value[:19]+"…"
        col=GREEN if label=="Backend" and value=="LIVE" else BLUE_BRIGHT
        surf=app.font_s.render(shown,True,col); app.ui.blit(surf,(304-surf.get_width(),y+17))
        y+=38
    err=str(snap.get("error","")).strip()
    if err:
        app._text("ERR "+err[-35:],160,421,app.font_s,RED,center=True)
    app._text("tap top/bottom to return",160,463,app.font_s,DIM,center=True)

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
        threshold=float(snap["noise"])+float(app.cfg.get("threshold_db",10)); n=_clamp((threshold-pmin)/(pmax-pmin)); ty=plot.bottom-int(n*(plot.height-1))
        pygame.draw.line(app.ui,YELLOW,(plot.left,ty),(plot.right,ty),1); app._text(f'TH {app.cfg.get("threshold_db",10):.0f} dB',17,max(75,ty-17),app.font_s,YELLOW)
    lf=app.cfg.get("mobile_band_start_hz",app.cfg["scan_start_hz"])/1e6; rf=app.cfg.get("mobile_band_end_hz",app.cfg["scan_end_hz"])/1e6
    app._text(f"{lf:.3f}",12,268,app.font_s,DIM); label=f"{rf:.3f} MHz"; surf=app.font_s.render(label,True,DIM); app.ui.blit(surf,(308-surf.get_width(),268))
    app._text(f'Noise floor {snap["noise"]:.1f} dB',160,300,app.font_m,WHITE,center=True)
    peaks=list(snap["peaks"][:3]); y=332
    if not peaks: app._text("No peaks above threshold",160,360,app.font_s,DIM,center=True)
    for i,p in enumerate(peaks):
        pygame.draw.rect(app.ui,(9,13,18),(12,y,296,34),border_radius=6); app._text(f'{i+1}. {p["freq_hz"]/1e6:.5f} MHz',20,y+9,app.font_s,WHITE)
        app._text(f'{int(p["level"]*100)}%',286,y+17,app.font_s,BLUE_BRIGHT,center=True); y+=42
    app._text("tap top/bottom to return",160,463,app.font_s,DIM,center=True)
