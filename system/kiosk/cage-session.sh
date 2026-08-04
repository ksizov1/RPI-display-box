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

# No visible mouse cursor, ever. 99-adiona-no-pointer.rules cannot cover a combo
# keyboard+trackpad (a Logitech K400): keyboard and pointer share one event node,
# so ignoring the device would take the keyboard with it. cage therefore sees a
# pointer and paints a cursor, which the page cannot hide - CSS `cursor: none`
# governs web content, not what the compositor composites over it. A cursor left
# parked in one spot is also precisely the burn-in the screensaver exists to
# prevent. So give the compositor a cursor made entirely of transparent pixels.
# The pointer still works for the click handlers in the setup overlay; it is just
# not drawn. Idempotent, so this is a no-op after the first boot.
/opt/adiona/kiosk/blank-cursor.py || echo "[cage-session] blank cursor unavailable; a cursor may be visible"
export XCURSOR_THEME=adiona-blank
export XCURSOR_SIZE=24

exec cage -s -- /opt/adiona/kiosk/kiosk-session.sh
