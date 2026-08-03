#!/usr/bin/env bash
#
# Launch the kiosk: the cage Wayland compositor running a single session script,
# which in turn owns Chromium (splash / status UI) and the native RTP video
# player. No window manager, no chrome, no user controls — a true black-box
# display.
#
# `cage -s -- <app>` runs exactly one app full-screen and exits when it does
# (systemd then restarts us). `-s` allows VT switching, so Ctrl+Alt+F2 still
# reaches a maintenance login shell.
#
set -euo pipefail

exec cage -s -- /opt/adiona/kiosk/kiosk-session.sh
