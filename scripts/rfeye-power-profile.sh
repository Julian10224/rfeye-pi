#!/usr/bin/env bash
set -euo pipefail

# Lower the appliance's idle draw, which is where a marginal 5 V rail is won
# or lost.
#
# Measured on the reference unit, RF Eye 0.9.14 in economical mode with the
# RTL-SDR scanning: the app used 13.4% of one core while the CPU sat at
# 1400 MHz for 18 of 20 sampled seconds. The `ondemand` governor throws the
# clock to maximum on any burst of work and leaves it there, so the 1.5 s
# pause the detector takes between dwells -- added in 0.9.10 precisely so the
# governor could clock back down -- never bought anything. The core runs at
# 1.3563 V at 1400 MHz and 1.2000 V at 600 MHz, and dynamic power goes as
# roughly f*V^2, so the difference is close to a factor of three on the SoC.
#
# `conservative` is the default here rather than `powersave`: it ramps up
# under load instead of jumping, and drops back quickly, so a verification
# dwell still gets the clock it needs.
#
# The clock ceiling defaults to 900 MHz because it was measured A/B on two
# Rev 1.3 Pi 3 B+ units of the same release. Compute does get slower -- the
# full analysis of one channel went 301 -> 433 ms and a bare 2^18 FFT
# 157 -> 211 ms -- but a band pass is not compute bound: every dwell listens
# for 910 ms of real time whatever the CPU does, most channels fail their
# first test in milliseconds, and the economical mode adds 1.5 s on top. Pass
# duration was 138-154 s uncapped against 141-146 s capped, and application
# CPU 19.3% against 19.6%. The capped unit ran 11.8 C cooler.
#
#   sudo ./scripts/rfeye-power-profile.sh              # apply, 900 MHz
#   sudo RFEYE_MAX_FREQ_KHZ=0 ./scripts/rfeye-power-profile.sh   # no ceiling
#   sudo RFEYE_MAX_FREQ_KHZ=1200000 ./scripts/rfeye-power-profile.sh
#   sudo RFEYE_GOVERNOR=powersave ./scripts/rfeye-power-profile.sh
#   sudo RFEYE_ETH_OFF=1 ./scripts/rfeye-power-profile.sh
#   sudo ./scripts/rfeye-power-profile.sh --off        # undo
#
# None of this makes a Pi 3 B+ run on a supply that cannot hold 5 V. It buys
# margin, and margin is what the car is short of.

UNIT=/etc/systemd/system/rfeye-power.service

if [[ $EUID -ne 0 ]]; then
  echo "Run with sudo/root" >&2
  exit 1
fi

if [[ "${1:-}" == "--off" ]]; then
  systemctl disable --now rfeye-power.service >/dev/null 2>&1 || true
  rm -f "$UNIT"
  systemctl daemon-reload
  for c in /sys/devices/system/cpu/cpu[0-9]*/cpufreq; do
    [[ -w "$c/scaling_governor" ]] && echo ondemand > "$c/scaling_governor" || true
    [[ -r "$c/cpuinfo_max_freq" && -w "$c/scaling_max_freq" ]] &&
      cat "$c/cpuinfo_max_freq" > "$c/scaling_max_freq" || true
  done
  for l in /sys/class/leds/ACT /sys/class/leds/PWR; do
    [[ -w "$l/trigger" ]] && echo default-on > "$l/trigger" || true
  done
  echo "RF Eye power profile removed; governor back to ondemand."
  exit 0
fi

GOVERNOR="${RFEYE_GOVERNOR:-conservative}"
# 900 MHz by default; 0 / none / max means leave the ceiling alone.
MAX_FREQ="${RFEYE_MAX_FREQ_KHZ-900000}"
case "$MAX_FREQ" in 0|none|max|NONE|MAX) MAX_FREQ="" ;; esac
ETH_OFF="${RFEYE_ETH_OFF:-0}"

available=$(cat /sys/devices/system/cpu/cpu0/cpufreq/scaling_available_governors 2>/dev/null || echo "")
if [[ -n "$available" && " $available " != *" $GOVERNOR "* ]]; then
  # conservative is a module on some kernels and simply absent on others.
  # Fall back rather than leave the unit on ondemand, which is the one
  # setting this script exists to get rid of.
  fallback=""
  for cand in schedutil powersave; do
    if [[ " $available " == *" $cand "* ]]; then fallback="$cand"; break; fi
  done
  if [[ -z "$fallback" ]]; then
    echo "Governor '$GOVERNOR' not available and no fallback found." >&2
    echo "This kernel offers: $available" >&2
    exit 1
  fi
  echo "Governor '$GOVERNOR' not available; using '$fallback' instead." >&2
  GOVERNOR="$fallback"
fi

cat > "$UNIT" <<EOF
[Unit]
Description=RF Eye appliance power profile
After=multi-user.target

[Service]
Type=oneshot
RemainAfterExit=yes
Environment=RFEYE_GOVERNOR=${GOVERNOR}
Environment=RFEYE_MAX_FREQ_KHZ=${MAX_FREQ}
Environment=RFEYE_ETH_OFF=${ETH_OFF}
ExecStart=/usr/local/sbin/rfeye-power-apply

[Install]
WantedBy=multi-user.target
EOF

cat > /usr/local/sbin/rfeye-power-apply <<'APPLY'
#!/usr/bin/env bash
# Applied at boot by rfeye-power.service. Every step is best-effort: a
# governor this kernel does not have, or a sysfs node that has moved, must
# not stop the rest of the profile or fail the boot.
set -u
for c in /sys/devices/system/cpu/cpu[0-9]*/cpufreq; do
  [[ -w "$c/scaling_governor" ]] && echo "${RFEYE_GOVERNOR:-conservative}" > "$c/scaling_governor" 2>/dev/null || true
  if [[ -n "${RFEYE_MAX_FREQ_KHZ:-}" && -w "$c/scaling_max_freq" ]]; then
    want="${RFEYE_MAX_FREQ_KHZ}"
    # A ceiling is only meaningful inside what this board can actually do.
    # Another Raspberry Pi model has a different frequency table, and a
    # ceiling below its minimum would be nonsense rather than economical.
    hw_min="$(cat "$c/cpuinfo_min_freq" 2>/dev/null || echo "")"
    hw_max="$(cat "$c/cpuinfo_max_freq" 2>/dev/null || echo "")"
    [[ -n "$hw_min" && "$want" -lt "$hw_min" ]] && want="$hw_min"
    [[ -n "$hw_max" && "$want" -gt "$hw_max" ]] && want="$hw_max"
    # Snap to a step the driver actually offers, where it lists them.
    steps="$(cat "$c/scaling_available_frequencies" 2>/dev/null || echo "")"
    if [[ -n "$steps" ]]; then
      best=""
      for f in $steps; do
        if [[ "$f" -le "$want" ]] && { [[ -z "$best" ]] || [[ "$f" -gt "$best" ]]; }; then
          best="$f"
        fi
      done
      [[ -n "$best" ]] && want="$best"
    fi
    echo "$want" > "$c/scaling_max_freq" 2>/dev/null || true
  fi
done
# The activity and power LEDs are of no use in a sealed enclosure in a car.
for l in /sys/class/leds/ACT /sys/class/leds/PWR; do
  [[ -w "$l/trigger" ]] && echo none > "$l/trigger" 2>/dev/null || true
  [[ -w "$l/brightness" ]] && echo 0 > "$l/brightness" 2>/dev/null || true
done
# The Ethernet PHY draws power even with nothing plugged into it. Only ever
# brought down when there is no carrier, so a wired unit is left alone.
if [[ "${RFEYE_ETH_OFF:-0}" == "1" ]] && [[ -r /sys/class/net/eth0/carrier ]]; then
  if [[ "$(cat /sys/class/net/eth0/carrier 2>/dev/null || echo 0)" == "0" ]]; then
    ip link set eth0 down 2>/dev/null || true
  fi
fi
exit 0
APPLY
chmod 0755 /usr/local/sbin/rfeye-power-apply

systemctl daemon-reload
systemctl enable --now rfeye-power.service >/dev/null

echo "RF Eye power profile applied."
echo "  governor : $(cat /sys/devices/system/cpu/cpu0/cpufreq/scaling_governor)"
echo "  max freq : $(cat /sys/devices/system/cpu/cpu0/cpufreq/scaling_max_freq) kHz${MAX_FREQ:+ (requested ${MAX_FREQ})}"
echo "  now at   : $(cat /sys/devices/system/cpu/cpu0/cpufreq/scaling_cur_freq) kHz"
echo "Undo with: sudo $0 --off"
