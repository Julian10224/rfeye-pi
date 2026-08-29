#!/usr/bin/env bash
set -euo pipefail

# RF Eye installer for the MHS35/CUQI 3.5-inch 480x320 GPIO SPI touchscreen.
# The display uses the Raspberry Pi kernel piscreen DRM stack; XPT2046 touch is
# handled by the ADS7846-compatible kernel driver plus RF Eye direct evdev input.

REPO_SLUG="${RFEYE_REPO:-Julian10224/rfeye-pi}"
REPO_BRANCH="${RFEYE_BRANCH:-main}"
SPI_HZ="${RFEYE_CUQI_SPI_HZ:-18000000}"
APP_ROTATION="${RFEYE_ROTATION:-cw}"

if [[ $EUID -ne 0 ]]; then
  echo "Run this installer with sudo."
  exit 1
fi

case "$APP_ROTATION" in
  cw|ccw) ;;
  *) echo "RFEYE_ROTATION must be cw or ccw"; exit 1 ;;
esac

TARGET_USER="${SUDO_USER:-$(logname 2>/dev/null || echo pi)}"
TARGET_HOME="$(getent passwd "$TARGET_USER" | cut -d: -f6)"
[[ -n "$TARGET_HOME" ]] || { echo "Could not determine home directory for $TARGET_USER"; exit 1; }

echo "[MHS35 1/8] Preparing RF Eye main firmware..."
apt-get update
DEBIAN_FRONTEND=noninteractive apt-get install -y git python3 libinput-tools evtest device-tree-compiler

TMP_ROOT="$(mktemp -d /tmp/rfeye-mhs35.XXXXXX)"
cleanup() { rm -rf "$TMP_ROOT"; }
trap cleanup EXIT

git clone --depth 1 --branch "$REPO_BRANCH" "https://github.com/${REPO_SLUG}.git" "$TMP_ROOT/src"

echo "[MHS35 2/8] Installing RF Eye appliance core..."
RFEYE_LOCAL_SOURCE="$TMP_ROOT/src" RFEYE_REPO="$REPO_SLUG" bash "$TMP_ROOT/src/install.sh"
# Start importing Pygame while Labwc is still coming up and defer SDR/NumPy
# until after the display path is ready. OTA builds apply the same transform.
python3 "$TMP_ROOT/src/scripts/patch-fast-app-start.py" /opt/rfeye/rfeye/app.py
python3 -m py_compile /opt/rfeye/rfeye/app.py

BOOT=/boot/firmware
[[ -d "$BOOT" ]] || BOOT=/boot
CONFIG="$BOOT/config.txt"
cp -n "$CONFIG" "${CONFIG}.rfeye-mhs35-backup" || true

echo "[MHS35 3/8] Configuring native 480x320 SPI/DRM display..."
python3 - "$CONFIG" "$SPI_HZ" <<'PY'
from pathlib import Path
import sys

path = Path(sys.argv[1])
speed = sys.argv[2]
lines = path.read_text().splitlines()
out = []
skip = False
for line in lines:
    stripped = line.strip()
    if stripped in {"# rfeye-display-start", "# rfeye-cuqi35-display-start"}:
        skip = True
        continue
    if skip and stripped in {"# rfeye-display-end", "# rfeye-cuqi35-display-end"}:
        skip = False
        continue
    if skip:
        continue
    if stripped.startswith((
        "dtoverlay=piscreen", "dtoverlay=rfeye-mhs35",
        "dtoverlay=mhs35", "dtoverlay=tft35a", "dtoverlay=ads7846",
    )):
        continue
    if stripped.startswith("dtoverlay=vc4-kms-v3d") or stripped.startswith("dtoverlay=vc4-fkms-v3d"):
        out.append("# RF Eye MHS35 disabled primary HDMI KMS: " + stripped)
        continue
    out.append(line)

out += [
    "", "# rfeye-cuqi35-display-start",
    "# MHS35 3.5in 480x320: ILI9486/piscreen SPI DRM + XPT2046 touch",
    "dtparam=spi=on",
    f"dtoverlay=piscreen,speed={speed},drm,rotate=0,xohms=60",
    "# rfeye-cuqi35-display-end",
]
path.write_text("\n".join(out).rstrip() + "\n")
PY

rm -f /usr/share/X11/xorg.conf.d/99-fbturbo.conf \
      /usr/share/X11/xorg.conf.d/99-fbdev.conf 2>/dev/null || true

mkdir -p "$TARGET_HOME/.config/kanshi"
cat > "$TARGET_HOME/.config/kanshi/config" <<'EOF'
# MHS35 SPI panel uses its native DRM mode. No HDMI override is required.
EOF

echo "[MHS35 4/8] Applying native 320x480 portrait UI and XPT2046 profile..."
CFG_DIR="$TARGET_HOME/.config/rfeye"
CFG_FILE="$CFG_DIR/config.json"
mkdir -p "$CFG_DIR"
python3 - "$CFG_FILE" "$APP_ROTATION" <<'PY'
from pathlib import Path
import json, sys
p = Path(sys.argv[1]); rotation = sys.argv[2]
try:
    cfg = json.loads(p.read_text()) if p.exists() else {}
except Exception:
    cfg = {}
cfg.update({
    "display_profile": "cuqi35",
    "ui_width": 320, "ui_height": 480,
    "physical_width": 480, "physical_height": 320,
    "rotation": rotation,
    "ui_fps": 20,
    "fullscreen": True,
    "touch_invert_x": False,
    "touch_invert_y": False,
})
p.write_text(json.dumps(cfg, indent=2) + "\n")
PY
chown -R "$TARGET_USER:$TARGET_USER" "$CFG_DIR" "$TARGET_HOME/.config/kanshi"

SERVICE="$TARGET_HOME/.config/systemd/user/rfeye-user.service"
if [[ -f "$SERVICE" ]]; then
  sed -i '/^Environment=RFEYE_DISPLAY_PROFILE=/d' "$SERVICE"
  sed -i '/^Environment=PYGAME_HIDE_SUPPORT_PROMPT=1$/a Environment=RFEYE_DISPLAY_PROFILE=cuqi35' "$SERVICE"
  chown "$TARGET_USER:$TARGET_USER" "$SERVICE"
fi

LABWC="$TARGET_HOME/.config/labwc/autostart"
if [[ -f "$LABWC" ]]; then
  sed -i '/\/usr\/bin\/kanshi[[:space:]]*&/d' "$LABWC"
fi

# Apply measured MHS35 fixes: no nonexistent renderD128 wait, no network wait
# before the local display, lighter XPT2046 taps, direct touch, no unused audio
# stack on the critical path, and stable Plymouth centering across fb handoff.
RFEYE_SOURCE_ROOT="$TMP_ROOT/src" RFEYE_CUQI_SPI_HZ="$SPI_HZ" \
  bash "$TMP_ROOT/src/scripts/apply-cuqi35-system-fixes.sh"

echo "[MHS35 5/8] Rebuilding boot splash for 480x320..."
THEME_DIR=/usr/share/plymouth/themes/rfeye
if [[ -d "$THEME_DIR" ]]; then
  install -m 0644 "$TMP_ROOT/src/config/plymouth/rfeye/rfeye.script" "$THEME_DIR/rfeye.script"
  python3 "$TMP_ROOT/src/scripts/generate-plymouth-assets-cuqi35.py" "$THEME_DIR"
  chmod 0644 "$THEME_DIR"/*.png
  plymouth-set-default-theme -R rfeye
fi

echo "[MHS35 6/8] Installing display diagnostics..."
cat > /usr/local/bin/rfeye-cuqi35-status <<'EOF'
#!/usr/bin/env bash
set -u
echo "=== RF Eye MHS35 display status ==="
echo "Model: $(tr -d '\0' </proc/device-tree/model 2>/dev/null || echo unknown)"
echo ""
echo "DRM connectors:"
for s in /sys/class/drm/card*-*/status; do
  [[ -e "$s" ]] || continue
  printf '  %-28s %s\n' "$(basename "$(dirname "$s")")" "$(cat "$s")"
done
echo ""
echo "Framebuffers / DRM:"
ls -l /dev/fb* /dev/dri/* 2>/dev/null || true
echo ""
echo "Touch devices:"
grep -B1 -A6 -Ei 'ADS7846|XPT2046|Touchscreen' /proc/bus/input/devices 2>/dev/null || true
echo ""
echo "LightDM render dependency:"
systemctl show lightdm.service -p Wants -p After --no-pager 2>/dev/null || true
echo ""
echo "User-session ordering:"
systemctl show systemd-user-sessions.service -p After --no-pager 2>/dev/null || true
echo ""
echo "Boot timing:"
systemd-analyze 2>/dev/null || true
echo ""
echo "Kernel display/touch messages:"
dmesg | grep -iE 'ili9486|piscreen|ads7846|spi|drm' | tail -n 80 || true
EOF
chmod +x /usr/local/bin/rfeye-cuqi35-status

echo "[MHS35 7/8] Verifying configuration..."
grep -Eq '^dtoverlay=(rfeye-mhs35|piscreen),.*drm' "$CONFIG" || {
  echo "ERROR: MHS35 DRM overlay was not written to $CONFIG"; exit 1;
}
grep -q 'RFEYE_DISPLAY_PROFILE=cuqi35' "$SERVICE" || {
  echo "ERROR: compact display profile was not added to RF Eye service"; exit 1;
}
python3 - "$CFG_FILE" <<'PY'
import json, sys
cfg=json.load(open(sys.argv[1]))
assert (cfg.get('ui_width'),cfg.get('ui_height')) == (320,480)
assert (cfg.get('physical_width'),cfg.get('physical_height')) == (480,320)
PY

sync
echo "[MHS35 8/8] Done."
cat <<EOF

RF Eye MHS35 3.5 portrait firmware is installed.

Display:  MHS35-compatible 3.5 inch SPI touchscreen
Touch:    XPT2046 (Linux ADS7846 driver), direct calibrated RF Eye input
Native:   480x320 physical / 320x480 portrait UI
SPI:      ${SPI_HZ} Hz
Branch:   ${REPO_BRANCH}

Reboot now:
  sudo reboot

After reboot, diagnostics are available with:
  sudo rfeye-cuqi35-status

If the image is upside-down, reinstall with:
  curl -fsSL https://raw.githubusercontent.com/${REPO_SLUG}/${REPO_BRANCH}/install-cuqi35.sh | sudo env RFEYE_ROTATION=ccw bash
EOF
