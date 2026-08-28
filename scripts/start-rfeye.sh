#!/usr/bin/env bash
set -u

# Do not add a fixed sleep here. The user service already waits for the Wayland
# socket, so any extra delay only makes the appliance feel slower.
pkill -x wf-panel-pi 2>/dev/null || true
pkill -x squeekboard 2>/dev/null || true
pkill -9 -x rtl_sdr 2>/dev/null || true

if command -v wlr-randr >/dev/null 2>&1; then
  MAIN_OUT="$(wlr-randr | awk '
    /^[A-Za-z0-9-]+ / {o=$1}
    /800x480 px/ && /(preferred|current)/ {print o; exit}
  ')"
  if [[ -n "$MAIN_OUT" ]]; then
    while read -r out; do
      [[ -z "$out" ]] && continue
      if [[ "$out" == "$MAIN_OUT" ]]; then
        wlr-randr --output "$out" --on --mode 800x480 --pos 0,0 2>/dev/null || true
      else
        wlr-randr --output "$out" --off 2>/dev/null || true
      fi
    done < <(wlr-randr | awk '/^[A-Za-z0-9-]+ / {print $1}')
  fi
fi

exec /opt/rfeye/.venv/bin/python /opt/rfeye/rfeye/app.py
