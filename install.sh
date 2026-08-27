#!/usr/bin/env bash
set -euo pipefail

REPO_SLUG="${RFEYE_REPO:-Julian10224/rfeye-pi}"
APP_ROOT=/opt/rfeye
SRC_ROOT=/opt/rfeye-src

if [[ $EUID -ne 0 ]]; then
  echo "Run with sudo: sudo bash install.sh"
  exit 1
fi

TARGET_USER="${SUDO_USER:-$(logname 2>/dev/null || echo julian)}"
TARGET_HOME="$(getent passwd "$TARGET_USER" | cut -d: -f6)"
TARGET_UID="$(id -u "$TARGET_USER")"

echo "[1/8] Installing packages..."
apt-get update
# Configure the Dutch Wi-Fi regulatory domain so 2.4/5 GHz scanning uses NL rules.
if command -v raspi-config >/dev/null 2>&1; then
  raspi-config nonint do_wifi_country NL || true
fi
DEBIAN_FRONTEND=noninteractive apt-get install -y \
  git python3 python3-venv python3-pip python3-numpy python3-pygame \
  python3-rpi.gpio rtl-sdr librtlsdr0 librtlsdr-dev usbutils curl unzip kanshi network-manager

echo "[2/8] Downloading RF Eye..."
rm -rf "$SRC_ROOT"
if [[ -d "${RFEYE_LOCAL_SOURCE:-}" ]]; then
  cp -a "$RFEYE_LOCAL_SOURCE" "$SRC_ROOT"
else
  git clone --depth 1 "https://github.com/${REPO_SLUG}.git" "$SRC_ROOT"
fi

rm -rf "$APP_ROOT"
mkdir -p "$APP_ROOT/rfeye"
cp -a "$SRC_ROOT/rfeye/." "$APP_ROOT/rfeye/"
python3 -m venv --system-site-packages "$APP_ROOT/.venv"
"$APP_ROOT/.venv/bin/pip" install --upgrade pip wheel
"$APP_ROOT/.venv/bin/pip" install -r "$SRC_ROOT/requirements.txt"
chown -R "$TARGET_USER:$TARGET_USER" "$APP_ROOT/rfeye" "$SRC_ROOT"

echo "[3/8] Configuring RTL-SDR..."
cat >/etc/modprobe.d/rfeye-rtl-sdr.conf <<'EOF'
blacklist dvb_usb_rtl28xxu
blacklist rtl2832
blacklist rtl2830
EOF

echo "[4/8] Configuring Elecrow HDMI display..."
BOOT=/boot/firmware
[[ -d "$BOOT" ]] || BOOT=/boot
CONFIG="$BOOT/config.txt"
cp -n "$CONFIG" "${CONFIG}.rfeye-backup" || true
if ! grep -q '# rfeye-display-start' "$CONFIG"; then
cat >> "$CONFIG" <<'EOF'

# rfeye-display-start
disable_overscan=1
hdmi_force_hotplug=1
hdmi_group=2
hdmi_mode=87
hdmi_cvt=800 480 60 6 0 0 0
hdmi_drive=2
config_hdmi_boost=7
framebuffer_width=800
framebuffer_height=480
dtparam=spi=on
dtoverlay=ads7846,cs=1,penirq=25,penirq_pull=2,speed=50000,keep_vref_on=0,swapxy=0,pmax=255,xohms=150,xmin=200,xmax=3900,ymin=200,ymax=3900
# rfeye-display-end
EOF
fi

echo "[5/8] Installing appliance session..."
mkdir -p "$TARGET_HOME/.config/labwc" "$TARGET_HOME/.config/autostart" "$TARGET_HOME/.config/kanshi" "$TARGET_HOME/.config/systemd/user/default.target.wants"
CONNECTED_OUTPUT=""
for status in /sys/class/drm/card*-HDMI-A-*/status; do
  [[ -e "$status" ]] || continue
  if grep -q '^connected$' "$status"; then
    base="$(basename "$(dirname "$status")")"
    CONNECTED_OUTPUT="${base#*-}"
    break
  fi
done
[[ -n "$CONNECTED_OUTPUT" ]] || CONNECTED_OUTPUT="HDMI-A-1"
cat > "$TARGET_HOME/.config/kanshi/config" <<EOF
profile {
    output ${CONNECTED_OUTPUT} mode 800x480 position 0,0
}
EOF
cp "$SRC_ROOT/scripts/start-rfeye.sh" "$APP_ROOT/start-rfeye.sh"
chmod +x "$APP_ROOT/start-rfeye.sh"
cat > "$TARGET_HOME/.config/labwc/autostart" <<EOF
#!/bin/sh
/usr/bin/kanshi &
sleep 2
pkill -x wf-panel-pi 2>/dev/null || true
pkill -x squeekboard 2>/dev/null || true
# RF Eye itself is managed by rfeye-user.service. Do not launch a second copy here.
EOF
cat > "$TARGET_HOME/.config/systemd/user/rfeye-user.service" <<EOF
[Unit]
Description=RF Eye user display service
After=default.target

[Service]
Type=simple
WorkingDirectory=${APP_ROOT}/rfeye
Environment=XDG_RUNTIME_DIR=/run/user/${TARGET_UID}
Environment=WAYLAND_DISPLAY=wayland-0
Environment=SDL_VIDEODRIVER=wayland
Environment=PYGAME_HIDE_SUPPORT_PROMPT=1
Environment=RFEYE_CONFIG=${TARGET_HOME}/.config/rfeye/config.json
ExecStart=/bin/bash -lc 'while [ ! -S /run/user/${TARGET_UID}/wayland-0 ]; do sleep 2; done; exec ${APP_ROOT}/start-rfeye.sh'
Restart=always
RestartSec=2

[Install]
WantedBy=default.target
EOF
ln -sfn ../rfeye-user.service "$TARGET_HOME/.config/systemd/user/default.target.wants/rfeye-user.service"
cat > "$TARGET_HOME/.config/autostart/squeekboard.desktop" <<'EOF'
[Desktop Entry]
Type=Application
Name=Squeekboard
Hidden=true
EOF
chown -R "$TARGET_USER:$TARGET_USER" "$TARGET_HOME/.config/labwc" "$TARGET_HOME/.config/autostart" "$TARGET_HOME/.config/kanshi" "$TARGET_HOME/.config/systemd"

echo "[6/8] Configuring permissions and autologin..."
usermod -aG video,render,input,gpio,plugdev "$TARGET_USER" || true
if command -v raspi-config >/dev/null 2>&1; then
  raspi-config nonint do_boot_behaviour B4 || true
fi
systemctl disable --now rfeye.service 2>/dev/null || true
loginctl enable-linger "$TARGET_USER" 2>/dev/null || true

echo "[7/8] Configuring Wi-Fi updater..."
MANIFEST_URL="https://raw.githubusercontent.com/${REPO_SLUG}/main/update/manifest.json"
python3 - "$APP_ROOT/rfeye/config.py" "$MANIFEST_URL" <<'PY'
from pathlib import Path
import sys
p=Path(sys.argv[1]); url=sys.argv[2]
s=p.read_text()
s=s.replace('"update_manifest_url": "",', f'"update_manifest_url": "{url}",')
p.write_text(s)
PY

VERSION="$(cat "$SRC_ROOT/VERSION")"
echo "Installed RF Eye ${VERSION}"
echo "[8/8] Finished."
cat <<EOF

RF Eye is installed.
Reboot with:
  sudo reboot

After reboot the Raspberry Pi desktop auto-login starts RF Eye fullscreen.
Updater manifest:
  ${MANIFEST_URL}
EOF
