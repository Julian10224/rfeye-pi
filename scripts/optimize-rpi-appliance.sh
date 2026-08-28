#!/usr/bin/env bash
set -euo pipefail

# RF Eye is a dedicated appliance. Disable services that are not needed for
# display, RTL-SDR, Wi-Fi, GPIO audio, updates or the graphical kiosk session.
# Keep this conservative: do not purge packages or disable core Raspberry Pi,
# NetworkManager, display-manager, udev, dbus or filesystem services.

if [[ $EUID -ne 0 ]]; then
  echo "Run with sudo/root"
  exit 1
fi

disable_unit() {
  local unit="$1"
  if systemctl list-unit-files "$unit" >/dev/null 2>&1; then
    systemctl disable --now "$unit" >/dev/null 2>&1 || true
  fi
}

mask_unit() {
  local unit="$1"
  if systemctl list-unit-files "$unit" >/dev/null 2>&1; then
    systemctl disable --now "$unit" >/dev/null 2>&1 || true
    systemctl mask "$unit" >/dev/null 2>&1 || true
  fi
}

# Do not block boot waiting for network. RF Eye itself can start offline.
mask_unit NetworkManager-wait-online.service
mask_unit systemd-networkd-wait-online.service

# Printing is not part of the appliance.
if [[ "${RFEYE_KEEP_PRINTING:-0}" != "1" ]]; then
  mask_unit cups.service
  mask_unit cups.socket
  mask_unit cups.path
  mask_unit cups-browsed.service
fi

# Bluetooth is not used by RF Eye. Set RFEYE_KEEP_BLUETOOTH=1 before install
# if a future hardware setup needs it.
if [[ "${RFEYE_KEEP_BLUETOOTH:-0}" != "1" ]]; then
  mask_unit bluetooth.service
  mask_unit hciuart.service
fi

# Cellular modem probing can delay USB startup and is not used here.
if [[ "${RFEYE_KEEP_MODEMMANAGER:-0}" != "1" ]]; then
  mask_unit ModemManager.service
fi

# Desktop hotkey/input daemon is unnecessary in the fullscreen kiosk.
mask_unit triggerhappy.service
mask_unit triggerhappy.socket

# Avoid background package maintenance competing with SDR/UI shortly after boot.
# Updates remain available explicitly through RF Eye/GitHub or apt when desired.
disable_unit apt-daily.timer
disable_unit apt-daily-upgrade.timer
disable_unit man-db.timer

# Avahi/mDNS is optional. Keep it by default because it is useful for finding
# the Pi on a LAN; set RFEYE_DISABLE_MDNS=1 for the leanest appliance boot.
if [[ "${RFEYE_DISABLE_MDNS:-0}" == "1" ]]; then
  mask_unit avahi-daemon.service
  mask_unit avahi-daemon.socket
fi

systemctl daemon-reload || true

echo "RF Eye appliance optimization applied."
echo "Kept: NetworkManager, graphical session, udev, dbus, GPIO/pigpio, RTL-SDR."
