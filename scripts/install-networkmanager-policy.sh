#!/usr/bin/env bash
set -euo pipefail

if [[ $EUID -ne 0 ]]; then
  echo "Run as root." >&2
  exit 1
fi

TARGET_USER="${1:-${SUDO_USER:-julian}}"
RULE=/etc/polkit-1/rules.d/49-rfeye-networkmanager.rules
cat > "$RULE" <<EOF
polkit.addRule(function(action, subject) {
    if (subject.user != "${TARGET_USER}") return;
    var allowed = [
        "org.freedesktop.NetworkManager.network-control",
        "org.freedesktop.NetworkManager.wifi.scan",
        "org.freedesktop.NetworkManager.settings.modify.own",
        "org.freedesktop.NetworkManager.settings.modify.system"
    ];
    if (allowed.indexOf(action.id) >= 0) return polkit.Result.YES;
});
EOF
chmod 0644 "$RULE"

# v0.7.14 created private RF Eye profiles. Convert only RF Eye-owned profiles
# to persistent system profiles so NetworkManager can autoconnect before login.
while IFS=: read -r name type; do
  [[ "$type" == "802-11-wireless" ]] || continue
  [[ "$name" == RF\ Eye\ WiFi\ * ]] || continue
  nmcli connection modify id "$name" \
    connection.permissions "" \
    connection.autoconnect yes \
    connection.autoconnect-priority 100 || true
  key_mgmt="$(nmcli -g 802-11-wireless-security.key-mgmt connection show id "$name" 2>/dev/null || true)"
  if [[ -n "$key_mgmt" ]]; then
    nmcli connection modify id "$name" 802-11-wireless-security.psk-flags 0 || true
  fi
done < <(nmcli -t -f NAME,TYPE connection show)

nmcli connection reload || true
echo "RF Eye NetworkManager policy installed for ${TARGET_USER}."
