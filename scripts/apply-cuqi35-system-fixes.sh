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

# SPI-only MHS35 exposes card0 but no renderD128. A full override is required:
# systemd drop-ins cannot reliably subtract an inherited device ordering list.
VENDOR_LIGHTDM=/usr/lib/systemd/system/lightdm.service
[[ -f "$VENDOR_LIGHTDM" ]] || VENDOR_LIGHTDM=/lib/systemd/system/lightdm.service
if [[ -f "$VENDOR_LIGHTDM" ]]; then
  install -m 0644 "$VENDOR_LIGHTDM" /etc/systemd/system/lightdm.service
  sed -i 's/[[:space:]]dev-dri-renderD128\.device//g' /etc/systemd/system/lightdm.service
  rm -rf /etc/systemd/system/lightdm.service.d/30-rfeye-spi-drm.conf
fi

# RF Eye needs the local display before it needs Wi-Fi. Raspberry Pi OS orders
# user sessions after network.target, which measured ~8.5 s on this unit. Copy
# the vendor unit and remove only that ordering so networking continues in the
# background while LightDM/Labwc/RF Eye start.
VENDOR_USERS=/usr/lib/systemd/system/systemd-user-sessions.service
[[ -f "$VENDOR_USERS" ]] || VENDOR_USERS=/lib/systemd/system/systemd-user-sessions.service
if [[ -f "$VENDOR_USERS" ]]; then
  install -m 0644 "$VENDOR_USERS" /etc/systemd/system/systemd-user-sessions.service
  sed -i '/^After=/ s/[[:space:]]network.target//g' /etc/systemd/system/systemd-user-sessions.service
  rm -rf /etc/systemd/system/systemd-user-sessions.service.d/20-rfeye-no-network.conf
fi

# No NFS mounts are used by the appliance. Do not let dormant NFS/RPC client
# targets pull network-online work back into the display critical path.
systemctl disable nfs-client.target nfs-blkmap.service rpcbind.service rpcbind.socket >/dev/null 2>&1 || true
systemctl mask nfs-client.target nfs-blkmap.service rpcbind.service rpcbind.socket rpc-statd-notify.service >/dev/null 2>&1 || true

# Build an RF Eye MHS35 overlay from this kernel's piscreen overlay. Keep the
# display/GPIO definitions but raise XPT2046 pressure-max so lighter taps work.
if command -v dtc >/dev/null 2>&1 && [[ -f "$OVERLAYS/piscreen.dtbo" ]]; then
  TMP="$(mktemp -d /tmp/rfeye-mhs35-overlay.XXXXXX)"
  trap 'rm -rf "$TMP"' EXIT
  dtc -I dtb -O dts -o "$TMP/piscreen.dts" "$OVERLAYS/piscreen.dtbo" 2>/dev/null || true
  python3 - "$TMP/piscreen.dts" <<'PY'
from pathlib import Path
import sys
p=Path(sys.argv[1])
if p.exists():
    s=p.read_text()
    s=s.replace('ti,pressure-max = [00 ff];','ti,pressure-max = [04 00];',1)
    p.write_text(s)
PY
  if [[ -s "$TMP/piscreen.dts" ]] && dtc -@ -I dts -O dtb -o "$OVERLAYS/rfeye-mhs35.dtbo" "$TMP/piscreen.dts" 2>/dev/null; then
    python3 - "$CONFIG" "$SPI_HZ" <<'PY'
from pathlib import Path
import sys
p=Path(sys.argv[1]); speed=sys.argv[2]; lines=[]
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

# Linux exposes XPT2046 through the ADS7846-compatible input driver. RF Eye
# reads evdev directly; keep Labwc mapped to the correct DRM output as fallback.
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
  python3 - "$SERVICE" <<'PY'
from pathlib import Path
import sys
p=Path(sys.argv[1]); out=[]
for line in p.read_text().splitlines():
    if line.startswith('Environment=SDL_TOUCH_MOUSE_EVENTS=') or line.startswith('Environment=SDL_MOUSE_TOUCH_EVENTS='):
        continue
    if line.startswith('ExecStart=') and 'wayland-0' in line and 'start-rfeye.sh' in line:
        line='ExecStart=/opt/rfeye/start-rfeye.sh'
    out.append(line)
insert=next((i+1 for i,x in enumerate(out) if x=='Environment=PYGAME_HIDE_SUPPORT_PROMPT=1'), None)
if insert is not None:
    out[insert:insert]=['Environment=SDL_TOUCH_MOUSE_EVENTS=0','Environment=SDL_MOUSE_TOUCH_EVENTS=0']
p.write_text('\n'.join(out).rstrip()+'\n')
PY
  chown "$TARGET_USER:$TARGET_USER" "$SERVICE"
fi

# RF Eye uses its GPIO buzzer, not desktop audio. Mask the unused multimedia
# user stack so it does not compete with Pygame during cold startup.
mkdir -p "$TARGET_HOME/.config/systemd/user"
for unit in pipewire.service pipewire-pulse.service wireplumber.service filter-chain.service mpris-proxy.service; do
  ln -sfn /dev/null "$TARGET_HOME/.config/systemd/user/$unit"
done
chown -R "$TARGET_USER:$TARGET_USER" "$TARGET_HOME/.config/systemd/user"

# Reinstall the branch Plymouth script. It centers using window-local width and
# height only, avoiding the non-zero output origin seen during fb0 -> fb1 handoff.
SRC_ROOT="${RFEYE_SOURCE_ROOT:-/opt/rfeye-src}"
THEME_DIR=/usr/share/plymouth/themes/rfeye
if [[ -f "$SRC_ROOT/config/plymouth/rfeye/rfeye.script" && -d "$THEME_DIR" ]]; then
  install -m 0644 "$SRC_ROOT/config/plymouth/rfeye/rfeye.script" "$THEME_DIR/rfeye.script"
  plymouth-set-default-theme -R rfeye
fi

systemctl daemon-reload
sync

echo '=== LightDM dependencies ==='
systemctl show lightdm.service -p Wants -p After --no-pager || true
if systemctl show lightdm.service -p Wants -p After --value 2>/dev/null | grep -q 'renderD128'; then
  echo 'ERROR: renderD128 dependency is still present.' >&2
  exit 1
fi

echo '=== User-session ordering ==='
systemctl show systemd-user-sessions.service -p After --no-pager || true
if systemctl show systemd-user-sessions.service -p After --value 2>/dev/null | grep -qw 'network.target'; then
  echo 'ERROR: network.target is still on the display critical path.' >&2
  exit 1
fi

echo 'RF Eye MHS35/CUQI system fixes installed. Reboot to activate them.'
