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

# How long the last frame stays frozen on screen after the stream stops before we
# give up and go back to the waiting splash. Covers the headset being taken off,
# the app being backgrounded, or the process exiting: in all of those the app can
# keep serving :8080 (so `casting` stays true) while sending no frames at all,
# which is why this is measured from actual packet arrival rather than app state.
STREAM_TIMEOUT_SEC="${STREAM_TIMEOUT_SEC:-30}"

# Inbound UDP datagrams per second above which the stream counts as flowing. The
# real stream runs ~150-300 pkt/s; idle background chatter (DHCP/DNS for one
# headset) is a handful. Anywhere in between separates them safely.
RTP_FLOW_MIN_PPS="${RTP_FLOW_MIN_PPS:-20}"

chromium_pid=""
player_pid=""
player_retry_at=0
# Consecutive polls with casting=false, for debouncing the new-session edge.
# Starts at 0 so the first true reading is never itself an edge; only a sustained
# run of false readings can arm one.
not_casting_polls=0
CASTING_EDGE_POLLS="${CASTING_EDGE_POLLS:-3}"
# Last time RTP was observed arriving, and the running UDP counter it came from.
# last_rtp_at=0 means "never seen", which is what keeps the player from starting
# over the splash before any stream actually exists.
last_rtp_at=0
last_udp_count=""

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

# Total inbound UDP datagrams since boot: delivered (InDatagrams) PLUS those that
# found no listener (NoPorts). Both terms are needed because the stream keeps
# arriving while the player is stopped, and then lands in NoPorts instead.
#
# Why the kernel counter rather than something stream-specific: the player holds
# :5004 exclusively, so nothing else can bind it to observe traffic, and asking
# GStreamer would mean parsing gst-launch's message stream. This box carries
# almost no other UDP (a little DHCP/DNS for the one headset) while the stream is
# ~150-300 packets/s, so the two are never close enough to confuse.
udp_datagrams() {
    awk '/^Udp:/ { if (++n == 2) { print $2 + $3; exit } }' /proc/net/snmp 2>/dev/null || echo 0
}

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
    state_json="$(curl -sf --max-time 2 "$STATE_URL" 2>/dev/null)"
    casting="$(printf '%s' "$state_json" \
            | sed -n 's/.*"casting"[[:space:]]*:[[:space:]]*\(true\|false\).*/\1/p')"

    # A sustained false->true edge means a NEW cast session on the headset: the app
    # was restarted, Live Stream was toggled, or the resolution changed. Each of
    # those rebuilds the sender, and a receiver that is already running stays locked
    # to the previous RTP session and would show nothing.
    #
    # DEBOUNCED, because `casting` comes from a 0.6 s HTTP probe of the headset over
    # Wi-Fi and occasionally times out on a perfectly healthy link. Acting on a
    # single false reading tears the player down and rebuilds it for nothing, which
    # on screen is a black flash and a fresh wait for the next keyframe. Requires
    # CASTING_EDGE_POLLS consecutive false readings before a true counts as new.
    case "$casting" in
        false) not_casting_polls=$(( not_casting_polls + 1 )) ;;
        true)
            if [ "$not_casting_polls" -ge "$CASTING_EDGE_POLLS" ] && running "$player_pid"; then
                log "new cast session detected - restarting player"
                stop_player
            fi
            not_casting_polls=0
            ;;
        *) ;;   # controller unreachable this poll - carry the counter unchanged
    esac

    # ── Is RTP actually flowing? ─────────────────────────────────────────────
    now=$(date +%s)
    udp_now="$(udp_datagrams)"
    if [ -n "$last_udp_count" ]; then
        udp_delta=$(( udp_now - last_udp_count ))
        # Counters are monotonic; a negative delta means a wrap or a reboot.
        [ "$udp_delta" -lt 0 ] && udp_delta=0
        if [ "$udp_delta" -ge "$RTP_FLOW_MIN_PPS" ]; then
            last_rtp_at=$now
        fi
    fi
    last_udp_count="$udp_now"

    # Frames flowing, or stopped recently enough that we still hold the last one.
    # The grace window is what makes a brief pause (headset set down for a moment)
    # freeze rather than flicker back to the splash.
    if [ "$last_rtp_at" -ne 0 ] && [ $(( now - last_rtp_at )) -lt "$STREAM_TIMEOUT_SEC" ]; then
        have_stream=1
    else
        have_stream=0
    fi

    # Gated ONLY on frames actually arriving - deliberately not on the controller's
    # `mode`. `mode` depends on the headset appearing in `iw station dump` and
    # answering an HTTP probe, and a single missed poll used to tear the player
    # down and rebuild it a second later. Arriving RTP is both necessary and
    # sufficient: if packets are on :5004 there is a stream to show, whatever the
    # controller currently believes about association.
    if [ "$have_stream" = "1" ]; then
        if ! running "$player_pid"; then
            # A player that exited on its own (decoder error, socket problem)
            # gets retried on a backoff rather than in a tight respawn loop.
            if [ -n "$player_pid" ]; then
                player_pid=""
                player_retry_at=$((now + PLAYER_RETRY_SECONDS))
                log "player exited; retrying in ${PLAYER_RETRY_SECONDS}s"
            elif [ "$now" -ge "$player_retry_at" ]; then
                start_player
            fi
        fi
    else
        if running "$player_pid"; then
            if [ "$have_stream" = "0" ]; then
                log "no RTP for ${STREAM_TIMEOUT_SEC}s - returning to the waiting screen"
            fi
            stop_player
        fi
        [ -n "$player_pid" ] && player_pid=""
    fi

    # Chromium is the persistent background; bring it back if it ever dies.
    if [ "$OVERLAY_MODE" = "stack" ] || ! running "$player_pid"; then
        running "$chromium_pid" || { chromium_pid=""; start_chromium; }
    fi

    sleep "$POLL_SECONDS"
done
