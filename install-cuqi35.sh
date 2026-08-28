#!/usr/bin/env bash
set -euo pipefail

# RF Eye installer for the CUQI RPM-01 / common 3.5-inch 480x320 GPIO SPI
# touchscreen family. The panel is driven through the Raspberry Pi kernel's
# piscreen DRM driver instead of the legacy fbtft/LCD-show graphics stack.

REPO_SLUG="${RFEYE_REPO:-Julian10224/rfeye-pi}"
REPO_BRANCH="${RFEYE_BRANCH:-display-cuqi-35-portrait}"
SPI_HZ="${RFEYE_CUQI_SPI_HZ:-18000000}"
TOUCH_OPTS="${RFEYE_CUQI_TOUCH_OPTS:-}"
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

echo "[CUQI 1/8] Preparing RF Eye display fork..."
apt-get update
DEBIAN_FRONTEND=noninteractive apt-get install -y git python3 libinput-tools evtest

TMP_ROOT="$(mktemp -d /tmp/rfeye-cuqi35.XXXXXX)"
cleanup() { rm -rf "$TMP_ROOT"; }
trap cleanup EXIT

git clone --depth 1 --branch "$REPO_BRANCH" "https://github.com/${REPO_SLUG}.git" "$TMP_ROOT/src"

# Reuse the normal RF Eye appliance installer so SDR, Wi-Fi, updater,
# autologin, splash infrastructure and permissions stay aligned with main.
echo "[CUQI 2/8] Installing RF Eye appliance core..."
RFEYE_LOCAL_SOURCE="$TMP_ROOT/src" RFEYE_REPO="$REPO_SLUG" bash "$TMP_ROOT/src/install.sh"

BOOT=/boot/firmware
[[ -d "$BOOT" ]] || BOOT=/boot
CONFIG="$BOOT/config.txt"
cp -n "$CONFIG" "${CONFIG}.rfeye-cuqi35-backup" || true

echo "[CUQI 3/8] Configuring native 480x320 SPI/DRM display..."
python3 - "$CONFIG" "$SPI_HZ" "$TOUCH_OPTS" <<'PY'
from pathlib import Path
import sys

path = Path(sys.argv[1])
speed = sys.argv[2]
touch = sys.argv[3].strip().strip(',')
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

    # Remove legacy/duplicate LCD overlays that can claim the same SPI/GPIOs.
    if stripped.startswith((
        "dtoverlay=piscreen",
        "dtoverlay=mhs35",
        "dtoverlay=tft35a",
        "dtoverlay=ads7846",
    )):
        continue

    # Make the SPI DRM panel the appliance's primary graphics device. The
    # piscreen overlay itself creates a DRM card suitable for labwc/Wayland.
    if stripped.startswith("dtoverlay=vc4-kms-v3d") or stripped.startswith("dtoverlay=vc4-fkms-v3d"):
        out.append("# RF Eye CUQI disabled primary HDMI KMS: " + stripped)
        continue
    out.append(line)

# Keep the controller in native landscape orientation. RF Eye rotates its
# native 320x480 portrait surface once into the 480x320 framebuffer.
overlay = f"dtoverlay=piscreen,speed={speed},drm,rotate=0"
if touch:
    overlay += "," + touch

out += [
    "",
    "# rfeye-cuqi35-display-start",
    "# CUQI 3.5in 480x320: ILI9486/piscreen-family SPI panel + resistive touch",
    "dtparam=spi=on",
    overlay,
    "# rfeye-cuqi35-display-end",
]
path.write_text("\n".join(out).rstrip() + "\n")
PY

# Old vendor LCD-show packages can leave Xorg snippets that conflict with
# Bookworm/Trixie graphics. They are not needed by the native DRM route.
rm -f /usr/share/X11/xorg.conf.d/99-fbturbo.conf \
      /usr/share/X11/xorg.conf.d/99-fbdev.conf 2>/dev/null || true

# The generic installer creates an HDMI-specific kanshi profile. The SPI DRM
# panel advertises its own native mode and does not need an HDMI override.
mkdir -p "$TARGET_HOME/.config/kanshi"
cat > "$TARGET_HOME/.config/kanshi/config" <<'EOF'
# CUQI SPI panel uses its native DRM mode. No HDMI mode override is required.
EOF

echo "[CUQI 4/8] Applying native 320x480 portrait UI profile..."
CFG_DIR="$TARGET_HOME/.config/rfeye"
CFG_FILE="$CFG_DIR/config.json"
mkdir -p "$CFG_DIR"
python3 - "$CFG_FILE" "$APP_ROTATION" <<'PY'
from pathlib import Path
import json, sys

p = Path(sys.argv[1])
rotation = sys.argv[2]
try:
    cfg = json.loads(p.read_text()) if p.exists() else {}
except Exception:
    cfg = {}

cfg.update({
    "display_profile": "cuqi35",
    "ui_width": 320,
    "ui_height": 480,
    "physical_width": 480,
    "physical_height": 320,
    "rotation": rotation,
    "ui_fps": 20,
    "fullscreen": True,
})
p.write_text(json.dumps(cfg, indent=2) + "\n")
PY
chown -R "$TARGET_USER:$TARGET_USER" "$CFG_DIR" "$TARGET_HOME/.config/kanshi"

# Enable the compact class patch only for this display fork.
SERVICE="$TARGET_HOME/.config/systemd/user/rfeye-user.service"
if [[ -f "$SERVICE" ]]; then
  sed -i '/^Environment=RFEYE_DISPLAY_PROFILE=/d' "$SERVICE"
  sed -i '/^Environment=PYGAME_HIDE_SUPPORT_PROMPT=1$/a Environment=RFEYE_DISPLAY_PROFILE=cuqi35' "$SERVICE"
  chown "$TARGET_USER:$TARGET_USER" "$SERVICE"
fi

# Remove the HDMI-specific mode manager from appliance autostart.
LABWC="$TARGET_HOME/.config/labwc/autostart"
if [[ -f "$LABWC" ]]; then
  sed -i '/\/usr\/bin\/kanshi[[:space:]]*&/d' "$LABWC"
fi

echo "[CUQI 5/8] Rebuilding boot splash for 480x320..."
THEME_DIR=/usr/share/plymouth/themes/rfeye
if [[ -d "$THEME_DIR" ]]; then
  python3 "$TMP_ROOT/src/scripts/generate-plymouth-assets-cuqi35.py" "$THEME_DIR"
  chmod 0644 "$THEME_DIR"/*.png
  plymouth-set-default-theme -R rfeye
fi

echo "[CUQI 6/8] Installing display diagnostics..."
cat > /usr/local/bin/rfeye-cuqi35-status <<'EOF'
#!/usr/bin/env bash
set -u
echo "=== RF Eye CUQI 3.5 display status ==="
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
echo "SPI devices:"
ls -1 /dev/spidev* 2>/dev/null || true
echo ""
echo "Touch devices:"
grep -B1 -A4 -Ei 'ADS7846|XPT2046|Touchscreen' /proc/bus/input/devices 2>/dev/null || true
echo ""
echo "Kernel display messages:"
dmesg | grep -iE 'ili9486|piscreen|spi|drm' | tail -n 60 || true
EOF
chmod +x /usr/local/bin/rfeye-cuqi35-status

echo "[CUQI 7/8] Verifying configuration..."
grep -q '^dtoverlay=piscreen,.*drm' "$CONFIG" || {
  echo "ERROR: piscreen DRM overlay was not written to $CONFIG"
  exit 1
}
grep -q 'RFEYE_DISPLAY_PROFILE=cuqi35' "$SERVICE" || {
  echo "ERROR: compact display profile was not added to RF Eye service"
  exit 1
}
python3 - "$CFG_FILE" <<'PY'
import json, sys
cfg=json.load(open(sys.argv[1]))
assert (cfg.get('ui_width'),cfg.get('ui_height')) == (320,480)
assert (cfg.get('physical_width'),cfg.get('physical_height')) == (480,320)
PY

sync
echo "[CUQI 8/8] Done."
cat <<EOF

RF Eye CUQI 3.5 portrait fork is installed.

Display:  3.5 inch SPI touchscreen
Native:   480x320 physical / 320x480 portrait UI
RF Eye:   native portrait layout, no aspect-ratio scaling
SPI:      ${SPI_HZ} Hz
Branch:   ${REPO_BRANCH}

Reboot now:
  sudo reboot

After reboot, diagnostics are available with:
  sudo rfeye-cuqi35-status

If the image is upside-down, reinstall with counter-clockwise RF Eye rotation:
  curl -fsSL https://raw.githubusercontent.com/${REPO_SLUG}/${REPO_BRANCH}/install-cuqi35.sh | sudo env RFEYE_ROTATION=ccw bash
EOF
