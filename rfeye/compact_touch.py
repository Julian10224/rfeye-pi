"""Direct XPT2046/ADS7846 touch reader for the MHS35 compact profile."""
from collections import deque
import os, select, struct, threading, time

# Factory affine transform derived from the working MHS35 four-corner values.
# ui_x = a*raw_x + b*raw_y + c ; ui_y = d*raw_x + e*raw_y + f
FACTORY_AFFINE = [0.0, -0.08831672203765227, 342.6688815060908,
                  -0.12914532218926936, 0.0, 508.31598813696417]
EV_KEY=1; EV_ABS=3; EV_SYN=0
BTN_TOUCH=330; ABS_X=0; ABS_Y=1; SYN_REPORT=0
EVENT=struct.Struct("llHHi")

def _event_device():
    from pathlib import Path
    for name in Path('/sys/class/input').glob('event*/device/name'):
        try:
            label=name.read_text().strip().lower()
            if 'ads7846' in label or 'xpt2046' in label:
                return '/dev/input/'+name.parents[1].name
        except Exception:
            pass
    return None

def _coeffs(app):
    values=app.cfg.get('touch_calibration_affine', FACTORY_AFFINE)
    try:
        values=[float(v) for v in values]
        if len(values)==6: return values
    except Exception:
        pass
    return list(FACTORY_AFFINE)

def _map(app,raw_x,raw_y):
    a,b,c,d,e,f=_coeffs(app)
    x=a*float(raw_x)+b*float(raw_y)+c
    y=d*float(raw_x)+e*float(raw_y)+f
    return max(0,min(319,int(round(x)))), max(0,min(479,int(round(y))))

def install(app):
    app._direct_touch_queue=deque(maxlen=12)
    app._direct_touch_path=_event_device()
    def worker():
        raw_x=2048; raw_y=2048; pending=False; touching=False; dirty=False; last_emit=0.0
        while getattr(app,'running',True):
            fd=None
            try:
                # Re-discover on every retry. This covers late driver startup
                # and event-number changes without requiring an app restart.
                path=_event_device()
                app._direct_touch_path=path
                if not path:
                    time.sleep(0.5)
                    continue
                fd=os.open(path,os.O_RDONLY|os.O_NONBLOCK)
                while getattr(app,'running',True):
                    ready,_,_=select.select([fd],[],[],0.25)
                    if not ready: continue
                    data=os.read(fd,EVENT.size*32)
                    if not data:
                        raise OSError('touch device closed')
                    for off in range(0,len(data)-EVENT.size+1,EVENT.size):
                        _sec,_usec,etype,code,value=EVENT.unpack_from(data,off)
                        if etype==EV_ABS and code==ABS_X: raw_x=value; dirty=True
                        elif etype==EV_ABS and code==ABS_Y: raw_y=value; dirty=True
                        elif etype==EV_KEY and code==BTN_TOUCH:
                            if value==1: touching=True; pending=True
                            elif value==0: touching=False
                        elif etype==EV_SYN and code==SYN_REPORT:
                            mapped=_map(app,raw_x,raw_y); now=time.monotonic()
                            slider_drag=(touching and dirty and getattr(app,'page',None)=='settings' and ((135<=mapped[1]<=190) or (205<=mapped[1]<=260)) and now-last_emit>=0.06)
                            if pending or slider_drag:
                                app._direct_touch_queue.append((raw_x,raw_y,mapped[0],mapped[1])); last_emit=now
                            pending=False; dirty=False
            except Exception:
                if getattr(app,'running',True):
                    time.sleep(0.5)
            finally:
                if fd is not None:
                    try: os.close(fd)
                    except OSError: pass
    threading.Thread(target=worker,name='rfeye-xpt2046',daemon=True).start()

def drain(app):
    q=getattr(app,'_direct_touch_queue',None)
    if not q: return
    while q:
        raw_x,raw_y,x,y=q.popleft()
        try:
            with open('/tmp/rfeye-touch.log','a') as f: f.write(f'{time.time():.3f} raw={raw_x},{raw_y} ui={x},{y} page={app.page}\n')
        except Exception: pass
        if getattr(app,'page',None)=='calibration':
            from compact_ui_controls import capture_calibration
            capture_calibration(app,raw_x,raw_y)
        else:
            app._tap(x,y)
