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

# Never make the graphical appliance wait for networking to be considered
# 'online'. NetworkManager itself still starts normally and Wi-Fi remains usable.
mask_unit NetworkManager-wait-online.service
mask_unit systemd-networkd-wait-online.service

# cloud-init is intended for provisioning cloud/first-boot images. On the RF Eye
# appliance it was measured on the critical boot chain and added roughly nine
# seconds before NetworkManager/display startup. RF Eye configures networking,
# hostname and application state itself after installation, so keep cloud-init
# disabled unless an integrator explicitly opts back in.
if [[ "${RFEYE_KEEP_CLOUD_INIT:-0}" != "1" ]]; then
  mkdir -p /etc/cloud
  touch /etc/cloud/cloud-init.disabled
  mask_unit cloud-init-main.service
  mask_unit cloud-init-local.service
  mask_unit cloud-init-network.service
  mask_unit cloud-config.service
  mask_unit cloud-final.service
  mask_unit cloud-init.target
fi

# Printing is not part of the appliance.
if [[ "${RFEYE_KEEP_PRINTING:-0}" != "1" ]]; then
  mask_unit cups.service
  mask_unit cups.socket
  mask_unit cups.path
  mask_unit cups-browsed.service
fi

# Bluetooth is not used by RF Eye. Keep an escape hatch for future hardware.
if [[ "${RFEYE_KEEP_BLUETOOTH:-0}" != "1" ]]; then
  mask_unit bluetooth.service
  mask_unit hciuart.service
fi

# Cellular modem probing can delay USB enumeration and is unused here.
if [[ "${RFEYE_KEEP_MODEMMANAGER:-0}" != "1" ]]; then
  mask_unit ModemManager.service
fi

# Desktop hotkey/input daemon is unnecessary in the fullscreen kiosk.
mask_unit triggerhappy.service
mask_unit triggerhappy.socket

# mDNS discovery is convenient but not needed for normal RF Eye operation and
# can be started manually if required. Keeping it off saves background startup.
if [[ "${RFEYE_KEEP_MDNS:-0}" != "1" ]]; then
  mask_unit avahi-daemon.service
  mask_unit avahi-daemon.socket
fi

# systemd-journald already keeps logs; rsyslog is duplicate work on an appliance.
# Preserve it when explicitly requested for external/syslog workflows.
if [[ "${RFEYE_KEEP_RSYSLOG:-0}" != "1" ]]; then
  mask_unit rsyslog.service
fi

# Avoid background package/index work competing with RF Eye around boot.
disable_unit apt-daily.timer
disable_unit apt-daily-upgrade.timer
disable_unit man-db.timer
disable_unit e2scrub_all.timer

# Pi-specific maintenance that is not useful on every boot. These units may not
# exist on all Raspberry Pi OS releases, so every operation is conditional.
disable_unit rpi-eeprom-update.service
disable_unit rpi-eeprom-update.timer
disable_unit update-notifier-download.timer

action_log=/var/lib/rfeye/boot-optimization.txt
mkdir -p /var/lib/rfeye
{
  echo "RF Eye appliance boot optimization"
  echo "Applied: $(date -Is 2>/dev/null || date)"
  echo "NetworkManager remains enabled; wait-online disabled."
  echo "cloud-init disabled unless RFEYE_KEEP_CLOUD_INIT=1."
  echo "Optional services can be retained with RFEYE_KEEP_* environment flags."
} > "$action_log"

systemctl daemon-reload || true

echo "RF Eye appliance optimization applied."
echo "Kept: NetworkManager, graphical session, udev, dbus, GPIO, RTL-SDR."
