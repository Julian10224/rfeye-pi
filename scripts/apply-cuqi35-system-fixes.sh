#!/usr/bin/env bash
set -euo pipefail

[[ $EUID -eq 0 ]] || { echo 'Run with sudo.' >&2; exit 1; }
TARGET_USER="${SUDO_USER:-$(logname 2>/dev/null || echo pi)}"
TARGET_HOME="$(getent passwd "$TARGET_USER" | cut -d: -f6)"
[[ -n "$TARGET_HOME" ]] || { echo 'Could not determine target home.' >&2; exit 1; }

# MHS35/piscreen DRM exposes /dev/dri/card0 but no renderD128. Raspberry Pi OS
# LightDM waits roughly 90 seconds for renderD128 unless that dependency is
# replaced for this SPI-only appliance profile.
mkdir -p /etc/systemd/system/lightdm.service.d
cat > /etc/systemd/system/lightdm.service.d/30-rfeye-spi-drm.conf <<'EOF'
[Unit]
Wants=
Wants=dev-dri-card0.device
After=
After=systemd-user-sessions.service dev-dri-card0.device plymouth-quit.service
EOF

BOOT=/boot/firmware
[[ -d "$BOOT" ]] || BOOT=/boot
CONFIG="$BOOT/config.txt"
python3 - "$CONFIG" <<'PY'
from pathlib import Path
import sys
p=Path(sys.argv[1]); s=p.read_text()
lines=[]
for line in s.splitlines():
    if line.strip().startswith('dtoverlay=piscreen') and 'drm' in line:
        parts=[x for x in line.strip().split(',') if not x.startswith('xohms=')]
        parts.append('xohms=60')
        line=','.join(parts)
    lines.append(line)
p.write_text('\n'.join(lines).rstrip()+'\n')
PY

# Linux reports XPT2046 through the ADS7846-compatible driver. Keep Labwc
# aware of the correct SPI connector even though RF Eye reads evdev directly.
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

THEME=/usr/share/plymouth/themes/rfeye/rfeye.script
if [[ -f "$THEME" ]]; then
cat > "$THEME" <<'PLY'
Window.SetBackgroundTopColor(0, 0, 0);
Window.SetBackgroundBottomColor(0, 0, 0);
logo.image = Image("splash.png"); logo.sprite = Sprite(logo.image);
progress_box.image = Image("progress_box.png"); progress_box.sprite = Sprite(progress_box.image);
progress_bar.original_image = Image("progress_bar.png"); progress_bar.sprite = Sprite();
fun refresh_callback()
{
  sx=Window.GetX(); sy=Window.GetY(); sw=Window.GetWidth(); sh=Window.GetHeight();
  lx=sx+(sw-logo.image.GetWidth())/2; ly=sy+(sh-logo.image.GetHeight())/2;
  logo.sprite.SetPosition(lx,ly,-10);
  bx=sx+((sw-progress_box.image.GetWidth())/2)-28; by=sy+(sh-progress_box.image.GetHeight())/2;
  progress_box.sprite.SetPosition(bx,by,0); progress_bar.sprite.SetPosition(bx+4,by+4,1);
}
Plymouth.SetRefreshFunction(refresh_callback); refresh_callback();
fun progress_callback(duration, progress)
{
  visible=progress*1.20; if (visible<0.0) visible=0.0; if (visible>0.96) visible=0.96;
  height=Math.Int(progress_bar.original_image.GetHeight()*visible); if (height<3) height=3;
  progress_bar.image=progress_bar.original_image.Scale(progress_bar.original_image.GetWidth(),height);
  progress_bar.sprite.SetImage(progress_bar.image);
}
Plymouth.SetBootProgressFunction(progress_callback);
PLY
  plymouth-set-default-theme -R rfeye
fi

systemctl daemon-reload
sync
echo 'RF Eye MHS35/CUQI system fixes installed. Reboot to activate them.'
