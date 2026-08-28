#!/usr/bin/env bash
set -euo pipefail

if [[ $EUID -ne 0 ]]; then
  echo "Run with sudo: sudo $0"
  exit 1
fi

TARGET_USER="${1:-${SUDO_USER:-julian}}"
TARGET_HOME="${2:-$(getent passwd "$TARGET_USER" | cut -d: -f6)}"
THEME_DIR="${3:-/usr/share/plymouth/themes/rfeye}"
WALL_DIR=/usr/share/rfeye
WALLPAPER="$WALL_DIR/rfeye-desktop-splash.png"
EMPTY_DESKTOP="$TARGET_HOME/.local/share/rfeye-empty-desktop"

mkdir -p "$WALL_DIR" "$EMPTY_DESKTOP"

if [[ -f "$THEME_DIR/splash.png" ]]; then
  install -m 0644 "$THEME_DIR/splash.png" "$WALLPAPER"
else
  python3 - "$WALLPAPER" <<'PY'
from pathlib import Path
import sys
from PIL import Image, ImageDraw, ImageFont

out = Path(sys.argv[1])
W, H = 800, 480
blue = (28, 190, 255)
dim = (86, 126, 146)
img = Image.new('RGB', (W, H), (0, 0, 0))
draw = ImageDraw.Draw(img)
fonts = [
    '/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf',
    '/usr/share/fonts/truetype/freefont/FreeSansBold.ttf',
]
font_path = next((p for p in fonts if Path(p).exists()), None)
if font_path:
    title = ImageFont.truetype(font_path, 72)
    subtitle = ImageFont.truetype(font_path, 18)
else:
    title = subtitle = ImageFont.load_default()

def centered(text, y, font, fill):
    box = draw.textbbox((0, 0), text, font=font)
    x = (W - (box[2] - box[0])) // 2
    draw.text((x, y), text, font=font, fill=fill)

centered('RF EYE', 170, title, blue)
centered('STARTING SYSTEM', 266, subtitle, dim)
img.save(out)
PY
  chmod 0644 "$WALLPAPER"
fi

set_conf() {
  local conf="$1"
  mkdir -p "$(dirname "$conf")"
  python3 - "$conf" "$WALLPAPER" "$EMPTY_DESKTOP" <<'PY'
from pathlib import Path
import sys

path = Path(sys.argv[1])
wallpaper = sys.argv[2]
empty_desktop = sys.argv[3]

lines = path.read_text().splitlines() if path.exists() else ['[*]']
if not lines:
    lines = ['[*]']
if not any(line.strip() == '[*]' for line in lines):
    lines.insert(0, '[*]')

values = {
    'wallpaper_mode': 'crop',
    'wallpaper_common': '1',
    'wallpaper': wallpaper,
    'desktop_bg': '#000000',
    'show_wm_menu': '0',
    'folder': empty_desktop,
    'show_documents': '0',
    'show_trash': '0',
    'show_mounts': '0',
}

seen = set()
out = []
in_global = False
for line in lines:
    stripped = line.strip()
    if stripped.startswith('[') and stripped.endswith(']'):
        if in_global:
            for key, value in values.items():
                if key not in seen:
                    out.append(f'{key}={value}')
        in_global = stripped == '[*]'
        if in_global:
            seen = set()
        out.append(line)
        continue
    if in_global and '=' in line:
        key = line.split('=', 1)[0].strip()
        if key in values:
            out.append(f'{key}={values[key]}')
            seen.add(key)
            continue
    out.append(line)

if in_global:
    for key, value in values.items():
        if key not in seen:
            out.append(f'{key}={value}')

path.write_text('\n'.join(out).rstrip() + '\n')
PY
}

for profile in default LXDE-pi; do
  set_conf "/etc/xdg/pcmanfm/${profile}/desktop-items-0.conf"
  [[ ! -f "/etc/xdg/pcmanfm/${profile}/desktop-items-1.conf" ]] || \
    set_conf "/etc/xdg/pcmanfm/${profile}/desktop-items-1.conf"
  set_conf "$TARGET_HOME/.config/pcmanfm/${profile}/desktop-items-0.conf"
done

chown -R "$TARGET_USER:$TARGET_USER" "$TARGET_HOME/.config/pcmanfm" "$EMPTY_DESKTOP"

# Refresh an already running desktop when possible. A reboot is still the
# definitive test because the goal is to mask the short login transition.
TARGET_UID="$(id -u "$TARGET_USER")"
sudo -u "$TARGET_USER" env \
  DISPLAY="${DISPLAY:-:0}" \
  XDG_RUNTIME_DIR="/run/user/${TARGET_UID}" \
  pcmanfm --set-wallpaper="$WALLPAPER" --wallpaper-mode=crop \
  >/dev/null 2>&1 || true
sudo -u "$TARGET_USER" env \
  DISPLAY="${DISPLAY:-:0}" \
  XDG_RUNTIME_DIR="/run/user/${TARGET_UID}" \
  pcmanfm --reconfigure >/dev/null 2>&1 || true

echo "RF Eye desktop transition installed."
echo "Wallpaper: $WALLPAPER"
echo "Desktop icons: disabled"
echo "Desktop folder: $EMPTY_DESKTOP"
echo "Reboot to verify the complete boot transition."
