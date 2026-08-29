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

TARGET_USER="${1:-${SUDO_USER:-julian}}"
TARGET_HOME="$(getent passwd "$TARGET_USER" | cut -d: -f6)"

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

# Wi-Fi is useful after startup, but it is not required to show RF Eye. Keep
# NetworkManager enabled and let it associate in parallel with the kiosk.
mask_unit NetworkManager-wait-online.service
mask_unit systemd-networkd-wait-online.service

# systemd-user-sessions normally orders itself after network.target. LightDM is
# ordered after systemd-user-sessions, so that made the display wait for
# NetworkManager even though RF Eye itself does not need networking. Reset only
# that ordering edge; retain the normal local filesystem/NSS prerequisites.
mkdir -p /etc/systemd/system/systemd-user-sessions.service.d
cat >/etc/systemd/system/systemd-user-sessions.service.d/20-rfeye-no-network.conf <<'EOF'
[Unit]
After=
After=remote-fs.target nss-user-lookup.target home.mount
EOF

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

if [[ "${RFEYE_KEEP_BLUETOOTH:-0}" != "1" ]]; then
  mask_unit bluetooth.service
  mask_unit hciuart.service
fi

if [[ "${RFEYE_KEEP_MODEMMANAGER:-0}" != "1" ]]; then
  mask_unit ModemManager.service
fi

mask_unit triggerhappy.service
mask_unit triggerhappy.socket

if [[ "${RFEYE_KEEP_MDNS:-0}" != "1" ]]; then
  mask_unit avahi-daemon.service
  mask_unit avahi-daemon.socket
fi

if [[ "${RFEYE_KEEP_RSYSLOG:-0}" != "1" ]]; then
  mask_unit rsyslog.service
fi

# RF Eye uses the GPIO buzzer and direct Wayland rendering. These environment
# hints prevent SDL/GTK from requesting desktop portal integration such as
# screensaver inhibition during startup. The Wayland socket is still used
# directly, and Wi-Fi/settings continue to use NetworkManager via nmcli/D-Bus.
if [[ -n "$TARGET_HOME" && -d "$TARGET_HOME" ]]; then
  mkdir -p "$TARGET_HOME/.config/systemd/user/rfeye-user.service.d"
  cat >"$TARGET_HOME/.config/systemd/user/rfeye-user.service.d/20-rfeye-fast-ui.conf" <<'EOF'
[Service]
Environment=SDL_VIDEO_ALLOW_SCREENSAVER=1
Environment=GTK_USE_PORTAL=0
Environment=NO_AT_BRIDGE=1
EOF
  chown -R "$TARGET_USER:$TARGET_USER" "$TARGET_HOME/.config/systemd/user/rfeye-user.service.d"
fi

# Avoid background package/index work competing with RF Eye around boot.
disable_unit apt-daily.timer
disable_unit apt-daily-upgrade.timer
disable_unit man-db.timer
disable_unit e2scrub_all.timer

disable_unit rpi-eeprom-update.service
disable_unit rpi-eeprom-update.timer
disable_unit update-notifier-download.timer

action_log=/var/lib/rfeye/boot-optimization.txt
mkdir -p /var/lib/rfeye
{
  echo "RF Eye appliance boot optimization"
  echo "Applied: $(date -Is 2>/dev/null || date)"
  echo "NetworkManager remains enabled and starts in parallel with the kiosk."
  echo "LightDM user sessions do not wait for network.target."
  echo "RF Eye SDL/GTK portal startup requests are disabled."
  echo "cloud-init disabled unless RFEYE_KEEP_CLOUD_INIT=1."
  echo "Optional services can be retained with RFEYE_KEEP_* environment flags."
} > "$action_log"

systemctl daemon-reload || true

echo "RF Eye appliance optimization applied."
echo "Kept: NetworkManager, graphical session, udev, dbus, GPIO, RTL-SDR."
