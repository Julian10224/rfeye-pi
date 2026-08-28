"""RF Eye Wi-Fi UI/connection patch.

Adds a lowercase-first touch keyboard with an explicit SHIFT toggle, a password
visibility eye, and persistent system NetworkManager profiles for reliable boot autoconnect.  The patch
is installed from config.py before app.py defines App, so existing appliance UI
code can be upgraded without replacing the whole app.py file.
"""
from __future__ import annotations

import builtins
import hashlib
import subprocess
import threading


def _run(cmd, timeout=15):
    return subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
    )


def _security_for(app, ssid):
    for item in getattr(app, "wifi_networks", []):
        if item and item[0] == ssid:
            return str(item[2] or "")
    return ""


def _profile_name(ssid):
    digest = hashlib.sha1(ssid.encode("utf-8", "replace")).hexdigest()[:10]
    return f"RF Eye WiFi {digest}"


def _error_text(cp, fallback="connect failed"):
    return (getattr(cp, "stderr", "") or getattr(cp, "stdout", "") or fallback).strip()


def _profile_exists(name):
    cp = _run(["nmcli", "-g", "connection.id", "connection", "show", "id", name], timeout=8)
    return cp.returncode == 0


def _connect_worker(app, ssid, password, security):
    try:
        sec = security.upper()
        if "802.1X" in sec or "EAP" in sec or "ENTERPRISE" in sec:
            raise RuntimeError("enterprise Wi-Fi is not supported by the PSK keyboard")
        if "WEP" in sec:
            raise RuntimeError("WEP is not supported by the RF Eye keyboard")

        secured = bool(sec and sec not in {"--", "OPEN", "NONE"})
        if secured and not password:
            raise RuntimeError("password required")

        profile = _profile_name(ssid)
        exists = _profile_exists(profile)
        # System profile + saved secret = reliable boot autoconnect.
        if not exists:
            cp = _run([
                "nmcli", "connection", "add",
                "type", "wifi", "ifname", "wlan0",
                "con-name", profile, "ssid", ssid,
                "connection.autoconnect", "yes",
                "connection.autoconnect-priority", "100",
            ], timeout=15)
            if cp.returncode != 0:
                raise RuntimeError(_error_text(cp))

        cp = _run([
            "nmcli", "connection", "modify", "id", profile,
            "connection.interface-name", "wlan0",
            "connection.autoconnect", "yes",
            "connection.autoconnect-priority", "100",
            "connection.permissions", "",
            "802-11-wireless.ssid", ssid,
        ], timeout=15)
        if cp.returncode != 0:
            raise RuntimeError(_error_text(cp))

        if secured:
            key_mgmt = "sae" if "SAE" in sec and "WPA2" not in sec else "wpa-psk"
            cp = _run([
                "nmcli", "connection", "modify", "id", profile,
                "802-11-wireless-security.key-mgmt", key_mgmt,
                "802-11-wireless-security.psk", password,
                "802-11-wireless-security.psk-flags", "0",
            ], timeout=15)
        else:
            cp = _run([
                "nmcli", "connection", "modify", "id", profile,
                "802-11-wireless-security.key-mgmt", "",
                "802-11-wireless-security.psk", "",
            ], timeout=15)
        if cp.returncode != 0:
            raise RuntimeError(_error_text(cp))

        cp = _run([
            "nmcli", "--wait", "25", "connection", "up", "id", profile,
            "ifname", "wlan0",
        ], timeout=30)
        if cp.returncode != 0:
            raise RuntimeError(_error_text(cp))

        app.wifi_message = "CONNECTED"
        app.wifi_selected = None
        app.wifi_password = ""
        app.wifi_shift = False
        app.wifi_show_password = False
        try:
            app._wifi_scan()
        except Exception:
            pass
    except Exception as exc:
        text = str(exc).lower()
        if "not authorized" in text or "not authorised" in text or "permission" in text:
            app.wifi_message = "CONNECT NOT AUTHORIZED"
        elif (
            "secret" in text
            or "password" in text
            or "psk" in text
            or "authentication" in text
            or "wrong key" in text
            or "supplicant" in text
        ):
            app.wifi_message = "PASSWORD FAILED"
        else:
            app.wifi_message = "CONNECT FAILED"
    finally:
        app.wifi_connect_busy = False


def _wifi_connect(app):
    if getattr(app, "wifi_connect_busy", False):
        return
    ssid = getattr(app, "wifi_selected", None)
    if not ssid:
        return
    app.wifi_connect_busy = True
    app.wifi_message = "CONNECTING..."
    password = str(getattr(app, "wifi_password", ""))
    security = _security_for(app, ssid)
    threading.Thread(
        target=_connect_worker,
        args=(app, ssid, password, security),
        daemon=True,
        name="rfeye-wifi-connect",
    ).start()


def _rows(app):
    # Lowercase is always the default. SHIFT is an explicit opt-in toggle.
    upper = bool(getattr(app, "wifi_shift", False))
    letters = ["qwertyuiop", "asdfghjkl", "zxcvbnm"]
    if upper:
        letters = [row.upper() for row in letters]
    return ["1234567890", *letters]


def _wifi_key_at(app, x, y):
    # Password visibility eye inside the password field.
    if getattr(app, "wifi_selected", None) and 394 <= x <= 452 and 258 <= y <= 304:
        app.wifi_show_password = not bool(getattr(app, "wifi_show_password", False))
        return None

    rows = _rows(app)
    top = 468
    row_h = 48
    for ri, row in enumerate(rows):
        yy = top + ri * row_h
        if yy <= y < yy + 40:
            total = 420
            kw = total / len(row)
            start = 30
            if start <= x < start + total:
                idx = int((x - start) // kw)
                if 0 <= idx < len(row):
                    return row[idx]

    if 660 <= y <= 704:
        if 30 <= x <= 116:
            return "BACK"
        if 124 <= x <= 214:
            app.wifi_shift = not bool(getattr(app, "wifi_shift", False))
            return None
        if 222 <= x <= 342:
            return "SPACE"
        if 350 <= x <= 450:
            return "ENTER"
    return None


def _draw_wifi_keyboard(app):
    import pygame

    rows = _rows(app)
    top = 468
    row_h = 48
    for ri, row in enumerate(rows):
        total = 420
        kw = total / len(row)
        start = 30
        y = top + ri * row_h
        for i, ch in enumerate(row):
            x = int(start + i * kw)
            w = int(kw - 4)
            pygame.draw.rect(app.ui, (18, 24, 30), (x, y, w, 40), border_radius=8)
            app._text(ch, x + w // 2, y + 20, app.font_s, (224, 229, 236), center=True)

    active = bool(getattr(app, "wifi_shift", False))
    pygame.draw.rect(app.ui, (30, 36, 44), (30, 660, 86, 44), border_radius=8)
    pygame.draw.rect(
        app.ui,
        (17, 132, 212) if active else (30, 36, 44),
        (124, 660, 90, 44),
        border_radius=8,
    )
    pygame.draw.rect(app.ui, (30, 36, 44), (222, 660, 120, 44), border_radius=8)
    pygame.draw.rect(app.ui, (17, 132, 212), (350, 660, 100, 44), border_radius=8)
    app._text("BACK", 73, 682, app.font_s, (224, 229, 236), center=True)
    app._text("SHIFT", 169, 682, app.font_s, (224, 229, 236), center=True)
    app._text("SPACE", 282, 682, app.font_s, (224, 229, 236), center=True)
    app._text("ENTER", 400, 682, app.font_s, (224, 229, 236), center=True)


def _fit_password_text(app, text, max_width):
    if not text:
        return text
    if app.font_s.size(text)[0] <= max_width:
        return text
    suffix = text
    while suffix and app.font_s.size("…" + suffix)[0] > max_width:
        suffix = suffix[1:]
    return "…" + suffix


def _draw_password_overlay(app):
    if not getattr(app, "wifi_selected", None) or getattr(app, "wifi_details", None):
        return

    import pygame

    field = pygame.Rect(24, 254, 432, 54)
    pygame.draw.rect(app.ui, (10, 15, 20), field, border_radius=12)

    show = bool(getattr(app, "wifi_show_password", False))
    password = str(getattr(app, "wifi_password", ""))
    if password:
        shown = password if show else ("•" * len(password))
        shown = _fit_password_text(app, shown, 335)
        app._text(shown, 42, 271, app.font_s, (224, 229, 236))
    else:
        app._text("type with keyboard...", 42, 270, app.font_s, (82, 90, 100))

    # Eye button. Blue means the password is currently visible.
    eye_col = (28, 190, 255) if show else (132, 184, 210)
    pygame.draw.rect(app.ui, (18, 24, 30), (394, 259, 54, 44), border_radius=9)
    pygame.draw.ellipse(app.ui, eye_col, (403, 270, 36, 21), 2)
    pygame.draw.circle(app.ui, eye_col, (421, 280), 5, 2)
    if not show:
        pygame.draw.line(app.ui, eye_col, (402, 294), (440, 266), 2)


def _patch_app_class(cls):
    old_init = cls.__init__
    old_tap = cls._tap
    old_draw_wifi = cls._draw_wifi

    def patched_init(self, *args, **kwargs):
        old_init(self, *args, **kwargs)
        self.wifi_shift = False
        self.wifi_show_password = False
        self.wifi_connect_busy = False

    def patched_tap(self, x, y):
        before = getattr(self, "wifi_selected", None)
        old_tap(self, x, y)
        after = getattr(self, "wifi_selected", None)
        # A new password entry session always starts lowercase and hidden.
        if before != after:
            self.wifi_shift = False
            self.wifi_show_password = False

    def patched_draw_wifi(self):
        old_draw_wifi(self)
        _draw_password_overlay(self)

    cls.__init__ = patched_init
    cls._tap = patched_tap
    cls._draw_wifi = patched_draw_wifi
    cls._wifi_connect = _wifi_connect
    cls._wifi_connect_worker = _connect_worker
    cls._wifi_key_at = _wifi_key_at
    cls._draw_wifi_keyboard = _draw_wifi_keyboard
    return cls


def install_app_patch():
    """Patch only the next RF Eye App class built after config.py is imported."""
    original = builtins.__build_class__
    if getattr(original, "_rfeye_wifi_patch", False):
        return

    def wrapper(func, name, *bases, **kwargs):
        cls = original(func, name, *bases, **kwargs)
        if name == "App" and getattr(cls, "__module__", "") in {"__main__", "app"}:
            try:
                _patch_app_class(cls)
            finally:
                builtins.__build_class__ = original
        return cls

    wrapper._rfeye_wifi_patch = True
    builtins.__build_class__ = wrapper
