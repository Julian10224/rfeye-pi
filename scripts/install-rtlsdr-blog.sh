#!/usr/bin/env bash
set -euo pipefail

# Install a librtlsdr that knows the dongle in front of it.
#
# Debian/Raspberry Pi OS ships librtlsdr 2.0.2. It has a code path for the
# RTL-SDR Blog V4 and none at all for the V4L: no "Blog V4L" string anywhere
# in the binary. That dongle carries an RF switch on a GPIO that routes the
# antenna either into the HF upconverter or straight to the tuner, and the
# tuner input has to be selected per band. A driver that does not know the
# model never throws either switch, so the antenna stays on the wrong branch
# and the entire UHF band reads as noise -- indistinguishable from "there is
# no C2000 here", which is exactly how it presented.
#
# Measured on the reference unit, same dongle and same air, four rounds
# alternating between the two builds, 390.7375 MHz:
#
#   Debian 2.0.2   snr 0.5-0.8 dB   bw 39.7 kHz   duty 0.00   FAIL:bandwidth
#   RTL-SDR Blog   snr 11.7-13.4    bw 22.2 kHz   duty 1.00   TETRA
#
# Installed to /usr/local so the packaged library is left alone and an apt
# upgrade cannot quietly take it back. RF Eye picks the library on what it
# supports rather than on what the loader cache happens to return first, so
# both can coexist.

REPO="${RFEYE_RTLSDR_REPO:-https://github.com/rtlsdrblog/rtl-sdr-blog.git}"
# Pinned: a driver is not something to re-roll silently under a user.
REF="${RFEYE_RTLSDR_REF:-aed0ea19f3a273370a13c9009b96313c75d54c7b}"  # 2026-03-22
PREFIX="${RFEYE_RTLSDR_PREFIX:-/usr/local}"

if [[ $EUID -ne 0 ]]; then
  echo "Run with sudo/root" >&2
  exit 1
fi

echo "[rtl-sdr] Installing build dependencies..."
DEBIAN_FRONTEND=noninteractive apt-get -o DPkg::Lock::Timeout=300 install -y \
  cmake build-essential pkg-config libusb-1.0-0-dev git >/dev/null

SRC="$(mktemp -d /tmp/rtlsdr-blog.XXXXXX)"
cleanup() { rm -rf "$SRC"; }
trap cleanup EXIT

echo "[rtl-sdr] Fetching RTL-SDR Blog driver..."
git clone --quiet "$REPO" "$SRC/src"
if ! git -C "$SRC/src" checkout --quiet "$REF" 2>/dev/null; then
  echo "[rtl-sdr] WARNING: pinned ref not found, using default branch" >&2
fi

# The whole reason for doing this. If the source does not carry the model we
# came for, installing it buys nothing and the failure should be loud.
if ! grep -rq 'Blog V4L' "$SRC/src/src/"; then
  echo "ERROR: this rtl-sdr source has no V4L support; refusing to install it" >&2
  exit 1
fi

echo "[rtl-sdr] Building..."
mkdir -p "$SRC/build"
cmake -S "$SRC/src" -B "$SRC/build" \
  -DCMAKE_BUILD_TYPE=Release \
  -DCMAKE_INSTALL_PREFIX="$PREFIX" \
  -DDETACH_KERNEL_DRIVER=ON \
  -DINSTALL_UDEV_RULES=ON >/dev/null
make -C "$SRC/build" -j"$(nproc)" >/dev/null
make -C "$SRC/build" install >/dev/null
ldconfig

LIB="$(ls "$PREFIX"/lib/librtlsdr.so.* 2>/dev/null | head -1 || true)"
if [[ -z "$LIB" ]]; then
  echo "ERROR: librtlsdr was not installed under $PREFIX" >&2
  exit 1
fi
if ! grep -aq 'Blog V4L' "$LIB"; then
  echo "ERROR: $LIB was built without V4L support" >&2
  exit 1
fi

echo "[rtl-sdr] Installed $LIB"
echo "[rtl-sdr] Models known to this build:"
strings "$LIB" | grep -a '^Blog V' | sort -u | sed 's/^/           /'
