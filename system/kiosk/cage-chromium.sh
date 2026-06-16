#!/usr/bin/env bash
#
# Launch the kiosk: a single full-screen Chromium under the cage Wayland
# compositor, pointed at the local controller page. No window manager, no
# chrome, no user controls — a true black-box display.
#
set -euo pipefail

# shellcheck source=/dev/null
[ -f /etc/adiona/box.conf ] && source /etc/adiona/box.conf
PORT="${CONTROLLER_PORT:-8090}"
URL="http://127.0.0.1:${PORT}/"

# Don't open Chromium until the controller is actually answering, otherwise the
# first load races the service and shows an error page.
for _ in $(seq 1 30); do
    if curl -sf "http://127.0.0.1:${PORT}/state" >/dev/null 2>&1; then break; fi
    sleep 1
done

# `cage -s -- <app>` runs exactly one app full-screen and exits when it does
# (systemd then restarts us). `-s` allows VT switching, so Ctrl+Alt+F2 reaches a
# maintenance login shell. Chromium flags strip every bit of UI and disable
# update/crash/translate prompts so nothing can ever cover the stream.
# (The mouse cursor is suppressed via the 99-adiona-no-pointer udev rule.)
exec cage -s -- chromium \
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
    --autoplay-policy=no-user-gesture-required \
    "$URL"
