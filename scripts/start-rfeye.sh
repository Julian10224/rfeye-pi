#!/usr/bin/env bash
set -u

# The systemd user service already waits for Wayland. Keep this launcher on the
# critical path as small as possible: desktop cleanup runs independently from
# labwc autostart and display mode is configured by boot config + kanshi.
pkill -9 -x rtl_sdr 2>/dev/null || true

# An update cut off by a power loss used to leave every runtime module at
# 0 bytes (seen 14 September 2026). Python then exits at once and systemd
# restarts it for ever behind a black panel. If the updater's backup is
# intact, put it back instead of spinning; the next update can try again.
RUNTIME=/opt/rfeye/rfeye
STATE="${HOME:-/home/julian}/.local/state/rfeye"
BACKUP="$STATE/rfeye.backup"
if [ ! -s "$RUNTIME/app.py" ] && [ -s "$BACKUP/app.py" ] \
   && [ -z "$(find "$BACKUP" -name '*.py' -size 0 -print -quit 2>/dev/null)" ]; then
  if cp -a "$BACKUP/." "$RUNTIME/" && sync; then
    echo "$(date +%Y-%m-%dT%H:%M:%S) start-rfeye restored the runtime from $BACKUP" \
      >> "$STATE/boot.log" 2>/dev/null || true
  fi
fi

exec /opt/rfeye/.venv/bin/python /opt/rfeye/rfeye/app.py
