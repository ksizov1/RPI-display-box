#!/usr/bin/env bash
#
# Adiona-TV first-boot provisioning. Runs once per SD card (guarded by the
# .firstboot-done stamp) to give each box a unique identity derived from its
# Wi-Fi MAC, then builds the NetworkManager Wi-Fi access point.
#
# The image itself is generic — a single flashable .img can be written to any
# number of cards, and every box self-names as  <SSID_PREFIX>-XXXX  where XXXX is
# a stable 4-hex-digit hash of wlan0's MAC.
#
set -euo pipefail

CONF="/etc/adiona/box.conf"
STAMP="/etc/adiona/.firstboot-done"
SSID_OUT="/etc/adiona/ssid"
AP_CON="Adiona-AP"

log() { echo "[adiona-firstboot] $*"; }

# shellcheck source=/dev/null
[ -f "$CONF" ] && source "$CONF"
SSID_PREFIX="${SSID_PREFIX:-Adiona-TV}"
WIFI_PASSPHRASE="${WIFI_PASSPHRASE:-adiona-drive}"
WIFI_CHANNEL="${WIFI_CHANNEL:-6}"
WIFI_COUNTRY="${WIFI_COUNTRY:-US}"
AP_CIDR="${AP_CIDR:-192.168.50.1/24}"

# ── Wait for the Wi-Fi interface to exist (driver can lag at first boot) ──────
for _ in $(seq 1 30); do
    [ -f /sys/class/net/wlan0/address ] && break
    sleep 1
done
if [ ! -f /sys/class/net/wlan0/address ]; then
    log "wlan0 not present; aborting (will retry next boot)"
    exit 1
fi

# ── Derive identity from the MAC ─────────────────────────────────────────────
MAC="$(cat /sys/class/net/wlan0/address)"
HASH4="$(printf '%s' "$MAC" | sha256sum | cut -c1-4 | tr 'a-f' 'A-F')"
SSID="${SSID_PREFIX}-${HASH4}"
HOSTNAME="$(echo "${SSID_PREFIX}-${HASH4}" | tr 'A-Z' 'a-z')"
log "MAC=$MAC -> SSID=$SSID hostname=$HOSTNAME"

mkdir -p /etc/adiona
printf '%s\n' "$SSID" > "$SSID_OUT"
hostnamectl set-hostname "$HOSTNAME" || true

# ── Unblock the Wi-Fi radio ──────────────────────────────────────────────────
# Raspberry Pi keeps the Wi-Fi radio rfkill-blocked until a regulatory country
# is set, so without this the AP can't broadcast. raspi-config persists the
# country and clears the block; the extras are runtime/no-raspi-config fallbacks.
log "setting WiFi country $WIFI_COUNTRY and enabling radio"
if command -v raspi-config >/dev/null; then
    raspi-config nonint do_wifi_country "$WIFI_COUNTRY" || true
fi
iw reg set "$WIFI_COUNTRY" 2>/dev/null || true
command -v rfkill >/dev/null && rfkill unblock wifi || true
nmcli radio wifi on || true

# ── Build / refresh the access-point connection ──────────────────────────────
# ipv4.method=shared gives DHCP + NAT masquerade to whatever holds the default
# route (eth0 when an uplink is plugged in); the AP keeps serving the isolated
# LAN unchanged when no uplink is present.
if nmcli -t -f NAME con show | grep -qx "$AP_CON"; then
    log "updating existing $AP_CON"
else
    log "creating $AP_CON"
    nmcli con add type wifi ifname wlan0 con-name "$AP_CON" autoconnect yes ssid "$SSID"
fi

nmcli con modify "$AP_CON" \
    connection.autoconnect yes \
    connection.autoconnect-priority 100 \
    802-11-wireless.ssid "$SSID" \
    802-11-wireless.mode ap \
    802-11-wireless.band bg \
    802-11-wireless.channel "$WIFI_CHANNEL" \
    802-11-wireless-security.key-mgmt wpa-psk \
    802-11-wireless-security.proto rsn \
    802-11-wireless-security.group ccmp \
    802-11-wireless-security.pairwise ccmp \
    802-11-wireless-security.psk "$WIFI_PASSPHRASE" \
    ipv4.method shared \
    ipv4.addresses "$AP_CIDR" \
    ipv6.method disabled

nmcli con up "$AP_CON" || log "AP will come up on next NM cycle"

touch "$STAMP"
log "done"
