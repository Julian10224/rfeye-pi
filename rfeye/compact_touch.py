"""Direct XPT2046/ADS7846 touch reader for the MHS35 compact profile."""
from collections import deque
import os, select, struct, threading, time
from pathlib import Path

# GoodTFT MHS35 calibration for the XPT2046-compatible ADS7846 driver.
RAW_X_TOP = 3936.0
RAW_X_BOTTOM = 227.0
RAW_Y_LEFT = 268.0
RAW_Y_RIGHT = 3880.0
EV_KEY=1; EV_ABS=3; EV_SYN=0
BTN_TOUCH=330; ABS_X=0; ABS_Y=1; SYN_REPORT=0
EVENT=struct.Struct("llHHi")


def _clamp(v): return max(0.0,min(1.0,v))


def _event_device():
    for name in Path('/sys/class/input').glob('event*/device/name'):
        try:
            label=name.read_text().strip().lower()
            if 'ads7846' in label or 'xpt2046' in label:
                return '/dev/input/'+name.parents[1].name
        except Exception:
            pass
    return None


def _map(app,raw_x,raw_y):
    # GoodTFT MHS35 uses SwapAxes=1.  This maps the raw resistive controller
    # directly into RF Eye's native 320x480 portrait canvas.
    x=_clamp((float(raw_y)-RAW_Y_LEFT)/(RAW_Y_RIGHT-RAW_Y_LEFT))
    y=_clamp((float(raw_x)-RAW_X_TOP)/(RAW_X_BOTTOM-RAW_X_TOP))
    ux=int(round(x*319.0)); uy=int(round(y*479.0))
    # The alternate application rotation is the same physical panel mounted
    # 180 degrees the other way, so invert both portrait axes.
    if app.cfg.get('rotation','cw')=='ccw':
        ux=319-ux; uy=479-uy
    if app.cfg.get('touch_invert_x',False): ux=319-ux
    if app.cfg.get('touch_invert_y',False): uy=479-uy
    return ux,uy


def install(app):
    path=_event_device()
    app._direct_touch_queue=deque(maxlen=8)
    app._direct_touch_path=path
    if not path:
        return
    def worker():
        raw_x=2048; raw_y=2048; pending=False
        while getattr(app,'running',True):
            try:
                fd=os.open(path,os.O_RDONLY|os.O_NONBLOCK)
                try:
                    while getattr(app,'running',True):
                        ready,_,_=select.select([fd],[],[],0.25)
                        if not ready: continue
                        data=os.read(fd,EVENT.size*32)
                        for off in range(0,len(data)-EVENT.size+1,EVENT.size):
                            _sec,_usec,etype,code,value=EVENT.unpack_from(data,off)
                            if etype==EV_ABS and code==ABS_X: raw_x=value
                            elif etype==EV_ABS and code==ABS_Y: raw_y=value
                            elif etype==EV_KEY and code==BTN_TOUCH and value==1: pending=True
                            elif etype==EV_SYN and code==SYN_REPORT and pending:
                                app._direct_touch_queue.append(_map(app,raw_x,raw_y)); pending=False
                finally:
                    os.close(fd)
            except Exception:
                time.sleep(0.5)
    threading.Thread(target=worker,name='rfeye-xpt2046',daemon=True).start()


def drain(app):
    q=getattr(app,'_direct_touch_queue',None)
    if not q: return
    while q:
        x,y=q.popleft()
        app._tap(x,y)
