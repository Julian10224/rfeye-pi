#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
REPO_SLUG="${RFEYE_REPO:-Julian10224/rfeye-pi}"
REPO_BRANCH="${RFEYE_BRANCH:-display-cuqi-35-portrait}"
VERSION="$(tr -d '[:space:]' < "$ROOT/VERSION")"
ZIP="$ROOT/update/rfeye-update.zip"
MANIFEST="$ROOT/update/manifest.json"
STAGE="$(mktemp -d /tmp/rfeye-release.XXXXXX)"
cleanup() { rm -rf "$STAGE"; }
trap cleanup EXIT

mkdir -p "$STAGE/rfeye"
cp -a "$ROOT/rfeye/." "$STAGE/rfeye/"

# Build the OTA payload from the same transformed runtime code produced by a
# fresh installation. This keeps existing CUQI units aligned with install.sh.
python3 "$ROOT/scripts/patch-startup-splash.py" "$STAGE/rfeye/app.py"
python3 "$ROOT/scripts/patch-fast-app-start.py" "$STAGE/rfeye/app.py"
python3 "$ROOT/scripts/patch-radar-buzzer.py" "$STAGE/rfeye/app.py"
python3 "$ROOT/scripts/patch-fast-scan.py" "$STAGE/rfeye/sdr_backend.py"
python3 "$ROOT/scripts/patch-persistent-sdr.py" "$STAGE/rfeye/sdr_backend.py"
python3 -m py_compile \
  "$STAGE/rfeye/app.py" \
  "$STAGE/rfeye/sdr_backend.py" \
  "$STAGE/rfeye/compact_display_patch.py" \
  "$STAGE/rfeye/compact_ui_controls.py" \
  "$STAGE/rfeye/compact_ui_draw.py" \
  "$STAGE/rfeye/compact_touch.py" \
  "$STAGE/rfeye/compact_wifi_ui.py"
find "$STAGE/rfeye" -type d -name __pycache__ -prune -exec rm -rf {} +
find "$STAGE/rfeye" -name '*.pyc' -delete

rm -f "$ZIP"
(
  cd "$STAGE"
  zip -qr "$ZIP" rfeye
)
SHA="$(sha256sum "$ZIP" | awk '{print $1}')"
URL="https://raw.githubusercontent.com/${REPO_SLUG}/${REPO_BRANCH}/update/rfeye-update.zip"

cat > "$MANIFEST" <<EOF
{
  "version": "${VERSION}",
  "url": "${URL}",
  "sha256": "${SHA}"
}
EOF

echo "Built patched RF Eye ${VERSION} for ${REPO_BRANCH}"
echo "SHA256: ${SHA}"
