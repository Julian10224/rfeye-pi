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

# Build the archive with Python and fully specified ZIP metadata. This avoids
# zlib/Info-ZIP implementation differences between Raspberry Pi OS and GitHub
# Actions, so identical RF Eye source produces byte-identical OTA packages.
rm -f "$ZIP"
python3 - "$STAGE" "$ZIP" <<'PYZIP'
from pathlib import Path
import stat, sys, zipfile
root=Path(sys.argv[1]); out=Path(sys.argv[2])
files=sorted(p for p in (root/'rfeye').rglob('*') if p.is_file())
with zipfile.ZipFile(out,'w',compression=zipfile.ZIP_STORED,allowZip64=True) as zf:
    for path in files:
        rel=path.relative_to(root).as_posix()
        zi=zipfile.ZipInfo(rel,date_time=(2020,1,1,0,0,0))
        zi.create_system=3
        zi.compress_type=zipfile.ZIP_STORED
        # OTA runtime files are data/source files; pin permissions so the ZIP
        # is independent of the checkout umask/group-write setting.
        zi.external_attr=((stat.S_IFREG | 0o644) << 16)
        zi.flag_bits=0
        zf.writestr(zi,path.read_bytes())
PYZIP
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
