#!/usr/bin/env bash
set -euo pipefail

REPO_SLUG="${RFEYE_REPO:-Julian10224/rfeye-pi}"
REPO_BRANCH="${RFEYE_BRANCH:-main}"
APP_ROOT=/opt/rfeye
SRC_ROOT=/opt/rfeye-src

if [[ $EUID -ne 0 ]]; then
  echo "Run with sudo: sudo bash install.sh"
  exit 1
fi

TARGET_USER="${SUDO_USER:-$(logname 2>/dev/null || echo julian)}"
TARGET_HOME="$(getent passwd "$TARGET_USER" | cut -d: -f6)"
TARGET_UID="$(id -u "$TARGET_USER")"

# Fresh Raspberry Pi OS desktop installs may still have PackageKit or an APT
# timer active. Retry only lock-related package-manager failures instead of
# aborting the RF Eye installation during first boot.
apt_retry() {
  local attempts="${RFEYE_APT_LOCK_RETRIES:-120}"
  local delay="${RFEYE_APT_LOCK_RETRY_DELAY:-3}"
  local attempt=1 rc output_file
  output_file="$(mktemp /tmp/rfeye-apt.XXXXXX)"

  while true; do
    : > "$output_file"
    set +e
    "$@" >"$output_file" 2>&1
    rc=$?
    set -e

    if (( rc == 0 )); then
      cat "$output_file"
      rm -f "$output_file"
      return 0
    fi

    if grep -qiE 'Could not get lock|Unable to acquire.*lock|Unable to lock directory|held by process|is another process using it' "$output_file" \
       && (( attempt < attempts )); then
      if (( attempt == 1 || attempt % 10 == 0 )); then
        echo "APT is busy; waiting for the Raspberry Pi package manager to release its lock..."
      fi
      attempt=$((attempt + 1))
      sleep "$delay"
      continue
    fi

    cat "$output_file" >&2
    rm -f "$output_file"
    return "$rc"
  done
}

systemctl stop packagekit.service 2>/dev/null || true
systemctl stop packagekit-offline-update.service 2>/dev/null || true

echo "[1/9] Installing packages..."
apt_retry apt-get -o DPkg::Lock::Timeout=300 update
if command -v raspi-config >/dev/null 2>&1; then
  raspi-config nonint do_wifi_country NL || true
fi
apt_retry env DEBIAN_FRONTEND=noninteractive apt-get -o DPkg::Lock::Timeout=300 install -y \
  git python3 python3-venv python3-pip python3-numpy python3-pygame python3-pil \
  python3-rpi.gpio rtl-sdr librtlsdr0 librtlsdr-dev usbutils curl unzip kanshi network-manager \
  plymouth plymouth-themes initramfs-tools

apt_retry env DEBIAN_FRONTEND=noninteractive apt-get -o DPkg::Lock::Timeout=300 install -y pigpio python3-pigpio || true
systemctl enable --now pigpiod.service 2>/dev/null || true

echo "[2/9] Downloading RF Eye..."
rm -rf "$SRC_ROOT"
if [[ -d "${RFEYE_LOCAL_SOURCE:-}" ]]; then
  cp -a "$RFEYE_LOCAL_SOURCE" "$SRC_ROOT"
else
  git clone --depth 1 --branch "$REPO_BRANCH" "https://github.com/${REPO_SLUG}.git" "$SRC_ROOT"
fi

rm -rf "$APP_ROOT"
mkdir -p "$APP_ROOT/rfeye"
cp -a "$SRC_ROOT/rfeye/." "$APP_ROOT/rfeye/"
python3 -m venv --system-site-packages "$APP_ROOT/.venv"
"$APP_ROOT/.venv/bin/pip" install --upgrade pip wheel
"$APP_ROOT/.venv/bin/pip" install -r "$SRC_ROOT/requirements.txt"
chown -R "$TARGET_USER:$TARGET_USER" "$APP_ROOT/rfeye" "$SRC_ROOT"

echo "[3/9] Configuring RTL-SDR..."
cat >/etc/modprobe.d/rfeye-rtl-sdr.conf <<'EOF'
blacklist dvb_usb_rtl28xxu
blacklist rtl2832
blacklist rtl2830
EOF

echo "[4/9] Configuring RF Eye boot splash..."
BOOT=/boot/firmware
[[ -d "$BOOT" ]] || BOOT=/boot
CONFIG="$BOOT/config.txt"
cp -n "$CONFIG" "${CONFIG}.rfeye-backup" || true

grep -q '^disable_splash=1$' "$CONFIG" || printf '\n# RF Eye appliance boot\ndisable_splash=1\n' >> "$CONFIG"
grep -q '^auto_initramfs=1$' "$CONFIG" || printf 'auto_initramfs=1\n' >> "$CONFIG"

CMDLINE="$BOOT/cmdline.txt"
cp -n "$CMDLINE" "${CMDLINE}.rfeye-backup" || true
python3 - "$CMDLINE" <<'PY'
from pathlib import Path
import sys
p = Path(sys.argv[1])
items = p.read_text().strip().split()
for value in [
    'quiet', 'splash', 'plymouth.ignore-serial-consoles', 'logo.nologo',
    'vt.global_cursor_default=0', 'loglevel=3', 'systemd.show_status=false',
    'rd.systemd.show_status=false'
]:
    if value not in items:
        items.append(value)
p.write_text(' '.join(items) + '\n')
PY

THEME_DIR=/usr/share/plymouth/themes/rfeye
mkdir -p "$THEME_DIR"
install -m 0644 "$SRC_ROOT/config/plymouth/rfeye/rfeye.plymouth" "$THEME_DIR/rfeye.plymouth"
install -m 0644 "$SRC_ROOT/config/plymouth/rfeye/rfeye.script" "$THEME_DIR/rfeye.script"
python3 "$SRC_ROOT/scripts/generate-plymouth-assets.py" "$THEME_DIR"
chmod 0644 "$THEME_DIR"/*.png

if [[ -f /etc/initramfs-tools/initramfs.conf ]]; then
  grep -q '^MODULES=' /etc/initramfs-tools/initramfs.conf \
    && sed -i 's/^MODULES=.*/MODULES=most/' /etc/initramfs-tools/initramfs.conf \
    || printf '\nMODULES=most\n' >> /etc/initramfs-tools/initramfs.conf
fi
if [[ -f /etc/initramfs-tools/update-initramfs.conf ]]; then
  grep -q '^update_initramfs=' /etc/initramfs-tools/update-initramfs.conf \
    && sed -i 's/^update_initramfs=.*/update_initramfs=all/' /etc/initramfs-tools/update-initramfs.conf \
    || printf '\nupdate_initramfs=all\n' >> /etc/initramfs-tools/update-initramfs.conf
fi

plymouth-set-default-theme rfeye
update-initramfs -u -k all

MODEL="$(tr -d '\0' </proc/device-tree/model 2>/dev/null || true)"
ARCH="$(uname -m)"
EXPECTED_INITRAMFS=""
if [[ "$MODEL" == *"Raspberry Pi 3"* ]]; then
  [[ "$ARCH" == "aarch64" ]] && EXPECTED_INITRAMFS="initramfs8" || EXPECTED_INITRAMFS="initramfs7"
elif [[ "$MODEL" == *"Raspberry Pi 4"* ]]; then
  [[ "$ARCH" == "aarch64" ]] && EXPECTED_INITRAMFS="initramfs8" || EXPECTED_INITRAMFS="initramfs7l"
elif [[ "$MODEL" == *"Raspberry Pi 5"* ]]; then
  EXPECTED_INITRAMFS="initramfs_2712"
fi

if [[ -n "$EXPECTED_INITRAMFS" ]]; then
  IMAGE="$BOOT/$EXPECTED_INITRAMFS"
  if [[ ! -f "$IMAGE" ]] || ! lsinitramfs "$IMAGE" 2>/dev/null | grep -q 'usr/share/plymouth/themes/rfeye/rfeye.script'; then
    echo "Rebuilding $EXPECTED_INITRAMFS explicitly for $MODEL ($ARCH)..."
    TMP_IMAGE="${IMAGE}.rfeye-new"
    mkinitramfs -o "$TMP_IMAGE" "$(uname -r)"
    mv -f "$TMP_IMAGE" "$IMAGE"
  fi
  lsinitramfs "$IMAGE" 2>/dev/null | grep -q 'usr/share/plymouth/themes/rfeye/rfeye.script' \
    && echo "Verified RF Eye Plymouth theme in $IMAGE" \
    || echo "WARNING: RF Eye theme could not be verified in $IMAGE" >&2
fi
sync

bash "$SRC_ROOT/scripts/apply-desktop-splash.sh" "$TARGET_USER" "$TARGET_HOME" "$THEME_DIR"

echo "[5/9] RF Eye boot splash installed."

echo "[6/9] Installing appliance session..."
mkdir -p "$TARGET_HOME/.config/labwc" "$TARGET_HOME/.config/autostart" "$TARGET_HOME/.config/kanshi" "$TARGET_HOME/.config/systemd/user/default.target.wants"

SYSTEM_LABWC_AUTOSTART=/etc/xdg/labwc/autostart
if [[ -f "$SYSTEM_LABWC_AUTOSTART" ]]; then
  cp -n "$SYSTEM_LABWC_AUTOSTART" "${SYSTEM_LABWC_AUTOSTART}.rfeye-backup" || true
  sed -i -E '/(^|[[:space:]])(wf-panel-pi|pcmanfm-pi|pcmanfm|lxpanel)([[:space:]]|$)/d' "$SYSTEM_LABWC_AUTOSTART"
fi

install -m 0644 "$SRC_ROOT/config/kanshi-config" "$TARGET_HOME/.config/kanshi/config"
install -m 0755 "$SRC_ROOT/config/labwc-autostart" "$TARGET_HOME/.config/labwc/autostart"
install -m 0644 "$SRC_ROOT/config/labwc/rc.xml" "$TARGET_HOME/.config/labwc/rc.xml"
cp "$SRC_ROOT/scripts/start-rfeye.sh" "$APP_ROOT/start-rfeye.sh"
chmod +x "$APP_ROOT/start-rfeye.sh"
sed -e "s|@UID@|${TARGET_UID}|g" -e "s|@HOME@|${TARGET_HOME}|g" \
  "$SRC_ROOT/config/systemd/rfeye-user.service.in" > "$TARGET_HOME/.config/systemd/user/rfeye-user.service"
mkdir -p "$TARGET_HOME/.config/systemd/user/rfeye-user.service.d"
install -m 0644 "$SRC_ROOT/config/systemd/rfeye-user-fast-ui.conf" \
  "$TARGET_HOME/.config/systemd/user/rfeye-user.service.d/20-rfeye-fast-ui.conf"
ln -sfn ../rfeye-user.service "$TARGET_HOME/.config/systemd/user/default.target.wants/rfeye-user.service"
cat > "$TARGET_HOME/.config/autostart/squeekboard.desktop" <<'EOF'
[Desktop Entry]
Type=Application
Name=Squeekboard
Hidden=true
EOF
chown -R "$TARGET_USER:$TARGET_USER" "$TARGET_HOME/.config/labwc" "$TARGET_HOME/.config/autostart" "$TARGET_HOME/.config/kanshi" "$TARGET_HOME/.config/systemd"

echo "[7/9] Configuring permissions and autologin..."
bash "$SRC_ROOT/scripts/install-networkmanager-policy.sh" "$TARGET_USER"
usermod -aG video,render,input,gpio,plugdev "$TARGET_USER" || true
if command -v raspi-config >/dev/null 2>&1; then
  raspi-config nonint do_boot_behaviour B4 || true
fi
systemctl disable --now rfeye.service 2>/dev/null || true
loginctl enable-linger "$TARGET_USER" 2>/dev/null || true

bash "$SRC_ROOT/scripts/optimize-rpi-appliance.sh" "$TARGET_USER"

echo "[8/9] Configuring Wi-Fi updater..."
MANIFEST_URL="https://raw.githubusercontent.com/${REPO_SLUG}/${REPO_BRANCH}/update/manifest.json"
python3 - "$APP_ROOT/rfeye/config.py" "$MANIFEST_URL" <<'PY'
from pathlib import Path
import re, sys
p = Path(sys.argv[1])
url = sys.argv[2]
s = p.read_text()
s, count = re.subn(
    r'("update_manifest_url"\s*:\s*)"[^"]*"',
    lambda m: m.group(1) + '"' + url + '"',
    s,
    count=1,
)
if count != 1:
    raise SystemExit("Could not set update_manifest_url in config.py")
p.write_text(s)
PY

VERSION="$(cat "$SRC_ROOT/VERSION")"
echo "Installed RF Eye ${VERSION}"
echo "[9/9] Finished."
cat <<EOF

RF Eye is installed.
Reboot with:
  sudo reboot

After reboot RF Eye starts as an appliance: portrait RF Eye boot splash, no Raspberry Pi desktop chrome, then RF Eye fullscreen.
Updater manifest:
  ${MANIFEST_URL}
EOF
