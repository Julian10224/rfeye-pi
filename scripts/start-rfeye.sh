#!/usr/bin/env bash
set -u

# The systemd user service already waits for Wayland. Keep this launcher on the
# critical path as small as possible: desktop cleanup runs independently from
# labwc autostart and display mode is configured by boot config + kanshi.
pkill -9 -x rtl_sdr 2>/dev/null || true

exec /opt/rfeye/.venv/bin/python /opt/rfeye/rfeye/app.py
