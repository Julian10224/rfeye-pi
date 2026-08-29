#!/usr/bin/env bash
set -euo pipefail

[[ $EUID -eq 0 ]] || { echo 'Run with sudo.' >&2; exit 1; }
TARGET_USER="${SUDO_USER:-$(logname 2>/dev/null || echo pi)}"
TARGET_HOME="$(getent passwd "$TARGET_USER" | cut -d: -f6)"
[[ -n "$TARGET_HOME" ]] || { echo 'Could not determine target home.' >&2; exit 1; }

BOOT=/boot/firmware
[[ -d "$BOOT" ]] || BOOT=/boot
CONFIG="$BOOT/config.txt"
OVERLAYS="$BOOT/overlays"
SPI_HZ="${RFEYE_CUQI_SPI_HZ:-18000000}"

# Raspberry Pi OS LightDM wants both card0 and renderD128. The SPI-only MHS35
# DRM driver creates card0 but never creates renderD128, causing the measured
# ~89 second device timeout before LightDM starts. Dependency lists from the
# vendor unit cannot reliably be subtracted with a drop-in, so install a full
# /etc override and remove only the nonexistent render node.
VENDOR_LIGHTDM=/usr/lib/systemd/system/lightdm.service
[[ -f "$VENDOR_LIGHTDM" ]] || VENDOR_LIGHTDM=/lib/systemd/system/lightdm.service
if [[ -f "$VENDOR_LIGHTDM" ]]; then
  cp "$VENDOR_LIGHTDM" /etc/systemd/system/lightdm.service
  sed -i 's/[[:space:]]dev-dri-renderD128\.device//g' /etc/systemd/system/lightdm.service
  rm -rf /etc/systemd/system/lightdm.service.d/30-rfeye-spi-drm.conf
fi

# Build an RF Eye MHS35 overlay from the kernel's matching piscreen overlay.
# It keeps the exact display/GPIO definitions from this OS image but raises
# ADS7846/XPT2046 pressure-max from 255 to 1024 so lighter taps are accepted.
if command -v dtc >/dev/null 2>&1 && [[ -f "$OVERLAYS/piscreen.dtbo" ]]; then
  TMP="$(mktemp -d /tmp/rfeye-mhs35-overlay.XXXXXX)"
  trap 'rm -rf "$TMP"' EXIT
  dtc -I dtb -O dts -o "$TMP/piscreen.dts" "$OVERLAYS/piscreen.dtbo" 2>/dev/null || true
  python3 - "$TMP/piscreen.dts" <<'PY'
from pathlib import Path
import sys
p=Path(sys.argv[1])
if not p.exists(): raise SystemExit(0)
s=p.read_text()
old='ti,pressure-max = [00 ff];'
if old in s:
    p.write_text(s.replace(old,'ti,pressure-max = [04 00];',1))
PY
  if [[ -s "$TMP/piscreen.dts" ]] && dtc -@ -I dts -O dtb -o "$OVERLAYS/rfeye-mhs35.dtbo" "$TMP/piscreen.dts" 2>/dev/null; then
    python3 - "$CONFIG" "$SPI_HZ" <<'PY'
from pathlib import Path
import sys
p=Path(sys.argv[1]); speed=sys.argv[2]
lines=[]
for line in p.read_text().splitlines():
    st=line.strip()
    if st.startswith('dtoverlay=piscreen') or st.startswith('dtoverlay=rfeye-mhs35'):
        continue
    lines.append(line)
lines.append(f'dtoverlay=rfeye-mhs35,speed={speed},drm,rotate=0,xohms=60')
p.write_text('\n'.join(lines).rstrip()+'\n')
PY
  fi
fi

# Linux calls XPT2046 an ADS7846-compatible touchscreen. Keep Labwc mapped to
# SPI-1, but RF Eye itself reads evdev directly with MHS35 calibration.
mkdir -p "$TARGET_HOME/.config/labwc"
cat > "$TARGET_HOME/.config/labwc/rc.xml" <<'EOF'
<?xml version="1.0"?>
<openbox_config xmlns="http://openbox.org/3.4/rc">
  <touch deviceName="ADS7846 Touchscreen" mapToOutput="SPI-1" mouseEmulation="yes"/>
</openbox_config>
EOF
chown "$TARGET_USER:$TARGET_USER" "$TARGET_HOME/.config/labwc/rc.xml"

SERVICE="$TARGET_HOME/.config/systemd/user/rfeye-user.service"
if [[ -f "$SERVICE" ]]; then
  sed -i '/^Environment=SDL_TOUCH_MOUSE_EVENTS=/d;/^Environment=SDL_MOUSE_TOUCH_EVENTS=/d' "$SERVICE"
  sed -i '/^Environment=PYGAME_HIDE_SUPPORT_PROMPT=1$/a Environment=SDL_TOUCH_MOUSE_EVENTS=0\nEnvironment=SDL_MOUSE_TOUCH_EVENTS=0' "$SERVICE"
  chown "$TARGET_USER:$TARGET_USER" "$SERVICE"
fi

# Reinstall the repository Plymouth script. It recenters every refresh because
# this Pi changes from a 720x480 firmware framebuffer to the 480x320 SPI DRM
# framebuffer during boot; one-time coordinates visibly jump down/right.
SRC_ROOT="${RFEYE_SOURCE_ROOT:-/opt/rfeye-src}"
THEME_DIR=/usr/share/plymouth/themes/rfeye
if [[ -f "$SRC_ROOT/config/plymouth/rfeye/rfeye.script" && -d "$THEME_DIR" ]]; then
  install -m 0644 "$SRC_ROOT/config/plymouth/rfeye/rfeye.script" "$THEME_DIR/rfeye.script"
  plymouth-set-default-theme -R rfeye
fi

systemctl daemon-reload
sync
echo 'RF Eye MHS35/CUQI system fixes installed. Reboot to activate them.'
