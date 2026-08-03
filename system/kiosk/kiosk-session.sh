#!/usr/bin/env bash
#
# Adiona-TV kiosk session — runs *inside* cage as its single application.
#
# Two things share the display:
#
#   * Chromium, showing the controller page: the marketing splash, the join
#     credentials, uplink status and the keyboard Wi-Fi setup overlay. It no
#     longer plays video — that moved to GStreamer (see adiona-player.sh).
#   * adiona-player.sh, the native RTP video player, started only while a headset
#     is actually casting and mapped on top of Chromium.
#
# Chromium stays resident so the transition is instant. Restarting it per session
# would mean a ~2 s black gap every time a headset connects or disconnects.
#
# cage exits when its application exits, so this script must stay in the
# foreground for the whole session; systemd restarts the unit if it returns.
#
# NOTE: no `set -e` — a child dying is an expected, recoverable event here, and
# the supervision loop has to outlive it.
set -uo pipefail

# shellcheck source=/dev/null
[ -f /etc/adiona/box.conf ] && source /etc/adiona/box.conf
PORT="${CONTROLLER_PORT:-8090}"
STATE_URL="http://127.0.0.1:${PORT}/state"
URL="http://127.0.0.1:${PORT}/"
PLAYER="/opt/adiona/kiosk/adiona-player.sh"

# stack — Chromium keeps running behind the video (default; instant switching).
# swap  — Chromium is stopped while the player runs. Fall back to this if a cage
#         build refuses to map a second Wayland client on top.
OVERLAY_MODE="${PLAYER_OVERLAY_MODE:-stack}"

POLL_SECONDS=1
PLAYER_RETRY_SECONDS=2

chromium_pid=""
player_pid=""
player_retry_at=0

log() { echo "[adiona-kiosk] $*"; }

start_chromium() {
    [ -n "$chromium_pid" ] && kill -0 "$chromium_pid" 2>/dev/null && return
    # Flags strip every bit of UI and disable update/crash/translate prompts so
    # nothing can ever cover the display. The mouse cursor is suppressed via the
    # 99-adiona-no-pointer udev rule.
    chromium \
        --kiosk \
        --ozone-platform=wayland \
        --enable-features=UseOzonePlatform \
        --noerrdialogs \
        --disable-infobars \
        --disable-session-crashed-bubble \
        --hide-crash-restore-bubble \
        --disable-translate \
        --disable-features=Translate \
        --no-first-run \
        --fast \
        --fast-start \
        --check-for-update-interval=31536000 \
        --overscroll-history-navigation=0 \
        --disable-pinch \
        "$URL" &
    chromium_pid=$!
    log "chromium started (pid $chromium_pid)"
}

stop_chromium() {
    [ -n "$chromium_pid" ] || return
    kill "$chromium_pid" 2>/dev/null
    wait "$chromium_pid" 2>/dev/null
    chromium_pid=""
    log "chromium stopped"
}

start_player() {
    [ -n "$player_pid" ] && kill -0 "$player_pid" 2>/dev/null && return
    [ "$OVERLAY_MODE" = "swap" ] && stop_chromium
    "$PLAYER" &
    player_pid=$!
    log "player started (pid $player_pid)"
}

stop_player() {
    [ -n "$player_pid" ] || return
    kill "$player_pid" 2>/dev/null
    wait "$player_pid" 2>/dev/null
    player_pid=""
    log "player stopped"
    [ "$OVERLAY_MODE" = "swap" ] && start_chromium
}

running() { [ -n "${1:-}" ] && kill -0 "$1" 2>/dev/null; }

cleanup() {
    trap - TERM INT EXIT
    stop_player
    stop_chromium
    exit 0
}
trap cleanup TERM INT EXIT

# Don't open anything until the controller answers, otherwise the first load
# races the service and shows an error page.
for _ in $(seq 1 30); do
    curl -sf "$STATE_URL" >/dev/null 2>&1 && break
    sleep 1
done

start_chromium

while true; do
    # The controller reports "live" only once a headset has actually started
    # casting, and keeps it sticky while that headset stays associated to the AP
    # — so a paused stream holds the last frame rather than flashing the splash,
    # matching the behaviour the browser player used to have.
    mode="$(curl -sf --max-time 2 "$STATE_URL" 2>/dev/null \
            | sed -n 's/.*"mode"[[:space:]]*:[[:space:]]*"\([^"]*\)".*/\1/p')"

    if [ "$mode" = "live" ]; then
        if ! running "$player_pid"; then
            # A player that exited on its own (decoder error, socket problem)
            # gets retried on a backoff rather than in a tight respawn loop.
            now=$(date +%s)
            if [ -n "$player_pid" ]; then
                player_pid=""
                player_retry_at=$((now + PLAYER_RETRY_SECONDS))
                log "player exited; retrying in ${PLAYER_RETRY_SECONDS}s"
            elif [ "$now" -ge "$player_retry_at" ]; then
                start_player
            fi
        fi
    else
        running "$player_pid" && stop_player
        [ -n "$player_pid" ] && player_pid=""
    fi

    # Chromium is the persistent background; bring it back if it ever dies.
    if [ "$OVERLAY_MODE" = "stack" ] || ! running "$player_pid"; then
        running "$chromium_pid" || { chromium_pid=""; start_chromium; }
    fi

    sleep "$POLL_SECONDS"
done
