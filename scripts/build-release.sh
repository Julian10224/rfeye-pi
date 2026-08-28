#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
REPO_SLUG="${RFEYE_REPO:-Julian10224/rfeye-pi}"
REPO_BRANCH="${RFEYE_BRANCH:-display-cuqi-35-portrait}"
VERSION="$(tr -d '[:space:]' < "$ROOT/VERSION")"
ZIP="$ROOT/update/rfeye-update.zip"
MANIFEST="$ROOT/update/manifest.json"

rm -f "$ZIP"
cd "$ROOT"
zip -qr "$ZIP" rfeye -x 'rfeye/__pycache__/*' 'rfeye/*.pyc'
SHA="$(sha256sum "$ZIP" | awk '{print $1}')"
URL="https://raw.githubusercontent.com/${REPO_SLUG}/${REPO_BRANCH}/update/rfeye-update.zip"

cat > "$MANIFEST" <<EOF
{
  "version": "${VERSION}",
  "url": "${URL}",
  "sha256": "${SHA}"
}
EOF

echo "Built RF Eye ${VERSION} for ${REPO_BRANCH}"
echo "SHA256: ${SHA}"
