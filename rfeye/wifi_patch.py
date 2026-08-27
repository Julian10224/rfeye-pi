"""RF Eye Wi-Fi UI/connection patch.

Installed from config.py before app.py defines App. It adds a case-toggle key to
RF Eye's own on-screen keyboard and creates a complete user-owned
NetworkManager profile so no external GUI secret-agent password dialog is
needed for normal WPA/WPA2/WPA3-Personal networks.
"""
from __future__ import annotations

import builtins
import hashlib
import os
import pwd
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


def _connect_worker(app, ssid, password, security):
    profile = _profile_name(ssid)
    try:
        # Remove only our own stale profile. Never touch a user's unrelated profile.
        _run(["nmcli", "connection", "delete", "id", profile], timeout=8)

        user = pwd.getpwuid(os.getuid()).pw_name
        sec = security.upper()
        if "802.1X" in sec or "EAP" in sec or "ENTERPRISE" in sec:
            raise RuntimeError("enterprise Wi-Fi is not supported by the PSK keyboard")
        secured = bool(sec and sec not in {"--", "OPEN", "NONE"})

        add_cmd = [
            "nmcli", "connection", "add",
            "type", "wifi",
            "ifname", "wlan0",
            "con-name", profile,
            "ssid", ssid,
            "connection.permissions", f"user:{user}:",
            "connection.autoconnect", "yes",
        ]
        if secured:
            if not password:
                raise RuntimeError("password required")
            if "WEP" in sec:
                raise RuntimeError("WEP is not supported by the RF Eye keyboard")
            # WPA-PSK is the compatibility choice for WPA/WPA2 and WPA3 transition
            # networks. SAE is used only when the scan advertises WPA3/SAE without
            # WPA2/WPA1 compatibility. The PSK is saved in this user-owned profile,
            # so activation has no reason to invoke a desktop secret agent.
            wpa3_only = ("WPA3" in sec or "SAE" in sec) and "WPA2" not in sec and "WPA1" not in sec and "WPA " not in sec
            key_mgmt = "sae" if wpa3_only else "wpa-psk"
            add_cmd += [
                "802-11-wireless-security.key-mgmt", key_mgmt,
                "802-11-wireless-security.psk", password,
                "802-11-wireless-security.psk-flags", "0",
            ]

        cp = _run(add_cmd, timeout=15)
        if cp.returncode != 0:
            raise RuntimeError(cp.stderr.strip() or cp.stdout.strip() or "profile create failed")

        cp = _run([
            "nmcli", "--wait", "25",
            "connection", "up", "id", profile,
            "ifname", "wlan0",
        ], timeout=30)
        if cp.returncode != 0:
            msg = (cp.stderr or cp.stdout or "connect failed").strip()
            raise RuntimeError(msg)

        app.wifi_message = "CONNECTED"
        app.wifi_selected = None
        app.wifi_password = ""
        app.wifi_shift = False
        try:
            app._wifi_scan()
        except Exception:
            pass
    except Exception as exc:
        text = str(exc).lower()
        if "secret" in text or "password" in text or "key" in text:
            app.wifi_message = "PASSWORD FAILED"
        elif "not authorized" in text or "permission" in text:
            app.wifi_message = "CONNECT NOT AUTHORIZED"
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
    upper = bool(getattr(app, "wifi_shift", False))
    letters = ["qwertyuiop", "asdfghjkl", "zxcvbnm"]
    if upper:
        letters = [row.upper() for row in letters]
    return ["1234567890", *letters]


def _wifi_key_at(app, x, y):
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
    pygame.draw.rect(app.ui, (17, 132, 212) if active else (30, 36, 44), (124, 660, 90, 44), border_radius=8)
    pygame.draw.rect(app.ui, (30, 36, 44), (222, 660, 120, 44), border_radius=8)
    pygame.draw.rect(app.ui, (17, 132, 212), (350, 660, 100, 44), border_radius=8)
    app._text("BACK", 73, 682, app.font_s, (224, 229, 236), center=True)
    app._text("SHIFT", 169, 682, app.font_s, (224, 229, 236), center=True)
    app._text("SPACE", 282, 682, app.font_s, (224, 229, 236), center=True)
    app._text("ENTER", 400, 682, app.font_s, (224, 229, 236), center=True)


def _patch_app_class(cls):
    old_init = cls.__init__

    def patched_init(self, *args, **kwargs):
        old_init(self, *args, **kwargs)
        self.wifi_shift = False
        self.wifi_connect_busy = False

    cls.__init__ = patched_init
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
