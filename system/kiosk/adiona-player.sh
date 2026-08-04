#!/usr/bin/env bash
#
# Adiona-TV native low-latency video receiver.
#
# Plays the headset's RTP/H.264 stream straight to the display via GStreamer.
# This replaced a browser + Media Source Extensions player, for two reasons that
# are both structural rather than tuning:
#
#   * UDP instead of TCP. A live frame that misses its moment is worthless, so the
#     transport has to DROP it. TCP acknowledges, retransmits and delivers in
#     order, so one lost Wi-Fi frame stalls everything behind it and the backlog
#     is delivered late instead of discarded.
#   * A bounded jitter buffer instead of MSE. MSE is an accumulation API — it has
#     no way to discard what it is behind on, so a burst of late data became
#     permanent latency that only a seek could clear, and the seek underran the
#     decoder and froze the picture. `drop-on-latency` throws late packets away
#     and stays at the live edge by construction.
#
# The Pi 5 has no hardware H.264 decoder (the Pi 4 block was dropped; only HEVC
# decode remains), so avdec_h264 software-decodes. At 480–720p15 that is nothing
# for four A76 cores.
#
# Debian packages providing the elements used below (see the image's 00-packages;
# note that h264parse and waylandsink are BOTH in -bad, and that there is no
# `gstreamer1.0-wayland` package on Trixie):
#   udpsrc, rtpjitterbuffer, rtph264depay  gstreamer1.0-plugins-good
#   h264parse, waylandsink                 gstreamer1.0-plugins-bad
#   avdec_h264                             gstreamer1.0-libav
#   videoconvert                           gstreamer1.0-plugins-base
#   gst-launch-1.0                         gstreamer1.0-tools
#
# Usage:
#   adiona-player.sh            play (runs until killed; started by kiosk-session.sh)
#   adiona-player.sh --probe    diagnose: count arriving packets and exit
#
set -euo pipefail

# shellcheck source=/dev/null
[ -f /etc/adiona/box.conf ] && source /etc/adiona/box.conf
RTP_PORT="${RTP_PORT:-5004}"
# Jitter buffer. The floor is the FRAME PERIOD, not network jitter: at 15 fps
# frames are 66 ms apart and arrive as bursts, so a buffer shorter than that
# drops packets it should have waited for and partial frames decode as heavy
# blocking that looks like a codec mismatch. ~2 frame periods. See box.conf.
PLAYER_LATENCY_MS="${PLAYER_LATENCY_MS:-120}"

# RTP has no in-band stream description, so the receiver must be told what the
# payload is. Matches CastingPlugin's RtpSender: dynamic PT 96, H.264, 90 kHz.
# No sprop-parameter-sets needed — the encoder emits SPS/PPS before every IDR
# (once per second), so a receiver that joins mid-stream self-configures.
CAPS="application/x-rtp,media=(string)video,clock-rate=(int)90000,encoding-name=(string)H264,payload=(int)96"

# ── Diagnostic mode ──────────────────────────────────────────────────────────
# Answers "is the headset actually sending to this box?" without GStreamer in the
# way. Stop the kiosk first (systemctl stop adiona-kiosk) — it holds the port.
if [ "${1:-}" = "--probe" ]; then
    echo "Probing UDP :${RTP_PORT} for 5 s …"
    exec python3 - "$RTP_PORT" <<'PY'
import socket, sys, time
port = int(sys.argv[1])
s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
s.bind(("0.0.0.0", port))
s.settimeout(0.5)
packets = octets = 0
senders = set()
end = time.monotonic() + 5.0
while time.monotonic() < end:
    try:
        data, addr = s.recvfrom(2048)
    except socket.timeout:
        continue
    packets += 1
    octets += len(data)
    senders.add(addr[0])
if not packets:
    print("NO PACKETS.  Check: headset Live Stream on? joined THIS box's Wi-Fi? "
          "INPUT chain allowing udp/%d on wlan0?" % port)
    sys.exit(1)
print("%d packets, %.1f kB, %.0f kbit/s, from: %s"
      % (packets, octets / 1024.0, octets * 8 / 5.0 / 1000.0, ", ".join(sorted(senders))))
PY
fi

# ── Playback ─────────────────────────────────────────────────────────────────
# udpsrc buffer-size    bounds the kernel receive buffer. Big enough to absorb a
#                       keyframe burst, small enough that it can't hoard seconds.
# drop-on-latency=true  discard packets that arrive too late instead of growing
#                       the buffer. This is the whole point of the rewrite.
# h264parse config-interval=-1  re-insert SPS/PPS ahead of each IDR so the
#                       decoder recovers on its own after packet loss.
# output-corrupt=false  don't emit visibly broken frames while recovering from
#                       loss; the next IDR is at most 1 s away.
# sync=false            render on arrival rather than scheduling against the
#                       pipeline clock — the jitter buffer already does the
#                       smoothing, and clock sync would re-add the delay we just
#                       removed.
exec gst-launch-1.0 -q \
    udpsrc port="$RTP_PORT" caps="$CAPS" buffer-size=262144 \
    ! rtpjitterbuffer latency="$PLAYER_LATENCY_MS" drop-on-latency=true \
    ! rtph264depay \
    ! h264parse config-interval=-1 \
    ! avdec_h264 output-corrupt=false \
    ! videoconvert \
    ! waylandsink sync=false
