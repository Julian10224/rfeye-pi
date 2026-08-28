"""Wi-Fi and touch-keyboard UI for RF Eye on 320x480."""
BG=(2,3,5); BLUE=(0,152,222); BLUE_BRIGHT=(28,190,255); WHITE=(224,229,236); DIM=(82,90,100); GREEN=(57,205,91)

def rows(app):
    letters=["qwertyuiop","asdfghjkl","zxcvbnm"]
    if bool(getattr(app,"wifi_shift",False)): letters=[r.upper() for r in letters]
    return ["1234567890",*letters]

def key_at(app,x,y):
    for ri,row in enumerate(rows(app)):
        yy=264+ri*32
        if yy<=y<yy+28 and 8<=x<312:
            idx=int((x-8)//(304/len(row)))
            if 0<=idx<len(row): return row[idx]
    if 392<=y<=428:
        if 8<=x<=68: return "BACK"
        if 74<=x<=140: app.wifi_shift=not bool(getattr(app,"wifi_shift",False)); return None
        if 146<=x<=232: return "SPACE"
        if 238<=x<=312: return "ENTER"
    return None

def draw_keyboard(app):
    import pygame
    for ri,row in enumerate(rows(app)):
        kw=304/len(row); y=264+ri*32
        for i,ch in enumerate(row):
            x=int(8+i*kw); w=max(17,int(kw-3)); pygame.draw.rect(app.ui,(18,24,30),(x,y,w,28),border_radius=5)
            app._text(ch,x+w//2,y+14,app.font_s,WHITE,center=True)
    buttons=[((8,392,60,36),"BACK",(30,36,44)),((74,392,66,36),"SHIFT",BLUE if getattr(app,"wifi_shift",False) else (30,36,44)),
             ((146,392,86,36),"SPACE",(30,36,44)),((238,392,74,36),"ENTER",(17,132,212))]
    for rect,label,col in buttons:
        pygame.draw.rect(app.ui,col,rect,border_radius=6); app._text(label,rect[0]+rect[2]//2,rect[1]+18,app.font_s,WHITE,center=True)

def draw(app):
    import pygame
    app.ui.fill(BG); pygame.draw.rect(app.ui,(7,11,16),(0,0,320,60)); app._text("‹",18,26,app.font_xl,BLUE_BRIGHT,center=True); app._text("WI-FI",48,10,app.font_l,WHITE)
    msg=app.wifi_message or app._wifi_text(); app._text(msg if len(msg)<=27 else msg[:26]+"…",50,38,app.font_s,DIM)
    if app.wifi_details:
        d=app.wifi_details; app._text("CONNECTED",12,72,app.font_s,DIM); app._text(str(d.get("ssid",""))[:26],12,90,app.font_m,GREEN)
        data=[("IP",d.get("ip","-")),("Gateway",d.get("gateway","-")),("DNS",d.get("dns","-")),("Signal",f'{d.get("signal",0)}%'),("Security",d.get("security","-"))]
        y=120
        for label,value in data:
            pygame.draw.rect(app.ui,(9,13,18),(8,y,304,46),border_radius=7); app._text(label,18,y+6,app.font_s,DIM)
            shown=str(value); app._text(shown if len(shown)<=34 else shown[:33]+"…",18,y+24,app.font_s,WHITE); y+=52
        app._text("tap top/bottom to close",160,455,app.font_s,DIM,center=True); return
    if app.wifi_selected:
        app._text("Network",10,70,app.font_s,DIM); app._text(str(app.wifi_selected)[:25],10,88,app.font_m,WHITE); app._text("Password",10,112,app.font_s,DIM)
        pygame.draw.rect(app.ui,(10,15,20),(8,130,304,38),border_radius=7); password=str(app.wifi_password)
        shown=password if getattr(app,"wifi_show_password",False) else "•"*len(password); shown=("…"+shown[-27:]) if len(shown)>28 else shown
        app._text(shown or "type below",16,141,app.font_s,WHITE if shown else DIM)
        eye=BLUE_BRIGHT if getattr(app,"wifi_show_password",False) else (132,184,210); pygame.draw.rect(app.ui,(18,24,30),(270,130,42,38),border_radius=6)
        pygame.draw.ellipse(app.ui,eye,(278,141,25,14),2); pygame.draw.circle(app.ui,eye,(290,148),3,1)
        pygame.draw.rect(app.ui,(17,132,212),(8,178,304,40),border_radius=7); app._text("CONNECT",160,198,app.font_m,WHITE,center=True)
        pygame.draw.rect(app.ui,(20,25,31),(8,224,304,34),border_radius=7); app._text("CANCEL",160,241,app.font_s,DIM,center=True); app._draw_wifi_keyboard(); return
    app._text("Available networks",10,70,app.font_s,DIM)
    for i,(ssid,sig,sec,active) in enumerate(app.wifi_networks[:9]):
        y=94+i*38; pygame.draw.rect(app.ui,(9,13,18),(8,y,304,32),border_radius=6); name=str(ssid); name=name if len(name)<=19 else name[:18]+"…"
        app._text(name,16,y+9,app.font_s,GREEN if active else WHITE); app._text(f"{sig}%",275,y+14,app.font_s,BLUE_BRIGHT,center=True)
        if sec: app._text("•",303,y+16,app.font_m,DIM,center=True)
    pygame.draw.rect(app.ui,(17,132,212),(8,438,304,34),border_radius=7); app._text("SCANNING…" if app.wifi_scan_busy else "RESCAN",160,455,app.font_s,WHITE,center=True)
