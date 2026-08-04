#!/usr/bin/env python3
"""Generate a fully transparent XCursor theme, so the compositor draws no cursor.

Why this exists
---------------
99-adiona-no-pointer.rules makes libinput ignore pure pointer devices, but it
deliberately does NOT ignore a device that is also a keyboard - otherwise a combo
keyboard+trackpad (a Logitech K400, the usual kiosk remote) would take the
keyboard down with it and there would be no way to reach the Wi-Fi overlay or
Ctrl+Alt+F2. Such a device exposes keyboard and pointer on ONE event node, so
libinput reports a pointer and cage paints a cursor.

That cursor cannot be hidden from the page: CSS `cursor: none` applies to the web
content, not to what the Wayland compositor composites on top. And a cursor
parked in one spot for hours is itself exactly the burn-in this box is trying to
avoid.

So the cursor is still there and still moves the pointer - it is just drawn
entirely from transparent pixels. The trackpad keeps working for the click
handlers in the setup overlay; it simply has no visible pointer, which matches
the design intent already stated in the udev rule.

Writes nothing if the theme is already present and non-empty, so it is safe to
run on every boot.

Usage: blank-cursor.py [theme-dir]      (default /usr/share/icons/adiona-blank)
"""

import os
import struct
import sys

# XCursor on-disk format (see libXcursor). A file is a header, a table of
# contents, then one chunk per image.
MAGIC = b"Xcur"
FILE_HEADER_LEN = 16
FILE_VERSION = 0x00010000
CHUNK_TYPE_IMAGE = 0xFFFD0002
IMAGE_HEADER_LEN = 36
IMAGE_VERSION = 1

# Nominal sizes to provide. Xcursor picks the nearest available to XCURSOR_SIZE,
# so covering the common ones avoids any chance of falling through to the
# inherited theme - which would be a visible cursor.
SIZES = (16, 24, 32, 48, 64)

# Cursor names to provide. A name that is missing falls back to the inherited
# theme and becomes visible again, so this covers what a compositor and a page
# actually ask for rather than just the default.
NAMES = (
    "default", "left_ptr", "arrow", "top_left_arrow", "pointer", "hand1", "hand2",
    "text", "xterm", "ibeam", "crosshair", "watch", "wait", "progress",
    "left_ptr_watch", "not-allowed", "grab", "grabbing", "move", "all-scroll",
    "n-resize", "s-resize", "e-resize", "w-resize",
    "ne-resize", "nw-resize", "se-resize", "sw-resize",
    "ns-resize", "ew-resize", "col-resize", "row-resize",
)


def build_cursor(sizes=SIZES):
    """One XCursor file holding a 1x1 fully transparent image per nominal size."""
    images = []
    for size in sizes:
        # width, height, xhot, yhot, delay, then w*h ARGB pixels. 0x00000000 is
        # premultiplied transparent black: nothing is drawn.
        body = struct.pack("<IIIII", 1, 1, 0, 0, 0) + struct.pack("<I", 0)
        header = struct.pack("<IIII", IMAGE_HEADER_LEN, CHUNK_TYPE_IMAGE,
                             size, IMAGE_VERSION)
        images.append((size, header + body))

    ntoc = len(images)
    offset = FILE_HEADER_LEN + ntoc * 12
    toc = b""
    chunks = b""
    for size, blob in images:
        toc += struct.pack("<III", CHUNK_TYPE_IMAGE, size, offset)
        chunks += blob
        offset += len(blob)

    return (MAGIC + struct.pack("<III", FILE_HEADER_LEN, FILE_VERSION, ntoc)
            + toc + chunks)


def main():
    theme_dir = sys.argv[1] if len(sys.argv) > 1 else "/usr/share/icons/adiona-blank"
    name = os.path.basename(theme_dir)
    cursors = os.path.join(theme_dir, "cursors")

    probe = os.path.join(cursors, "default")
    if os.path.isfile(probe) and os.path.getsize(probe) > 0:
        return 0                                    # already generated

    try:
        os.makedirs(cursors, exist_ok=True)
        with open(os.path.join(theme_dir, "index.theme"), "w") as fh:
            # No Inherits: a miss must not fall through to a visible theme.
            fh.write("[Icon Theme]\nName=%s\nComment=Transparent kiosk cursor\n" % name)
        blob = build_cursor()
        for cursor_name in NAMES:
            with open(os.path.join(cursors, cursor_name), "wb") as fh:
                fh.write(blob)
    except OSError as e:
        print("blank-cursor: %s" % e, file=sys.stderr)
        return 1

    print("blank-cursor: wrote %d cursors to %s" % (len(NAMES), cursors))
    return 0


if __name__ == "__main__":
    sys.exit(main())
