#!/usr/bin/env python3
"""
Fake LAN wheel — speaks adiona-wheel.py's wire protocol without a wheel or a Pi.

Point Adiona-G at this to exercise the whole headset-side receive path (Godot's
LanWheelManager, the CarUserControl branch, the sensitivity sliders) on a
workstation, with no Raspberry Pi and no G920 on the desk. It is also the
quickest way to tell a headset-side bug from a box-side one: if the game drives
correctly against this, the fault is in the wheel service or the device.

Usage
    python3 tools/wheel-sim.py                 # sweep the wheel lock to lock
    python3 tools/wheel-sim.py --range 270     # pretend it is a 270 deg wheel
    python3 tools/wheel-sim.py --static 90     # hold 90 deg right, no sweep
    python3 tools/wheel-sim.py --no-wheel      # box alive, nothing plugged in
    python3 tools/wheel-sim.py --app-version 1.6.0   # what the box claims to run
    python3 tools/wheel-sim.py --keys F1,TAB,CTRL+S  # send these keystrokes on a loop

Then in Godot set Global.lan_wheel.box_ip_override to this machine's IP (or
"127.0.0.1" when the game runs on the same box).

Run it as the RECEIVER's peer, i.e. on whatever machine the game will treat as
the display box. It binds the real port, so the actual wheel service must not be
running at the same time.

stdlib only, same rule as the rest of the repo.
"""

import argparse
import math
import socket
import struct
import sys
import time

# Mirrors the wire format in system/wheel/adiona-wheel.py. PACKET SIZES 48
# (STATE), 56 (INFO) and 64 (BOX) ARE TAKEN — the headset dispatches on size
# first, so any new message must avoid all three. Change this file and the
# service together, or the sim stops being a valid stand-in.
KEY_SLOTS = 4
STATE_FMT = struct.Struct("<4sIHHfHHIiHBB" + "HBB" * KEY_SLOTS)
INFO_FMT = struct.Struct("<4sHH48s")
BOX_FMT = struct.Struct("<4sHHBBBBHHI20s24s")
SUB_FMT = struct.Struct("<4sI")
STATE_MAGIC, INFO_MAGIC, BOX_MAGIC, SUB_MAGIC = b"AW02", b"AI01", b"AB01", b"ASUB"

FLAG_WHEEL_PRESENT = 1 << 0
FLAG_PEDALS_MAPPED = 1 << 1
FLAG_RANGE_APPLIED = 1 << 2
FLAG_KEYBOARD_PRESENT = 1 << 4

# Keystrokes, as adiona_keys.py encodes them: a printable key is its uppercase
# ASCII value, anything else is 0x8000 | the ordinal Godot gives it past
# KEY_SPECIAL. Only the handful --keys accepts are listed; the real table lives
# in adiona_keys.GODOT_KEYS.
KEY_NAMES = {
    "ESC": 0x8001, "TAB": 0x8002, "BACKSPACE": 0x8004, "ENTER": 0x8005,
    "LEFT": 0x800F, "UP": 0x8010, "RIGHT": 0x8011, "DOWN": 0x8012,
    "PAGEUP": 0x8013, "PAGEDOWN": 0x8014, "SPACE": ord(" "),
}
for _i in range(12):
    KEY_NAMES["F%d" % (_i + 1)] = 0x801C + _i
for _c in "ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789":
    KEY_NAMES[_c] = ord(_c)

MOD_NAMES = {"SHIFT": 1 << 0, "CTRL": 1 << 1, "ALT": 1 << 2, "META": 1 << 3}

ACTION_UP, ACTION_DOWN, ACTION_REPEAT = 0, 1, 2


def parse_keys(text):
    """'F1,CTRL+S,SHIFT+LEFT' -> [(code, mods), ...]. Exits on a bad name."""
    combos = []
    for chunk in text.split(","):
        chunk = chunk.strip().upper()
        if not chunk:
            continue
        parts = chunk.split("+")
        name, mods = parts[-1], 0
        for m in parts[:-1]:
            if m not in MOD_NAMES:
                sys.exit("unknown modifier %r (use %s)" % (m, "/".join(MOD_NAMES)))
            mods |= MOD_NAMES[m]
        if name not in KEY_NAMES:
            sys.exit("unknown key %r (try F1, TAB, SPACE, LEFT, a letter or a digit)"
                     % name)
        combos.append((KEY_NAMES[name], mods))
    return combos


def parse_semver(text):
    parts = text.strip().split(".")
    if len(parts) != 3 or not all(p.isdigit() and int(p) < 256 for p in parts):
        return (0, 0, 0)
    return tuple(int(p) for p in parts)


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--port", type=int, default=5010)
    ap.add_argument("--hz", type=int, default=90)
    ap.add_argument("--range", type=int, default=900, dest="range_deg",
                    help="hardware rotation range in degrees (900 or 270)")
    ap.add_argument("--static", type=float, default=None, metavar="DEG",
                    help="hold this steering angle instead of sweeping")
    ap.add_argument("--period", type=float, default=6.0,
                    help="seconds per full lock-to-lock sweep")
    ap.add_argument("--no-wheel", action="store_true",
                    help="report the box as alive but with no wheel plugged in")
    ap.add_argument("--name", default="Logitech G920 Driving Force Racing Wheel")
    ap.add_argument("--app-version", default="1.5.0", dest="app_version",
                    help="version the box reports in the 1 Hz AB01 packet")
    ap.add_argument("--os-string", default="Debian 13 (trixie)",
                    dest="os_string", help="OS string reported in AB01 (24 bytes max)")
    ap.add_argument("--os-version", default="13", dest="os_version",
                    help="numeric OS version reported in AB01, e.g. 13 or 12.5")
    ap.add_argument("--box-id", default="A3F1", dest="box_id",
                    help="4 hex chars, as adiona-firstboot.sh derives from the MAC")
    ap.add_argument("--no-box-report", action="store_true",
                    help="omit AB01 entirely, to test a headset against an old box")
    ap.add_argument("--keys", default="",
                    help="comma-separated keystrokes to send on a loop, e.g. "
                         "'F1,TAB,CTRL+S,SHIFT+LEFT'")
    ap.add_argument("--key-period", type=float, default=2.0, dest="key_period",
                    help="seconds between keystrokes when --keys is given")
    args = ap.parse_args()

    combos = parse_keys(args.keys)

    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
        sock.bind(("0.0.0.0", args.port))
    except OSError as e:
        sys.exit("could not bind UDP %d: %s\n"
                 "(is the real adiona-wheel service running?)" % (args.port, e))
    sock.setblocking(False)

    print("wheel-sim on UDP %d — %d Hz, range %d deg%s" %
          (args.port, args.hz, args.range_deg,
           ", NO WHEEL" if args.no_wheel else ""))
    print("box report: v%s on %s%s" %
          (args.app_version, args.os_string,
           " (SUPPRESSED)" if args.no_box_report else ""))
    print("waiting for a subscriber (the headset sends ASUB every 500 ms)...")

    half = args.range_deg / 2.0
    interval = 1.0 / args.hz
    info_every = max(1, args.hz // 2)

    app_num = parse_semver(args.app_version)
    os_bits = args.os_version.split(".")
    os_major = int(os_bits[0]) if os_bits and os_bits[0].isdigit() else 0
    os_minor = int(os_bits[1]) if len(os_bits) > 1 and os_bits[1].isdigit() else 0
    try:
        box_id = int(args.box_id, 16) & 0xFFFF
    except ValueError:
        box_id = 0
    t_boot = time.monotonic()

    sub = None
    sub_seen = 0.0
    seq = tick = 0
    t0 = time.monotonic()
    next_send = t0

    # Keystroke ring, exactly as adiona_keys.py keeps it: the last four, oldest
    # first, repeated in every packet. Each one is sent as a down followed by an
    # up, which is what a tap looks like on the wire.
    key_ring = []
    key_seq = 0
    next_key = t0 + args.key_period
    key_at = 0

    while True:
        # Drain every pending subscribe; the newest source address wins.
        while True:
            try:
                data, addr = sock.recvfrom(64)
            except BlockingIOError:
                break
            except ConnectionResetError:
                # Windows only: a previous sendto drew an ICMP port-unreachable
                # (the subscriber quit), and Winsock surfaces it here as WSAECONNRESET
                # on the *next* recv. It says nothing about this socket's health —
                # UDP has no connection to reset — so drop the stale subscriber and
                # carry on rather than dying on a peer that simply went away.
                sub = None
                break
            if len(data) >= SUB_FMT.size and data[:4] == SUB_MAGIC:
                if sub != addr:
                    print("subscriber %s:%d" % addr)
                sub = addr
                sub_seen = time.monotonic()

        now = time.monotonic()
        if now < next_send:
            time.sleep(min(interval, next_send - now))
            continue
        next_send += interval
        if next_send < now:
            next_send = now + interval

        if sub is None:
            continue
        if now - sub_seen > 3.0:
            print("subscriber timed out")
            sub = None
            continue

        t = now - t0
        if args.no_wheel:
            steer, throttle, brake, flags = 0.0, 0, 0, 0
        else:
            if args.static is not None:
                steer = args.static
            else:
                steer = half * math.sin(2.0 * math.pi * t / args.period)
            # Gas and brake on separate, deliberately out-of-phase ramps, so a
            # crossed mapping in the game shows up as both pedals moving together.
            throttle = int((0.5 + 0.5 * math.sin(2.0 * math.pi * t / 4.0)) * 65535)
            brake = int((0.5 + 0.5 * math.sin(2.0 * math.pi * t / 7.0)) * 65535)
            flags = FLAG_WHEEL_PRESENT | FLAG_PEDALS_MAPPED | FLAG_RANGE_APPLIED

        if combos:
            flags |= FLAG_KEYBOARD_PRESENT
            if now >= next_key:
                next_key = now + args.key_period
                code, mods = combos[key_at % len(combos)]
                key_at += 1
                for action in (ACTION_DOWN, ACTION_UP):
                    key_seq = (key_seq + 1) & 0xFFFF
                    key_ring.append((code, mods, action))
                key_ring = key_ring[-KEY_SLOTS:]
                print("key: %s%s" %
                      ("+".join(n for n, b in MOD_NAMES.items() if mods & b) + "+"
                       if mods else "",
                       next(n for n, c in KEY_NAMES.items() if c == code)))

        key_fields = [key_seq, len(key_ring), 0]
        for k in key_ring:
            key_fields += list(k)
        key_fields += [0, 0, 0] * (KEY_SLOTS - len(key_ring))

        seq = (seq + 1) & 0xFFFFFFFF
        tick += 1
        t_us = int(time.monotonic_ns() // 1000) & 0x7FFFFFFF
        try:
            sock.sendto(STATE_FMT.pack(STATE_MAGIC, seq, flags, 0, steer,
                                       throttle, brake, 0, t_us, *key_fields), sub)
            if tick % info_every == 0:
                name = b"" if args.no_wheel else args.name.encode("utf-8")[:48]
                sock.sendto(INFO_FMT.pack(INFO_MAGIC, args.range_deg, flags, name), sub)
            # Offset by one tick so BOX never shares a tick with INFO.
            if not args.no_box_report and tick % args.hz == 1:
                sock.sendto(BOX_FMT.pack(
                    BOX_MAGIC, 0, box_id, app_num[0], app_num[1], app_num[2],
                    2, os_major, os_minor, int(now - t_boot),
                    args.app_version.encode("utf-8")[:20],
                    args.os_string.encode("utf-8")[:24]), sub)
        except OSError:
            pass                    # drop-tolerant, exactly like the real service


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print()
