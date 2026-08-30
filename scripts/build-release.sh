#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
REPO_SLUG="${RFEYE_REPO:-Julian10224/rfeye-pi}"
REPO_BRANCH="${RFEYE_BRANCH:-main}"
VERSION="$(tr -d '[:space:]' < "$ROOT/VERSION")"
ZIP="$ROOT/update/rfeye-update.zip"
MANIFEST="$ROOT/update/manifest.json"
STAGE="$(mktemp -d /tmp/rfeye-release.XXXXXX)"
cleanup() { rm -rf "$STAGE"; }
trap cleanup EXIT

mkdir -p "$STAGE/rfeye"
cp -a "$ROOT/rfeye/." "$STAGE/rfeye/"

# The repository runtime is the source of truth. Do not mutate it with legacy
# post-install patchers; fresh installs and OTA updates must receive identical code.
find "$STAGE/rfeye" -type d -name '.backup-*' -prune -exec rm -rf {} +
find "$STAGE/rfeye" -type d -name __pycache__ -prune -exec rm -rf {} +
find "$STAGE/rfeye" -name '*.bak*' -delete

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

# Normalize metadata so rebuilding unchanged source produces the same OTA SHA.
find "$STAGE/rfeye" -exec touch -h -t 202001010000 {} +
rm -f "$ZIP"
(
  cd "$STAGE"
  find rfeye -type f -print | LC_ALL=C sort | zip -X -q "$ZIP" -@
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

echo "Built RF Eye ${VERSION} source-of-truth release for ${REPO_BRANCH}"
echo "SHA256: ${SHA}"
