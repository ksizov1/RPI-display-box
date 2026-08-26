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

# The compositor's cursor, made entirely of transparent pixels. This is the
# BACKSTOP, not the whole answer: it covers what the page cannot reach - chiefly
# the video player's own Wayland surface, over which CSS has no say at all, and
# where a cursor parked in one spot for hours is exactly the burn-in the
# screensaver exists to prevent.
#
# The page handles its own: `cursor: none` on html/body for the waiting screen
# and the screensaver, and a data: URI image for the settings panel, so a mouse
# is properly visible there. An image is used rather than a cursor NAME on
# purpose - every name resolves through the theme below and would come back
# invisible, which is what used to make the pointer flicker in and out as it
# crossed the panel's buttons and text fields.
#
# Idempotent, so this is a no-op after the first boot.
/opt/adiona/kiosk/blank-cursor.py || echo "[cage-session] blank cursor unavailable; a cursor may be visible"
export XCURSOR_THEME=adiona-blank
export XCURSOR_SIZE=24

exec cage -s -- /opt/adiona/kiosk/kiosk-session.sh
