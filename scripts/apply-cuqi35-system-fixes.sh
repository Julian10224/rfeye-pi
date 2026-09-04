#!/usr/bin/env bash
set -euo pipefail

[[ $EUID -eq 0 ]] || { echo 'Run with sudo.' >&2; exit 1; }
TARGET_USER="${SUDO_USER:-$(logname 2>/dev/null || echo pi)}"
TARGET_HOME="$(getent passwd "$TARGET_USER" | cut -d: -f6)"
TARGET_UID="$(id -u "$TARGET_USER")"
[[ -n "$TARGET_HOME" ]] || { echo 'Could not determine target home.' >&2; exit 1; }

SRC_ROOT="${RFEYE_SOURCE_ROOT:-/opt/rfeye-src}"
BOOT=/boot/firmware
[[ -d "$BOOT" ]] || BOOT=/boot
CONFIG="$BOOT/config.txt"
OVERLAYS="$BOOT/overlays"
SPI_HZ="${RFEYE_CUQI_SPI_HZ:-18000000}"

# Install the exact systemd units measured on the reference 0.7.28 appliance.
# This removes the nonexistent renderD128 wait and keeps user sessions off the
# network critical path while retaining the normal local filesystem ordering.
install -m 0644 "$SRC_ROOT/config/systemd/lightdm.service" /etc/systemd/system/lightdm.service
install -m 0644 "$SRC_ROOT/config/systemd/systemd-user-sessions.service" /etc/systemd/system/systemd-user-sessions.service
rm -rf /etc/systemd/system/lightdm.service.d/30-rfeye-spi-drm.conf \
       /etc/systemd/system/systemd-user-sessions.service.d/20-rfeye-no-network.conf

# Rebuild the exact MHS35/XPT2046 Device Tree overlay from the committed DTS.
# The expected SHA is the byte-for-byte hash measured on the 0.7.28 reference Pi.
EXPECTED_OVERLAY_SHA="1727ca3c3161bd90db1cbc7a076dad692d34ee67c7acf70afab28fbf16fdec34"
mkdir -p "$OVERLAYS"
dtc -@ -I dts -O dtb -o "$OVERLAYS/rfeye-mhs35.dtbo" "$SRC_ROOT/config/overlays/rfeye-mhs35.dts"
echo "$EXPECTED_OVERLAY_SHA  $OVERLAYS/rfeye-mhs35.dtbo" | sha256sum -c -

# Rewrite only RF Eye display entries and leave unrelated Raspberry Pi settings
# intact. The resulting block matches the working reference Pi.
python3 - "$CONFIG" "$SPI_HZ" <<'PY'
from pathlib import Path
import sys
p=Path(sys.argv[1]); speed=sys.argv[2]
lines=p.read_text().splitlines(); out=[]; skip=False
for line in lines:
    st=line.strip()
    if st in {'# rfeye-display-start','# rfeye-cuqi35-display-start'}:
        skip=True; continue
    if skip and st in {'# rfeye-display-end','# rfeye-cuqi35-display-end'}:
        skip=False; continue
    if skip: continue
    if st.startswith(('dtoverlay=piscreen','dtoverlay=rfeye-mhs35','dtoverlay=mhs35','dtoverlay=tft35a','dtoverlay=ads7846')):
        continue
    if st.startswith('dtoverlay=vc4-kms-v3d') or st.startswith('dtoverlay=vc4-fkms-v3d'):
        out.append('# RF Eye CUQI disabled primary HDMI KMS: '+st)
        continue
    out.append(line)
for needed in ('auto_initramfs=1','disable_splash=1'):
    if not any(x.strip()==needed for x in out): out.append(needed)
out += ['', '# rfeye-cuqi35-display-start',
        '# MHS35 3.5in 480x320: ILI9486/piscreen SPI DRM + XPT2046 touch',
        'dtparam=spi=on', '# rfeye-cuqi35-display-end',
        f'dtoverlay=rfeye-mhs35,speed={speed},drm,rotate=0,xohms=60']
p.write_text('\n'.join(out).rstrip()+'\n')
PY

# Reproduce the reference graphical session and RF Eye user service. Only UID
# and home directory are substituted so installations under another username
# remain valid while behavior stays identical.
mkdir -p "$TARGET_HOME/.config/labwc" "$TARGET_HOME/.config/kanshi" \
         "$TARGET_HOME/.config/systemd/user/default.target.wants" \
         "$TARGET_HOME/.config/systemd/user/rfeye-user.service.d"
install -m 0755 "$SRC_ROOT/config/labwc-autostart" "$TARGET_HOME/.config/labwc/autostart"
install -m 0644 "$SRC_ROOT/config/labwc/rc.xml" "$TARGET_HOME/.config/labwc/rc.xml"
install -m 0644 "$SRC_ROOT/config/kanshi-config" "$TARGET_HOME/.config/kanshi/config"
sed -e "s|@UID@|${TARGET_UID}|g" -e "s|@HOME@|${TARGET_HOME}|g" \
  "$SRC_ROOT/config/systemd/rfeye-user.service.in" > "$TARGET_HOME/.config/systemd/user/rfeye-user.service"
install -m 0644 "$SRC_ROOT/config/systemd/rfeye-user-fast-ui.conf" \
  "$TARGET_HOME/.config/systemd/user/rfeye-user.service.d/20-rfeye-fast-ui.conf"
ln -sfn ../rfeye-user.service "$TARGET_HOME/.config/systemd/user/default.target.wants/rfeye-user.service"

# RF Eye uses the GPIO buzzer, not desktop audio. Match the reference appliance
# and keep unused multimedia/NFS/RPC services out of the startup path.
for unit in pipewire.service pipewire-pulse.service wireplumber.service filter-chain.service mpris-proxy.service; do
  ln -sfn /dev/null "$TARGET_HOME/.config/systemd/user/$unit"
done
systemctl disable nfs-client.target nfs-blkmap.service rpcbind.service rpcbind.socket >/dev/null 2>&1 || true
systemctl mask nfs-client.target nfs-blkmap.service rpcbind.service rpcbind.socket rpc-statd-notify.service >/dev/null 2>&1 || true

chown -R "$TARGET_USER:$TARGET_USER" "$TARGET_HOME/.config/labwc" "$TARGET_HOME/.config/kanshi" "$TARGET_HOME/.config/systemd"

# Keep the same Plymouth script/assets and regenerate initramfs so the splash is
# present from cold boot through the SPI framebuffer handoff.
THEME_DIR=/usr/share/plymouth/themes/rfeye
if [[ -d "$THEME_DIR" ]]; then
  install -m 0644 "$SRC_ROOT/config/plymouth/rfeye/rfeye.script" "$THEME_DIR/rfeye.script"
  python3 "$SRC_ROOT/scripts/generate-plymouth-assets-cuqi35.py" "$THEME_DIR"
  chmod 0644 "$THEME_DIR"/*.png
  plymouth-set-default-theme -R rfeye
fi

systemctl daemon-reload
sync

# Hard verification of the startup fixes that made the reference Pi fast.
if systemctl show lightdm.service -p Wants -p After --value 2>/dev/null | grep -q 'renderD128'; then
  echo 'ERROR: renderD128 dependency is still present.' >&2; exit 1
fi
if systemctl show systemd-user-sessions.service -p After --value 2>/dev/null | grep -qw 'network.target'; then
  echo 'ERROR: network.target is still on the display critical path.' >&2; exit 1
fi
echo "1727ca3c3161bd90db1cbc7a076dad692d34ee67c7acf70afab28fbf16fdec34  $OVERLAYS/rfeye-mhs35.dtbo" | sha256sum -c -
grep -q '^ExecStart=/opt/rfeye/start-rfeye.sh$' "$TARGET_HOME/.config/systemd/user/rfeye-user.service" || {
  echo 'ERROR: RF Eye direct startup service was not installed.' >&2; exit 1;
}

echo 'RF Eye reference system fixes installed. Reboot to activate them.'
