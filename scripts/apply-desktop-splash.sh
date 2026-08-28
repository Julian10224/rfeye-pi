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
  python3 "$(dirname "$0")/generate-plymouth-assets.py" /tmp/rfeye-desktop-assets
  install -m 0644 /tmp/rfeye-desktop-assets/splash.png "$WALLPAPER"
fi

python3 - "$TARGET_HOME" "$WALLPAPER" "$EMPTY_DESKTOP" <<'PY'
from pathlib import Path
import sys

home = Path(sys.argv[1])
wallpaper = sys.argv[2]
empty_desktop = sys.argv[3]
values = {
    'wallpaper_mode': 'crop',
    'wallpaper_common': '1',
    'wallpaper': wallpaper,
    'desktop_bg': '#000000',
    'show_wm_menu': '0',
    'folder': empty_desktop,
    'show_documents': '0',
    'show_home': '0',
    'show_trash': '0',
    'show_mounts': '0',
}

paths = []
for root in (Path('/etc/xdg/pcmanfm'), home / '.config/pcmanfm'):
    if root.exists():
        paths.extend(root.rglob('desktop-items-*.conf'))

# Ensure both standard profiles exist even on a fresh image.
for profile in ('default', 'LXDE-pi'):
    p = home / '.config/pcmanfm' / profile / 'desktop-items-0.conf'
    if not p.exists():
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text('[*]\n')
        paths.append(p)

for path in sorted(set(paths)):
    lines = path.read_text().splitlines() if path.exists() else ['[*]']
    if not lines:
        lines = ['[*]']
    if not any(line.strip() == '[*]' for line in lines):
        lines.insert(0, '[*]')

    seen = set()
    out = []
    for line in lines:
        if '=' in line and not line.lstrip().startswith('#'):
            key = line.split('=', 1)[0].strip()
            if key in values:
                out.append(f'{key}={values[key]}')
                seen.add(key)
                continue
        out.append(line)
    for key, value in values.items():
        if key not in seen:
            out.append(f'{key}={value}')
    path.write_text('\n'.join(out).rstrip() + '\n')
    print('RF Eye desktop config:', path)
PY

chown -R "$TARGET_USER:$TARGET_USER" "$TARGET_HOME/.config/pcmanfm" "$EMPTY_DESKTOP" 2>/dev/null || true

# Stop the desktop process itself. This prevents a per-output PCManFM config
# generated during login from briefly drawing Trash/Wastebasket or mounts.
pkill -x pcmanfm 2>/dev/null || true
pkill -x pcmanfm-pi 2>/dev/null || true

echo "RF Eye desktop transition installed."
echo "Wallpaper: $WALLPAPER"
echo "Desktop icons: disabled on all detected outputs"
echo "Desktop folder: $EMPTY_DESKTOP"
