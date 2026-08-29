"""Touch controls for the RF Eye 320x480 compact display profile."""
import time


def _save(app):
    from config import save_config
    save_config(app.cfg)


def tap(app, x, y):
    now=time.monotonic()
    if now-float(getattr(app,'_last_compact_tap',0.0)) < 0.12:
        return
    app._last_compact_tap=now

    if app.page == 'main':
        # Settings gear is now on the left.
        if x <= 68 and y <= 76:
            app.page = 'settings'
        elif y >= 366 and x < 104:
            app._toggle_mute()
        elif y >= 366 and 106 <= x <= 216:
            app.page = 'spectrum'
        return

    if app.page == 'settings':
        from compact_ui_draw import SETTINGS_TOP, SETTINGS_STEP, SETTINGS_COUNT
        if y < 62:
            app.page = 'main'; return
        idx = int((y - SETTINGS_TOP) / SETTINGS_STEP)
        keys = ['muted','demo_mode','threshold_db','audio_mode','brightness',
                'show_frequency','wifi','update','spectrum','debug']
        if not 0 <= idx < min(SETTINGS_COUNT, len(keys)):
            return
        key = keys[idx]
        if key == 'muted': app._toggle_mute()
        elif key == 'demo_mode': app._toggle_demo(); app.page = 'main'
        elif key == 'threshold_db':
            v = float(app.cfg['threshold_db']) + 3.0
            app.cfg['threshold_db'] = 6.0 if v > 24.0 else v; _save(app)
        elif key == 'audio_mode':
            app.cfg['audio_mode'] = 'standard' if app.cfg.get('audio_mode') == 'adaptive' else 'adaptive'; _save(app)
        elif key == 'brightness':
            v = round(float(app.cfg.get('brightness', 1.0)) - 0.1, 1)
            app.cfg['brightness'] = 1.0 if v < 0.4 else v; _save(app)
        elif key == 'show_frequency':
            app.cfg['show_frequency'] = not app.cfg.get('show_frequency', True); _save(app)
        elif key == 'wifi': app.page = 'wifi'; app._wifi_scan()
        elif key == 'update': app._update_action()
        elif key == 'spectrum': app.page = 'spectrum'
        elif key == 'debug': app.page = 'debug'
        return

    if app.page == 'wifi':
        if app.wifi_details:
            if y < 64 or y > 430: app.wifi_details = None
            return
        if y < 62:
            app.page = 'settings'; app.wifi_selected = None; app.wifi_password = ''
            app.wifi_shift = False; app.wifi_show_password = False
            return
        if app.wifi_selected:
            if 270 <= x <= 312 and 130 <= y <= 168:
                app.wifi_show_password = not bool(app.wifi_show_password); return
            key = app._wifi_key_at(x, y)
            if key:
                if key == 'BACK': app.wifi_password = app.wifi_password[:-1]
                elif key == 'SPACE' and len(app.wifi_password) < 63: app.wifi_password += ' '
                elif key == 'ENTER': app._wifi_connect()
                elif len(app.wifi_password) < 63: app.wifi_password += key
                return
            if 178 <= y <= 218: app._wifi_connect()
            elif 224 <= y <= 258:
                app.wifi_selected = None; app.wifi_password = ''
                app.wifi_shift = False; app.wifi_show_password = False
            return
        if y >= 434:
            app._wifi_scan(); return
        idx = int((y - 94) / 38)
        if 0 <= idx < min(9, len(app.wifi_networks)):
            ssid, _sig, _sec, active = app.wifi_networks[idx]
            if active:
                app.wifi_details = app._wifi_details_for_connected(ssid); app.wifi_selected = None
            else:
                app.wifi_selected = ssid; app.wifi_password = ''
                app.wifi_shift = False; app.wifi_show_password = False
                app.wifi_message = 'ENTER PASSWORD'
        return

    if app.page == 'spectrum' and (y < 62 or y > 448):
        app.page = 'main'; return
    if app.page == 'debug' and (y < 62 or y > 448):
        app.page = 'settings'
