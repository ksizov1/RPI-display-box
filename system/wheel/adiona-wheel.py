#!/usr/bin/env python3
"""
Adiona-TV LAN wheel service.

Reads a USB HID racing wheel plugged into the display box and streams its
readings to the headset over the box's own Wi-Fi AP. This exists because good
wheels (Logitech G920, Fanatec, Thrustmaster) are USB-only and the Quest cannot
take USB peripherals in the field — so the box, which is already the AP and
already at every event, becomes the wheel's radio.

Design notes, in the order they matter:

  1. SEMANTIC, NOT RAW. This service owns all device knowledge: it identifies
     the wheel, maps its axes to steer/gas/brake, and sends a fixed 48-byte
     packet of *game* quantities (steering degrees, throttle, brake). The
     headset carries no per-device table at all. That is deliberate — adding a
     new wheel model is a 30-second `deploy.ps1` to one box, not an APK rebuild
     and a sideload to every headset.

  2. THE HEADSET SUBSCRIBES, THE BOX STREAMS. The headset sends an ASUB
     keepalive (8 bytes from an older APK, 16 with its licence mask) to
     <gateway>:5010 every 500 ms; we stream to whatever source address that
     arrived from. No discovery, no handshake, no session state that a dropped
     packet could corrupt.

  3. FIXED-RATE ABSOLUTE SNAPSHOTS. We send the latest complete state every
     11 ms whether or not the wheel moved. A lost packet therefore costs 11 ms
     of staleness rather than sticking a stale value until the next physical
     movement — which is what an event-driven design would do on a link with no
     retransmission. 48 B x 90 Hz is ~4.3 KB/s, nothing next to the video.

  4. NO IPC SOCKETS. Status goes to /run/adiona/wheel.json, commands arrive via
     /run/adiona/wheel-cmd.json. adiona_controller.py reads/writes those files
     for the on-TV setup UI. Neither service can take the other down, and either
     can restart independently.

  5. IT CARRIES THE KEYBOARD TOO. The same packet carries the last four
     keystrokes from a USB keyboard plugged into the box, so the operator's one
     keyboard drives the headset as well as this box's own settings screens. See
     adiona_keys.py — that module is imported defensively, and a fault in it
     costs the keyboard and nothing else.

evdev is read directly with struct + ioctl: stdlib only, nothing to
pip-install on the image (same rule as adiona_controller.py).

Run `adiona-wheel.py --dump` on the box with a wheel attached to see every
input device, its name, and every axis with live values. Do that before
trusting any profile in WHEEL_PROFILES.
"""

import errno
import fcntl
import json
import os
import select
import socket
import struct
import sys
import threading
import time

# ── Paths / config ───────────────────────────────────────────────────────────
HERE = os.path.dirname(os.path.abspath(__file__))
CONF_PATH = os.environ.get("ADIONA_CONF", "/etc/adiona/box.conf")
MAP_PATH = os.environ.get("ADIONA_WHEEL_MAP", "/etc/adiona/wheel-map.json")
RUN_DIR = os.environ.get("ADIONA_RUN_DIR", "/run/adiona")
VERSION_FILE = os.environ.get("ADIONA_VERSION_FILE", "/opt/adiona/VERSION")
SSID_FILE = os.environ.get("ADIONA_SSID_FILE", "/etc/adiona/ssid")
OS_RELEASE = os.environ.get("ADIONA_OS_RELEASE", "/etc/os-release")

# Dev fallbacks so this runs from a checkout on a workstation.
if not os.path.exists(CONF_PATH):
    CONF_PATH = os.path.join(HERE, "..", "..", "config", "box.conf")
if not os.path.exists(VERSION_FILE):
    VERSION_FILE = os.path.join(HERE, "..", "..", "VERSION")


def load_conf(path):
    """Parse the box.conf KEY="VALUE" shell file into a dict (no shell needed)."""
    conf = {}
    try:
        with open(path, "r") as fh:
            for line in fh:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, _, val = line.partition("=")
                conf[key.strip()] = val.strip().strip('"').strip("'")
    except OSError:
        pass
    return conf


CONF = load_conf(CONF_PATH)
WHEEL_PORT = int(CONF.get("WHEEL_PORT", "5010"))
TX_HZ = int(CONF.get("WHEEL_TX_HZ", "90"))
DEFAULT_RANGE_DEG = int(CONF.get("WHEEL_DEFAULT_RANGE_DEG", "900"))
DEFAULT_AUTOCENTER_PCT = int(CONF.get("WHEEL_AUTOCENTER_PCT", "50"))
KEYS_ENABLED = CONF.get("KEYS_ENABLED", "1") not in ("0", "false", "no")
KEYS_REPEAT_HZ = int(CONF.get("KEYS_REPEAT_HZ", "10"))

# Range assumed for a wheel whose rotation range CANNOT be set — no sysfs
# `range` attribute, so nothing is being instructed and the figure is pure
# description. Narrow because that is what such wheels are: the ones with a
# settable range are the 900-degree Logitechs, and everything else is a small
# wheel that reaches full lock in about a third of a turn. Deliberately not
# DEFAULT_RANGE_DEG, which is an instruction to hardware that can obey it.
UNSETTABLE_RANGE_DEG = 270

TX_INTERVAL = 1.0 / TX_HZ
INFO_EVERY = max(1, TX_HZ // 2)          # INFO at ~2 Hz
SUBSCRIBER_TIMEOUT = 3.0                 # drop the subscriber after this silence
RESCAN_INTERVAL = 1.0                    # look for a newly plugged wheel
HOUSEKEEP_HZ = 20                        # status write + command poll rate

STATUS_PATH = os.path.join(RUN_DIR, "wheel.json")
CMD_PATH = os.path.join(RUN_DIR, "wheel-cmd.json")
# Same status/command pair for the keyboard bridge, read and written by
# adiona_controller.py on behalf of the kiosk page.
KEYS_STATUS_PATH = os.path.join(RUN_DIR, "keys.json")
KEYS_CMD_PATH = os.path.join(RUN_DIR, "keys-cmd.json")
# Written by adiona-updater.py. Read-only here, and entirely optional: a box with
# no updater service just reports zero flags.
UPDATE_STATUS_PATH = os.path.join(RUN_DIR, "update.json")
# Existence-checked only, to report BOXCAP_CASTING. Never executed from here.
PLAYER_PATH = os.environ.get("ADIONA_PLAYER", "/opt/adiona/kiosk/adiona-player.sh")

# The keyboard bridge is imported defensively. It is a convenience; the wheel is
# not. A syntax error or a missing file here must cost the keyboard and leave the
# steering, the pedals and the box's version report exactly as they were —
# Restart=always would otherwise turn it into a two-second crash loop.
KEYS_IMPORT_ERROR = ""
try:
    if HERE not in sys.path:
        sys.path.insert(0, HERE)
    import adiona_keys
except Exception as e:                             # deliberately broad
    # `as e` is unbound once the block ends, so the reason is copied out here.
    adiona_keys = None
    KEYS_IMPORT_ERROR = str(e) or e.__class__.__name__

# ── Wire format ──────────────────────────────────────────────────────────────
# Everything on UDP 5010, little-endian. Finalised in v1.7.0, before the first
# box shipped — after that, changing a field means updating a fleet.
#
# DISPATCH: read the 4-byte MAGIC, require the per-type minimum length below,
# decode the fields you know and IGNORE ANY TRAILING BYTES. Deliberately not
# dispatch-on-size, which is what this used to do: that made packet sizes a
# scarce resource ("48, 56 and 64 are taken") and turned every added field into a
# new message type AND a coordinated release.
#
# Two invariants make that work. They are the whole reason this format is final
# rather than frozen, so do not trade them away for a few bytes:
#
#   APPEND ONLY. Never reorder, resize or repurpose an existing field. A new one
#   goes on the END; a retired one becomes `reserved` and is still sent. A reader
#   that stops where its knowledge ends keeps working against a newer sender.
#
#   RESERVED MEANS ZERO. Senders write zero, readers ignore. That is what lets a
#   reserved field become real later with no version negotiation.
#
# STATE, minimum 48 bytes. A `reserved` u16 sits after `flags` purely so
# steer_deg lands on a 4-byte boundary; it is not optional padding, it is part
# of the format and must stay zero.
#   magic 'AW02' | seq u32 | flags u16 | reserved u16
#   steer_deg f32 | throttle u16 | brake u16 | buttons u32 | t_us i32
#   key_seq u16 | key_count u8 | reserved u8 | keys[4]
#
# keys[] is the last four keystrokes from the box's USB keyboard, oldest first;
# entry i has id = key_seq - key_count + 1 + i and each is
#   code u16 (Godot keycode, see adiona_keys.GODOT_KEYS) | mods u8 | action u8
#
# Riding in the wheel packet rather than in one of its own is what makes the
# keyboard reliable for free: there is no send scheduling and no retransmit
# anywhere, because a keystroke stays in the ring until four newer ones push it
# out and is therefore transmitted ~90 times. It also survives the headset's
# receive loop keeping only the NEWEST packet of each drain and discarding the
# rest — a keystroke that lived in one packet alone would be thrown away there.
KEY_SLOTS = 4
STATE_FMT = struct.Struct("<4sIHHfHHIiHBB" + "HBB" * KEY_SLOTS)
STATE_MAGIC = b"AW02"

# DEVICE, minimum 68 bytes, ~2 Hz PER ATTACHED DEVICE. One of these describes
# one thing plugged into the box: what it is, what it is called, and what it can
# do. The headset holds no per-device table of its own — same principle as the
# axis mapping, and the reason supporting new hardware is a deploy to one box.
#   magic 'AI02' | device_type u8 | device_index u8 | flags u16
#   capabilities u64 | range_deg u16 | reserved u16 | name 48s (NUL-padded)
#
# 48 bytes of name because "Logitech G920 Driving Force Racing Wheel" is 39 and a
# truncated name on the XR status line looks like a fault.
#
# flags vs capabilities is a distinction worth keeping: FLAGS ARE STATE THAT
# CHANGES WHILE RUNNING, CAPABILITIES ARE FACTS ABOUT THE HARDWARE. A capability
# answers "could this ever work", a flag answers "is it working now". Blur them
# and the capability word decays into a second status field.
NAME_LEN = 48
INFO_FMT = struct.Struct("<4sBBHQHH%ds" % NAME_LEN)
INFO_MAGIC = b"AI02"

# Device type. Ranges are reserved so a new class of hardware never renumbers an
# old one; 1-15 are driving controls, 16-31 vehicle sensors.
DEVICE_TYPE_UNKNOWN = 0
DEVICE_TYPE_WHEEL = 1
DEVICE_TYPE_PEDALS = 2               # only when they enumerate separately
DEVICE_TYPE_SHIFTER = 3
DEVICE_TYPE_SENSORS = 16             # USB vehicle sensor pack

# Device capabilities. Bits 10-31 are reserved for further input capabilities,
# 32-47 for vehicle-sensor ones (defined when that hardware lands), 48-63 spare.
CAP_STEERING = 1 << 0
CAP_PEDALS = 1 << 1
CAP_CLUTCH = 1 << 2
CAP_HANDBRAKE = 1 << 3
CAP_SHIFTER = 1 << 4
CAP_BUTTONS = 1 << 5
CAP_FORCE_FEEDBACK = 1 << 6          # accepts FF_AUTOCENTER
CAP_RANGE_SETTABLE = 1 << 7          # the sysfs `range` attribute exists
# Which rotation ranges the device supports, as opposed to range_deg, which is
# the one currently applied. Lets the headset offer only settings that will work.
CAP_RANGE_270 = 1 << 8
CAP_RANGE_900 = 1 << 9

# BOX, minimum 72 bytes, 1 Hz. What this box IS and what it CAN DO, so the
# headset can decide whether a feature is available without comparing version
# numbers — which is the thing that ages worst across a fleet on mixed releases.
#   magic 'AB02' | flags u16 | box_id u16
#   app_major u8 | app_minor u8 | app_patch u8 | os_id u8
#   os_major u16 | os_minor u16 | uptime_s u32 | box_caps u64
#   app_str 20s (NUL-padded) | os_str 24s (NUL-padded)
#
# Deliberately NOT folded into the device descriptor: one is about the box, the
# other about a thing plugged into it, and there can be several of the latter.
BOX_APP_LEN = 20
BOX_OS_LEN = 24
BOX_FMT = struct.Struct("<4sHHBBBBHHIQ%ds%ds" % (BOX_APP_LEN, BOX_OS_LEN))
BOX_MAGIC = b"AB02"

BOX_FLAG_UPDATE_AVAILABLE = 1 << 0
BOX_FLAG_UPDATE_IN_PROGRESS = 1 << 1
BOX_FLAG_JUST_UPDATED = 1 << 2       # applied within the last 10 minutes
BOX_FLAG_UPDATER_PRESENT = 1 << 3
BOX_FLAG_INTERNET_OK = 1 << 4

# Box capabilities — what this build can do at all, regardless of what is
# currently plugged in or running. Bits 5-63 reserved.
BOXCAP_WHEEL = 1 << 0                # this service, so always set
BOXCAP_KEYBOARD = 1 << 1             # the USB keyboard bridge is present
BOXCAP_SENSORS = 1 << 2              # USB vehicle sensor support is present
BOXCAP_UPDATER = 1 << 3
BOXCAP_CASTING = 1 << 4              # RTP video receiver

BOX_OS_UNKNOWN = 0
BOX_OS_RASPBIAN = 1
BOX_OS_DEBIAN = 2

# Subscribe keepalive from the headset, minimum 8 bytes:
#   magic 'ASUB' | nonce u32 | feature_flags u64
#
# feature_flags is the headset's ENTIRE licence mask, verbatim — never one bit,
# never a derived boolean. Bit 2 (in-vehicle sensors) is only its first consumer;
# carrying the whole word means a future capability is already on the wire the
# day the licence grants it, with no protocol change here.
#
# SUB_FMT DELIBERATELY DESCRIBES ONLY THE FIRST 8 BYTES. It is used as the
# minimum length in receiver_thread, so widening it to include the flags would
# silently start rejecting the 8-byte keepalive an older APK sends — the headset
# would never become a subscriber and the wheel stream would never start at all.
# Read the tail conditionally instead; see SUB_FLAGS_SIZE.
SUB_FMT = struct.Struct("<4sI")
SUB_MAGIC = b"ASUB"
SUB_FLAGS_FMT = struct.Struct("<Q")
SUB_FLAGS_OFFSET = 8
SUB_FLAGS_SIZE = SUB_FLAGS_OFFSET + SUB_FLAGS_FMT.size    # 16

FLAG_WHEEL_PRESENT = 1 << 0
FLAG_PEDALS_MAPPED = 1 << 1
FLAG_RANGE_APPLIED = 1 << 2
FLAG_BUTTONS_MAPPED = 1 << 3   # at least one button role is bound on this wheel
FLAG_KEYBOARD_PRESENT = 1 << 4  # a USB keyboard is plugged into the box

# The `buttons` field carries SEMANTIC ROLES, not raw button bits. Same principle
# as the axes: the box knows which physical button is the left paddle, the
# headset only ever learns "reverse was pressed". A different wheel with the
# paddles on different codes needs a box-side map change and nothing else.
#
# Confirm/cancel deliberately share the paddles by default, matching the Doyo
# profile in the game (btn_confirm == btn_drive, btn_cancel == btn_reverse):
# the same paddle means Drive while driving and OK in a menu.
BTN_REVERSE = 1 << 0
BTN_DRIVE = 1 << 1
BTN_CONFIRM = 1 << 2
BTN_CANCEL = 1 << 3

BUTTON_ROLES = ("reverse", "drive", "confirm", "cancel")
BUTTON_ROLE_BITS = {"reverse": BTN_REVERSE, "drive": BTN_DRIVE,
                    "confirm": BTN_CONFIRM, "cancel": BTN_CANCEL}

# ── evdev, via ioctl ─────────────────────────────────────────────────────────
# Linux asm-generic ioctl encoding: dir << 30 | size << 16 | type << 8 | nr
_IOC_READ = 2


def _ior(type_ch, nr, size):
    return (_IOC_READ << 30) | (size << 16) | (ord(type_ch) << 8) | nr


def EVIOCGNAME(length):
    return _ior("E", 0x06, length)


def EVIOCGBIT(ev, length):
    return _ior("E", 0x20 + ev, length)


def EVIOCGABS(abs_code):
    return _ior("E", 0x40 + abs_code, ABSINFO.size)


EV_SYN, EV_KEY, EV_ABS = 0x00, 0x01, 0x03
EV_MAX = 0x1F

# Force feedback. We use exactly one effect: FF_AUTOCENTER, the wheel's built-in
# spring back to centre. hid-logitech-hidpp already implements it, so this is a
# single write of a standard input_event rather than a hand-rolled HID++ packet —
# which would bypass the driver and break on a firmware or kernel change.
# FF_GAIN is set to full first so the autocenter percentage means what it says.
EV_FF = 0x15
FF_GAIN = 0x60
FF_AUTOCENTER = 0x61
FF_MAX = 0x7F
ABS_MAX = 0x3F
ABS_MT_SLOT = 0x2F                        # presence => touchscreen, not a wheel

# struct input_event on 64-bit: timeval{s64,s64}, u16 type, u16 code, s32 value
INPUT_EVENT = struct.Struct("=qqHHi")
# struct input_absinfo: s32 value, min, max, fuzz, flat, resolution
ABSINFO = struct.Struct("=iiiiii")

ABS_NAMES = {
    0x00: "ABS_X", 0x01: "ABS_Y", 0x02: "ABS_Z",
    0x03: "ABS_RX", 0x04: "ABS_RY", 0x05: "ABS_RZ",
    0x06: "ABS_THROTTLE", 0x07: "ABS_RUDDER", 0x08: "ABS_WHEEL",
    0x09: "ABS_GAS", 0x0A: "ABS_BRAKE",
    0x10: "ABS_HAT0X", 0x11: "ABS_HAT0Y",
    0x12: "ABS_HAT1X", 0x13: "ABS_HAT1Y",
}
ABS_CODES = {v: k for k, v in ABS_NAMES.items()}


def _bitmask_has(mask, bit):
    idx = bit // 8
    return idx < len(mask) and bool(mask[idx] & (1 << (bit % 8)))


def dev_name(fd):
    buf = bytearray(256)
    try:
        fcntl.ioctl(fd, EVIOCGNAME(len(buf)), buf)
    except OSError:
        return ""
    return buf.split(b"\x00", 1)[0].decode("utf-8", "replace")


def dev_has_ff(fd):
    """True if the device actually implements force feedback.

    Opening the node O_RDWR proves nothing about the hardware — a wheel derived
    from a gamepad board opens exactly the same way and supports no effects at
    all. Without this check the box tells the headset that such a wheel
    self-centres, and reports an autocentre percentage it never applied.

    Deliberately gated on EV_FF rather than on the FF_AUTOCENTER bit
    specifically: the tighter test cannot be verified here against a G920, and
    getting it wrong would disable centring on the one wheel that is known to
    work. A device that advertises EV_FF without autocentring still fails
    harmlessly at the write, which apply_autocenter already handles.
    """
    ev_mask = bytearray(EV_MAX // 8 + 1)
    try:
        fcntl.ioctl(fd, EVIOCGBIT(0, len(ev_mask)), ev_mask)
    except OSError:
        return False
    return _bitmask_has(ev_mask, EV_FF)


def dev_abs_axes(fd):
    """Return {code: absinfo tuple} for every ABS axis the device reports."""
    ev_mask = bytearray(EV_MAX // 8 + 1)
    try:
        fcntl.ioctl(fd, EVIOCGBIT(0, len(ev_mask)), ev_mask)
    except OSError:
        return {}
    if not _bitmask_has(ev_mask, EV_ABS):
        return {}

    abs_mask = bytearray(ABS_MAX // 8 + 1)
    try:
        fcntl.ioctl(fd, EVIOCGBIT(EV_ABS, len(abs_mask)), abs_mask)
    except OSError:
        return {}

    axes = {}
    for code in range(ABS_MAX + 1):
        if not _bitmask_has(abs_mask, code):
            continue
        buf = bytearray(ABSINFO.size)
        try:
            fcntl.ioctl(fd, EVIOCGABS(code), buf)
        except OSError:
            continue
        value, lo, hi, fuzz, flat, res = ABSINFO.unpack(bytes(buf))
        axes[code] = {"value": value, "min": lo, "max": hi,
                      "fuzz": fuzz, "flat": flat, "res": res}
    return axes


def sysfs_range_path(event_name):
    """
    Find the HID `range` attribute for /dev/input/<event_name>.

    hid-logitech-hidpp exposes the wheel's rotation range (in degrees) as a
    writable sysfs attribute a couple of levels above the input device. Walk up
    from /sys/class/input/<event> looking for it rather than hardcoding a depth,
    because the number of intermediate nodes differs between USB and Bluetooth
    parents and between kernel versions.
    """
    start = os.path.realpath(os.path.join("/sys/class/input", event_name))
    path = start
    for _ in range(6):
        path = os.path.dirname(path)
        if not path.startswith("/sys") or path == "/sys":
            break
        candidate = os.path.join(path, "range")
        if os.path.isfile(candidate):
            return candidate
    return None


# ── Device profiles ──────────────────────────────────────────────────────────
# Mirrors the game-side Global.HID_DEVICE_ALLOWLIST shape so both codebases
# describe controllers the same way. "name_match" is a case-insensitive
# substring of the evdev device name.
#
# A user map in /etc/adiona/wheel-map.json (written by the on-TV setup UI)
# overrides these, keyed by exact device name.
#
# VERIFY WITH --dump BEFORE TRUSTING THESE. The exact ABS_* assignment depends
# on the running hid-logitech-hidpp version. Also confirm the driver's
# `combine_pedals` parameter is 0 (its default) — if one axis moves for both
# pedals, they are combined and gas/brake cannot be separated here.
# VERIFIED against a real G920 (Logitech G920 Driving Force Racing Wheel,
# 046d:c262, hid-logitech-hidpp, HID++ 4.2). Do not "tidy" these without
# re-measuring — every value here contradicted a reasonable first guess:
#
#   steer  ABS_X   16-bit, logical 0..65535. Unambiguous: it is the only 16-bit
#                  axis, and the HID report descriptor declares it as Generic
#                  Desktop usage X.
#   gas    ABS_Y   MEASURED. Held 8.3 s -> ABS_Y fell to 0.
#   brake  ABS_Z   MEASURED. Held 10.1 s -> ABS_Z fell to 39.
#   clutch ABS_RZ  by elimination; unused (the sim has no clutch).
#
# The descriptor is NO help for the pedals: it declares them as bare Generic
# Desktop Y/Z/Rz, not Simulation Controls (Accelerator 0xC4 / Brake 0xC5 /
# Clutch 0xC6), because in Xbox-One mode the wheel presents as a generic
# Joystick. So there is nothing semantic to read and the mapping can only be
# measured. A first guess of gas=ABS_RZ was wrong — that is the CLUTCH.
#
# All three pedals are INVERTED: they rest at 255 and travel DOWN to 0 when
# pressed. Without invert the car starts at full throttle and full brake and
# eases off as you press.
WHEEL_PROFILES = [
    {
        "name_match": "g920",
        "steer": {"code": "ABS_X", "invert": False},
        "gas": {"code": "ABS_Y", "invert": True},
        "brake": {"code": "ABS_Z", "invert": True},
        "hw_range_deg": 900,
    },
    # Doyo L820 — the wheel that ships with many headsets, where it normally
    # pairs to the Quest over Bluetooth. Plugged into the box over USB instead it
    # runs at a fraction of the latency, which is the whole reason for this entry.
    #
    # IT DOES NOT SAY DOYO ANYWHERE. The board is ShanWan's (USB 2563:0526) and
    # it enumerates as "shanwan Android GamePad", so `name_match` has to be the
    # component vendor rather than anything printed on the product. Matching the
    # vendor alone would also catch a genuine ShanWan gamepad, which is harmless:
    # such a device already qualifies as a wheel candidate (it has ABS_X and no
    # touch slots), and this mapping is the sensible one for it anyway. If that
    # ever needs disambiguating, the vendor:product pair above is the ground truth.
    #
    # MEASURED on the attached device, from /proc/bus/input/devices and the box's
    # own /wheel dump — not guessed:
    #   steer  ABS_X      8-bit, 0..255, rests at 128 (dead centre, no offset)
    #   gas    ABS_GAS    rests at 0 and rises when pressed  -> NOT inverted
    #   brake  ABS_BRAKE  rests at 0 and rises when pressed  -> NOT inverted
    #
    # Unlike the G920 this device names its pedal axes semantically, and the
    # three unused stick axes (ABS_Y/ABS_Z/ABS_RZ) rest at 128 while exactly the
    # two pedal axes rest at 0 — so the assignment is corroborated from two
    # directions rather than resting on the excursion test alone.
    #
    # NO FORCE FEEDBACK: EV=1b in /proc/bus/input/devices carries no EV_FF bit,
    # so there is no autocentre to set. The wheel is sprung mechanically.
    #
    # NO SETTABLE RANGE either — this is not a hid-logitech-hidpp device and has
    # no sysfs `range` attribute. hw_range_deg therefore only scales the degrees
    # reported to the headset, which divides by the very same figure, so it is
    # display truth rather than feel. 270 is this wheel's physical travel.
    #
    # PADDLES, also measured — pressed one at a time while watching event codes:
    #   left  0x136 BTN_TL   right 0x137 BTN_TR
    # Corroborated twice over: the evdev names are the shoulder buttons, and the
    # game's own Doyo entry (Global.HID_DEVICE_ALLOWLIST) reaches the same two
    # through Godot's joypad buttons 9 and 10, which are the shoulders. Confirm
    # and cancel share them with drive and reverse, per the convention in
    # BUTTON_ROLE_BITS: the same paddle means Drive while driving and OK in a menu.
    {
        "name_match": "shanwan",
        "steer": {"code": "ABS_X", "invert": False},
        "gas": {"code": "ABS_GAS", "invert": False},
        "brake": {"code": "ABS_BRAKE", "invert": False},
        "hw_range_deg": 270,
        "buttons": {
            "reverse": 0x136, "cancel": 0x136,     # left paddle
            "drive": 0x137, "confirm": 0x137,      # right paddle
        },
    },
    # No G29 entry on purpose. It is the same driver family, but its pedal axis
    # order has not been measured, and a WRONG profile is far worse than none:
    # an unknown wheel falls through to the on-TV mapping UI (press F12 on the
    # box), whereas a wrong one silently puts the throttle on the brake pedal.
    # Add it here only after confirming it the same way these two were.
]

ROLES = ("steer", "gas", "brake")


def profile_for(name):
    """The built-in mapping for a device name, in the same shape as a saved user
    map — so a profiled wheel and a hand-mapped one take identical paths from
    here on. `buttons` is carried through when a profile declares it: a wheel
    whose paddles are known should arrive with its gears working, not just its
    axes."""
    lowered = name.lower()
    for prof in WHEEL_PROFILES:
        if prof["name_match"] in lowered:
            entry = {r: dict(prof[r]) for r in ROLES}
            entry["hw_range_deg"] = prof.get("hw_range_deg", DEFAULT_RANGE_DEG)
            if prof.get("buttons"):
                entry["buttons"] = dict(prof["buttons"])
            return entry
    return None


def load_user_map():
    try:
        with open(MAP_PATH) as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return {}


def save_user_map(data):
    tmp = MAP_PATH + ".tmp"
    try:
        os.makedirs(os.path.dirname(MAP_PATH), exist_ok=True)
        with open(tmp, "w") as fh:
            json.dump(data, fh, indent=2)
        os.replace(tmp, MAP_PATH)
        return True
    except OSError as e:
        log("could not save %s: %s" % (MAP_PATH, e))
        return False


def log(msg):
    print("[adiona-wheel] %s" % msg, flush=True)


# ── Box report (AB02) ────────────────────────────────────────────────────────
# Everything here is cheap and local. It lives in the wheel service rather than
# in a service of its own because the headset's socket is CONNECTED to
# <box>:5010 — datagrams from any other source port are dropped by its OS — and
# a second socket on 5010 would split the headset's ASUB keepalives between two
# processes. So this is the only process that can reach the headset at all.
BOX_INFO = {
    "app_str": "",
    "app_num": (0, 0, 0),
    "os_id": BOX_OS_UNKNOWN,
    "os_major": 0,
    "os_minor": 0,
    "os_str": "",
    "box_id": 0,
    "version_mtime": None,
}
BOX_FLAGS = [0]                          # refreshed at 1 Hz by housekeeping


def parse_semver(text):
    """'1.5.0' -> (1, 5, 0). Anything else -> (0, 0, 0), and the headset must
    treat that as 'unknown' rather than 'ancient' — app_str stays authoritative."""
    parts = text.strip().split(".")
    if len(parts) != 3:
        return (0, 0, 0)
    out = []
    for p in parts:
        if not p.isdigit():
            return (0, 0, 0)
        n = int(p)
        if n > 255:
            return (0, 0, 0)
        out.append(n)
    return tuple(out)


def read_os_release():
    """(os_id, major, minor, label) from /etc/os-release.

    The label is composed as "Debian 13 (trixie)" rather than taken from
    PRETTY_NAME, which is "Debian GNU/Linux 13 (trixie)" — 28 characters, four
    over the 24 the packet carries, and a truncated OS name on the headset's
    status line reads as a fault rather than as a field-width limit."""
    fields = {}
    try:
        with open(OS_RELEASE) as fh:
            for line in fh:
                key, _, val = line.strip().partition("=")
                if key:
                    fields[key] = val.strip().strip('"').strip("'")
    except OSError:
        return BOX_OS_UNKNOWN, 0, 0, ""
    ident = fields.get("ID", "").lower()
    os_id = (BOX_OS_RASPBIAN if ident == "raspbian" else
             BOX_OS_DEBIAN if ident == "debian" else BOX_OS_UNKNOWN)
    major = minor = 0
    bits = fields.get("VERSION_ID", "").split(".")
    if bits and bits[0].isdigit():
        major = min(int(bits[0]), 0xFFFF)
    if len(bits) > 1 and bits[1].isdigit():
        minor = min(int(bits[1]), 0xFFFF)

    name = ident.capitalize() if ident else ""
    ver = fields.get("VERSION_ID", "")
    code = fields.get("VERSION_CODENAME", "")
    label = " ".join(p for p in (name, ver) if p)
    if code:
        label = ("%s (%s)" % (label, code)).strip()
    return os_id, major, minor, label or fields.get("PRETTY_NAME", "")


def utf8_fit(text, limit):
    """Encode, truncating on a codepoint boundary so the headset never has to
    decode half a character."""
    raw = text.encode("utf-8")
    if len(raw) <= limit:
        return raw
    while limit > 0 and (raw[limit] & 0xC0) == 0x80:
        limit -= 1
    return raw[:limit]


def read_box_id():
    """The 4 hex chars adiona-firstboot.sh derives from the MAC, as a u16. This is
    the only fleet-unique identifier the box has that is not an RFC1918 address
    identical on every box."""
    try:
        with open(SSID_FILE) as fh:
            ssid = fh.read().strip()
    except OSError:
        return 0
    suffix = ssid.rsplit("-", 1)[-1]
    try:
        return int(suffix, 16) & 0xFFFF
    except ValueError:
        return 0


def read_box_info():
    """Refresh the cached box identity. VERSION is re-read when its mtime moves,
    so an OTA that swaps the release under us is reported without a restart."""
    try:
        mtime = os.stat(VERSION_FILE).st_mtime
    except OSError:
        mtime = None
    if mtime is not None and mtime == BOX_INFO["version_mtime"] and BOX_INFO["app_str"]:
        return
    try:
        with open(VERSION_FILE) as fh:
            app = fh.read().strip()
    except OSError:
        app = ""
    os_id, os_major, os_minor, pretty = read_os_release()
    BOX_INFO.update(app_str=app, app_num=parse_semver(app), os_id=os_id,
                    os_major=os_major, os_minor=os_minor, os_str=pretty,
                    box_id=read_box_id(), version_mtime=mtime)


def box_capabilities():
    """What this box CAN do, as opposed to what it is doing right now.

    Derived from what is actually installed rather than inferred from a version
    number: the headset asks "does this box have a keyboard bridge", not "is it
    newer than 1.7.0". Version comparisons are the thing that ages worst across a
    fleet running mixed releases."""
    caps = BOXCAP_WHEEL                       # this service is the wheel
    if adiona_keys is not None and KEYS_ENABLED:
        caps |= BOXCAP_KEYBOARD
    if os.path.exists(UPDATE_STATUS_PATH):
        caps |= BOXCAP_UPDATER
    if os.path.isfile(PLAYER_PATH):
        caps |= BOXCAP_CASTING
    return caps


def refresh_box_flags():
    """Fold adiona-updater.py's status file into the AB02 flag bits. An absent or
    unparseable file means zero flags — the updater is optional."""
    try:
        with open(UPDATE_STATUS_PATH) as fh:
            upd = json.load(fh)
    except (OSError, ValueError):
        BOX_FLAGS[0] = 0
        return
    flags = BOX_FLAG_UPDATER_PRESENT
    state = upd.get("state", "")
    if upd.get("available"):
        flags |= BOX_FLAG_UPDATE_AVAILABLE
    if state in ("downloading", "verifying", "staged", "applying", "health"):
        flags |= BOX_FLAG_UPDATE_IN_PROGRESS
    result = upd.get("last_result") or {}
    if result.get("ok") and (time.time() - float(result.get("at", 0))) < 600:
        flags |= BOX_FLAG_JUST_UPDATED
    if upd.get("internet"):
        flags |= BOX_FLAG_INTERNET_OK
    BOX_FLAGS[0] = flags


def uptime_seconds():
    try:
        with open("/proc/uptime") as fh:
            return min(int(float(fh.read().split()[0])), 0xFFFFFFFF)
    except (OSError, ValueError, IndexError):
        return 0


def box_packet():
    read_box_info()
    return BOX_FMT.pack(
        BOX_MAGIC, BOX_FLAGS[0] & 0xFFFF, BOX_INFO["box_id"],
        BOX_INFO["app_num"][0], BOX_INFO["app_num"][1], BOX_INFO["app_num"][2],
        BOX_INFO["os_id"], BOX_INFO["os_major"], BOX_INFO["os_minor"],
        uptime_seconds(), box_capabilities(),
        utf8_fit(BOX_INFO["app_str"], BOX_APP_LEN),
        utf8_fit(BOX_INFO["os_str"], BOX_OS_LEN))


# ── Shared state ─────────────────────────────────────────────────────────────
LOCK = threading.Lock()

STATE = {
    "present": False,
    "name": "",
    "event": "",
    "range_deg": DEFAULT_RANGE_DEG,
    "range_applied": False,
    "mapped": False,
    "map": {r: None for r in ROLES},
    "steer_deg": 0.0,
    "throttle": 0.0,          # 0..1
    "brake": 0.0,             # 0..1
    "buttons": 0,             # SEMANTIC role bitmask (BTN_REVERSE/DRIVE/...)
    "pressed": [],            # EV_KEY codes currently held, for the setup UI
    "last_button": None,      # last EV_KEY code pressed, for the setup UI
    "button_map": {},         # role -> EV_KEY code
    "centre": None,           # raw steering value treated as dead ahead
    "steer_raw": None,        # live raw steering, so the UI can show the offset
    "autocenter": DEFAULT_AUTOCENTER_PCT,   # spring-to-centre strength, 0-100%
    "ff_available": False,    # False when the device could not be opened O_RDWR
    "caps": 0,                # CAP_* word for the attached device, 0 when none
    "axes": {},               # code -> {"value": norm, "excursion": float, "raw": int}
    "subscriber": None,
    "tx_hz": TX_HZ,
    "seq": 0,
    "keyboard": False,        # a USB keyboard is plugged into the box
    # The headset's ENTIRE licence mask, exactly as it sent it — one integer,
    # never decomposed into per-feature booleans here or anywhere downstream.
    # Each consumer tests the bit it cares about; that is what makes the NEXT
    # licensed feature a one-line change instead of a two-repo one.
    #
    # None, not 0: null means "no headset has ever told us", which 0 cannot
    # express — and a fully-licensed older APK, which sends no mask at all,
    # would otherwise read as "licensed nothing" and have its options hidden.
    # Never cleared once set, so the value survives a headset disconnecting
    # mid-event; freshness, when something needs it, is STATE["subscriber"].
    "feature_flags": None,
}

RUNNING = True

# The keyboard bridge, once main() has started it. None means no keyboard on this
# box, the module failed to import, or KEYS_ENABLED=0 — in all three cases every
# keystroke field on the wire stays zero and the headset simply never sees a key.
KEYS = [None]


def keys_fields():
    """The keystroke half of STATE_FMT, flattened for pack()."""
    empty = (0, 0, 0) + (0, 0, 0) * KEY_SLOTS
    reader = KEYS[0]
    if reader is None:
        return empty
    seq, ring = reader.snapshot()
    ring = list(ring)[-KEY_SLOTS:]
    fields = [seq, len(ring), 0]
    for code, mods, action in ring:
        fields += [code, mods, action]
    # Unused slots stay zero. The headset reads only the first key_count of them,
    # so their contents are irrelevant — but a zeroed tail makes a hex dump of a
    # captured packet readable, which is worth more than the microsecond.
    fields += [0, 0, 0] * (KEY_SLOTS - len(ring))
    return tuple(fields)


def norm_symmetric(raw, info, centre=None):
    """
    Map a raw axis value to [-1, +1] about the wheel's centre.

    `centre` defaults to the axis midpoint, which is right for most wheels — but
    not all. The G920 measured here rests at 33917 against a midpoint of 32768,
    a stable +3.5% that would show up in the sim as ~16 degrees of permanent
    right steering. So a per-device centre can be captured from the box's setup
    screen and stored in the map.

    The two sides are scaled independently against their own travel, so an
    off-centre rest still reaches exactly -1.0 at full left and +1.0 at full
    right instead of clipping early on the short side.
    """
    lo, hi = info["min"], info["max"]
    if centre is None:
        centre = (hi + lo) / 2.0
    centre = max(lo, min(hi, centre))
    if raw >= centre:
        span = hi - centre
        return 0.0 if span <= 0 else min(1.0, (raw - centre) / float(span))
    span = centre - lo
    return 0.0 if span <= 0 else max(-1.0, (raw - centre) / float(span))


def norm_unipolar(raw, info, invert):
    """Map a raw axis value to [0, 1], with the axis's own deadzone at rest."""
    lo, hi = info["min"], info["max"]
    span = hi - lo
    if span <= 0:
        return 0.0
    v = (raw - lo) / float(span)
    if invert:
        v = 1.0 - v
    # `flat` is the driver's declared deadzone; fall back to 2% so a pedal at
    # rest reads exactly zero instead of jittering the engine force.
    dead = (info["flat"] / float(span)) if info["flat"] > 0 else 0.02
    if v <= dead:
        return 0.0
    return min(1.0, (v - dead) / (1.0 - dead))


class Wheel:
    """One open evdev device plus its role mapping and live axis state."""

    def __init__(self, path, event_name, fd, name, axes, mapping, writable=False):
        self.path = path
        self.event_name = event_name
        self.fd = fd
        self.name = name
        self.axes = axes                       # code -> absinfo dict
        self.mapping = mapping                 # role -> {"code","invert"} or None
        self.raw = {c: a["value"] for c, a in axes.items()}
        self.norm = {c: 0.0 for c in axes}
        self.excursion = {c: 0.0 for c in axes}
        self.pressed = set()            # EV_KEY codes currently held
        self.last_button = None         # last EV_KEY code pressed, for the setup UI
        # role -> EV_KEY code. Populated from the profile/map; empty means the
        # wheel's buttons are unmapped and no roles are ever reported.
        self.button_map = dict((mapping or {}).get("buttons", {}))
        self.range_applied = False
        # Raw steering value treated as dead ahead. None = use the axis midpoint.
        self.centre = (mapping or {}).get("centre")
        self.autocenter_pct = int((mapping or {}).get("autocenter", DEFAULT_AUTOCENTER_PCT))
        # Both halves are required: the node has to be writable AND the device
        # has to implement force feedback. Writability alone was reported as
        # "autocentre available" until a Doyo/ShanWan wheel — which opens
        # perfectly well and supports no effects whatever — showed the claim up.
        self.ff_available = writable and dev_has_ff(fd)
        # Resolved once, here, rather than per packet: sysfs_range_path() walks
        # the device tree, and the answer cannot change while the device is open.
        self.range_settable = sysfs_range_path(event_name) is not None

        # Rotation range. A profile or a saved map states it outright; otherwise
        # it depends on whether the range can be SET at all:
        #
        #   settable    -> DEFAULT_RANGE_DEG is a real instruction. We write it to
        #                  the device and the hardware then travels that far.
        #   not settable-> the number describes the wheel, and nothing more. A
        #                  small wheel described as 900 deg is simply a false
        #                  statement: it appears on the headset's status line, in
        #                  every log, and in the setup screen. Assume the narrow
        #                  end instead, which is what a wheel without a range
        #                  attribute almost always is.
        requested = (mapping or {}).get("hw_range_deg")
        if requested is None:
            requested = (DEFAULT_RANGE_DEG if self.range_settable
                         else UNSETTABLE_RANGE_DEG)
        self.range_deg = requested

    def capabilities(self):
        """The device's CAP_* word — what this hardware can do.

        Recomputed rather than cached because the role and button maps change
        while somebody is setting the wheel up on the box's screen, and the
        headset should see that as it happens."""
        caps = 0
        if self.role_code("steer") is not None:
            caps |= CAP_STEERING
        if self.role_code("gas") is not None and self.role_code("brake") is not None:
            caps |= CAP_PEDALS
        if self.button_map:
            caps |= CAP_BUTTONS
        if self.ff_available:
            caps |= CAP_FORCE_FEEDBACK
        if self.range_settable:
            # Both are offered by the setup UI and validated by the controller;
            # without a writable range attribute neither can actually be applied.
            caps |= CAP_RANGE_SETTABLE | CAP_RANGE_270 | CAP_RANGE_900
        return caps

    def close(self):
        try:
            os.close(self.fd)
        except OSError:
            pass

    def apply_range(self, deg):
        """Write the rotation range to sysfs. Non-fatal if unsupported."""
        self.range_deg = deg
        path = sysfs_range_path(self.event_name)
        if not path:
            self.range_applied = False
            log("no sysfs `range` attribute for %s — falling back to "
                "axis-relative scaling at %d deg" % (self.name, deg))
            return False
        try:
            with open(path, "w") as fh:
                fh.write("%d\n" % deg)
            self.range_applied = True
            log("range set to %d deg via %s" % (deg, path))
            return True
        except OSError as e:
            self.range_applied = False
            log("could not write range to %s: %s" % (path, e))
            return False

    def apply_autocenter(self, pct=None):
        """
        Set the wheel's spring-to-centre strength, 0-100%.

        Without this the G920 has no centring at all: the wheel stays wherever
        you leave it, which in a driving sim means the car holds a turn after
        you let go. Non-fatal if the device could not be opened for writing —
        input still works, there is just no centring.
        """
        if pct is not None:
            self.autocenter_pct = max(0, min(100, int(pct)))
        if not self.ff_available:
            log("%s has no force feedback — autocenter unavailable "
                "(the wheel centres mechanically or not at all)" % self.name)
            return False
        try:
            # Full gain first, so the autocenter percentage is absolute rather
            # than a fraction of some previously-set gain.
            os.write(self.fd, INPUT_EVENT.pack(0, 0, EV_FF, FF_GAIN, 0xFFFF))
            os.write(self.fd, INPUT_EVENT.pack(
                0, 0, EV_FF, FF_AUTOCENTER,
                int(0xFFFF * self.autocenter_pct / 100.0)))
            log("autocenter set to %d%%" % self.autocenter_pct)
            return True
        except OSError as e:
            log("could not set autocenter: %s" % e)
            return False

    def role_code(self, role):
        m = self.mapping.get(role) if self.mapping else None
        return ABS_CODES.get(m["code"]) if m else None

    def role_invert(self, role):
        m = self.mapping.get(role) if self.mapping else None
        return bool(m and m.get("invert"))

    def pump(self):
        """Drain pending events. Returns False if the device went away."""
        try:
            data = os.read(self.fd, INPUT_EVENT.size * 64)
        except OSError as e:
            if e.errno in (errno.EAGAIN, errno.EWOULDBLOCK):
                return True
            return False                        # ENODEV: unplugged
        if not data:
            return False

        for off in range(0, len(data) - INPUT_EVENT.size + 1, INPUT_EVENT.size):
            _s, _us, etype, code, value = INPUT_EVENT.unpack_from(data, off)
            if etype == EV_ABS and code in self.axes:
                info = self.axes[code]
                prev = self.norm[code]
                self.raw[code] = value
                self.norm[code] = norm_symmetric(value, info)
                self.excursion[code] += abs(self.norm[code] - prev)
            elif etype == EV_KEY:
                # Full EV_KEY codes, NOT a packed bitmask. The G920 reports
                # buttons in two disjoint ranges — 0x120-0x12F (BTN_TRIGGER..
                # BTN_DEAD) and 0x2C0-0x2C1 (BTN_TRIGGER_HAPPY1/2) — and any
                # low-bit packing aliases them: 0x2C0 & 0x1F == 0x120 & 0x1F.
                # A role bound to button 17 would then fire from button 1.
                if value:
                    self.pressed.add(code)
                    self.last_button = code
                else:
                    self.pressed.discard(code)
        return True

    def read_roles(self):
        """Return (steer_deg, throttle 0..1, brake 0..1)."""
        steer_deg = 0.0
        code = self.role_code("steer")
        if code is not None and code in self.axes:
            v = norm_symmetric(self.raw[code], self.axes[code], self.centre)
            if self.role_invert("steer"):
                v = -v
            steer_deg = v * (self.range_deg / 2.0)

        pedals = []
        for role in ("gas", "brake"):
            c = self.role_code(role)
            if c is None or c not in self.axes:
                pedals.append(0.0)
            else:
                pedals.append(norm_unipolar(self.raw[c], self.axes[c],
                                            self.role_invert(role)))
        return steer_deg, pedals[0], pedals[1]

    def role_buttons(self):
        """
        Translate the pressed physical buttons into the semantic role bitmask
        that goes on the wire. Returns 0 when the buttons are unmapped, so an
        unrecognised wheel simply has no gear paddles rather than random ones.
        """
        mask = 0
        for role, code in self.button_map.items():
            bit = BUTTON_ROLE_BITS.get(role)
            if bit is None or code is None:
                continue
            if int(code) in self.pressed:
                mask |= bit
        return mask

    def arm(self):
        for c in self.excursion:
            self.excursion[c] = 0.0
        self.last_button = None


# ── Device discovery ─────────────────────────────────────────────────────────
def candidate_devices():
    """Yield (path, event_name, fd, name, axes, writable) for every plausible wheel."""
    try:
        entries = sorted(e for e in os.listdir("/dev/input") if e.startswith("event"))
    except OSError:
        return
    for event_name in entries:
        path = os.path.join("/dev/input", event_name)
        # O_RDWR is needed to send force feedback (autocenter). Fall back to
        # read-only rather than skipping the device: a wheel with no centring is
        # still perfectly drivable, and refusing to open it would not be.
        writable = True
        try:
            fd = os.open(path, os.O_RDWR | os.O_NONBLOCK)
        except OSError:
            writable = False
            try:
                fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK)
            except OSError:
                continue
        name = dev_name(fd)
        axes = dev_abs_axes(fd)
        # A wheel has absolute axes including ABS_X, and is not a touchscreen.
        if not axes or ABS_CODES["ABS_X"] not in axes or ABS_MT_SLOT in axes:
            os.close(fd)
            continue
        yield path, event_name, fd, name, axes, writable


def open_wheel():
    """
    Find and open the best wheel. Returns a Wheel or None.

    A device with a known or user-saved mapping always wins over an unmapped
    one, so plugging a keyboard-with-axes oddity in alongside the wheel cannot
    steal the session.
    """
    user_map = load_user_map()
    best = None
    for path, event_name, fd, name, axes, writable in candidate_devices():
        mapping = user_map.get(name) or profile_for(name)
        cand = (path, event_name, fd, name, axes, mapping, writable)
        if best is None:
            best = cand
        elif mapping and not best[5]:
            os.close(best[2])
            best = cand
        else:
            os.close(fd)
    if best is None:
        return None

    path, event_name, fd, name, axes, mapping, writable = best
    wheel = Wheel(path, event_name, fd, name, axes, mapping or {}, writable)
    if mapping:
        wheel.apply_range(mapping.get("hw_range_deg", DEFAULT_RANGE_DEG))
        log("wheel '%s' on %s — mapped %s" %
            (name, path, {r: mapping[r]["code"] for r in ROLES if mapping.get(r)}))
    else:
        log("wheel '%s' on %s — NO MAPPING. Map it from the box's TV "
            "(press F12 on the kiosk screen)." % (name, path))
    # Applied whether or not the axes are mapped: centring is a property of the
    # wheel, not of whether we know which axis is the throttle.
    wheel.apply_autocenter()
    return wheel


# ── Status + command files ───────────────────────────────────────────────────
def write_json(path, body):
    """Publish a status file atomically. Silent on failure: /run may not exist
    yet on a workstation, and no status file is worth a traceback."""
    try:
        os.makedirs(RUN_DIR, exist_ok=True)
    except OSError:
        return
    tmp = path + ".tmp"
    try:
        with open(tmp, "w") as fh:
            fh.write(body)
        os.replace(tmp, path)
    except OSError:
        pass


def write_status():
    with LOCK:
        body = json.dumps(STATE)
    write_json(STATUS_PATH, body)


def write_keys_status():
    """Publish the keyboard bridge's state for the kiosk page.

    Written even with no keyboard attached, so the page can tell "no keyboard on
    this box" apart from "the wheel service is not running"."""
    reader = KEYS[0]
    status = reader.status() if reader else {
        "present": False, "devices": [], "forwarding": False,
        "panel_seq": 0, "panel_open": False, "tab": "wifi", "key_seq": 0,
    }
    status["enabled"] = KEYS_ENABLED and adiona_keys is not None
    write_json(KEYS_STATUS_PATH, json.dumps(status))


def read_command(last_seq, path=CMD_PATH):
    """Return (seq, command dict) if a newer command is waiting, else (last_seq, None)."""
    try:
        with open(path) as fh:
            cmd = json.load(fh)
    except (OSError, ValueError):
        return last_seq, None
    seq = int(cmd.get("seq", 0))
    if seq <= last_seq:
        return last_seq, None
    return seq, cmd


def device_packets():
    """One AI02 descriptor per device attached to the box.

    CALLER MUST HOLD LOCK. A list rather than a single packet because the USB
    vehicle sensors will append their own entry here; today it is the wheel or
    nothing at all.

    The `flags` here describe the DEVICE, not the stream — deliberately narrower
    than STATE's flags, which also carry the keyboard. Capabilities say what the
    hardware could ever do; flags say what it is doing now.
    """
    if not STATE["present"]:
        return ()
    flags = 0
    if STATE["mapped"]:
        flags |= FLAG_PEDALS_MAPPED
    if STATE["range_applied"]:
        flags |= FLAG_RANGE_APPLIED
    if STATE["button_map"]:
        flags |= FLAG_BUTTONS_MAPPED
    flags |= FLAG_WHEEL_PRESENT
    return (INFO_FMT.pack(INFO_MAGIC, DEVICE_TYPE_WHEEL, 0, flags,
                          STATE["caps"], STATE["range_deg"], 0,
                          utf8_fit(STATE["name"], NAME_LEN)),)


# ── Threads ──────────────────────────────────────────────────────────────────
def receiver_thread(sock):
    """Record the headset's address from its ASUB keepalives."""
    sock.settimeout(1.0)
    while RUNNING:
        try:
            data, addr = sock.recvfrom(64)
        except socket.timeout:
            continue
        except OSError:
            time.sleep(0.2)
            continue
        # Minimum length, NOT exact: an older APK sends 8 bytes and must still be
        # able to subscribe, and a newer one may append fields we do not know.
        if len(data) < SUB_FMT.size or data[:4] != SUB_MAGIC:
            continue
        flags = None
        if len(data) >= SUB_FLAGS_SIZE:
            flags = SUB_FLAGS_FMT.unpack_from(data, SUB_FLAGS_OFFSET)[0]
        with LOCK:
            if STATE["subscriber"] != addr[0]:
                log("subscriber %s%s" %
                    (addr[0], "" if flags is None
                     else " (licence 0x%X)" % flags))
            STATE["subscriber"] = addr[0]
            # Only ever overwritten by a headset that actually sent one, so an
            # older APK cannot erase what a newer one told us.
            if flags is not None:
                STATE["feature_flags"] = flags
        SUBSCRIBER["addr"] = addr
        SUBSCRIBER["seen"] = time.monotonic()


SUBSCRIBER = {"addr": None, "seen": 0.0}


def sender_thread(sock):
    """Fixed-rate STATE at TX_HZ, a DEVICE descriptor per attached device at
    ~2 Hz, BOX at 1 Hz. Never blocks on the wheel.

    `tick` only advances while a subscriber is live (see the `continue` below), so
    every sub-rate cadence here is "per second of streaming", independent of
    WHEEL_TX_HZ. Do not move the increment above that check."""
    seq = 0
    tick = 0
    next_send = time.monotonic()
    while RUNNING:
        now = time.monotonic()
        if now < next_send:
            time.sleep(min(TX_INTERVAL, next_send - now))
            continue
        # Absolute schedule, so a slow tick does not permanently shift the phase.
        next_send += TX_INTERVAL
        if next_send < now:
            next_send = now + TX_INTERVAL

        addr = SUBSCRIBER["addr"]
        if not addr or (now - SUBSCRIBER["seen"]) > SUBSCRIBER_TIMEOUT:
            if addr:
                log("subscriber timed out")
                SUBSCRIBER["addr"] = None
                with LOCK:
                    STATE["subscriber"] = None
            continue

        seq = (seq + 1) & 0xFFFFFFFF
        tick += 1
        with LOCK:
            flags = 0
            if STATE["present"]:
                flags |= FLAG_WHEEL_PRESENT
            if STATE["mapped"]:
                flags |= FLAG_PEDALS_MAPPED
            if STATE["range_applied"]:
                flags |= FLAG_RANGE_APPLIED
            if STATE["button_map"]:
                flags |= FLAG_BUTTONS_MAPPED
            if STATE["keyboard"]:
                flags |= FLAG_KEYBOARD_PRESENT
            steer = STATE["steer_deg"]
            throttle = int(round(STATE["throttle"] * 65535))
            brake = int(round(STATE["brake"] * 65535))
            buttons = STATE["buttons"]
            STATE["seq"] = seq
            # Built under the same lock rather than re-read later, so a device
            # unplugged mid-tick cannot be described half from one state and half
            # from the next.
            descriptors = device_packets() if tick % INFO_EVERY == 0 else ()

        t_us = int(time.monotonic_ns() // 1000) & 0x7FFFFFFF
        # Read the keystroke ring here, not under the lock above: it has its own,
        # and holding both at once is the shape a deadlock grows from.
        pkt = STATE_FMT.pack(STATE_MAGIC, seq, flags, 0, steer,
                             throttle, brake, buttons, t_us, *keys_fields())
        try:
            sock.sendto(pkt, addr)
            # One descriptor per attached device. Today that is the wheel or
            # nothing; the vehicle sensors will simply add entries.
            for descriptor in descriptors:
                sock.sendto(descriptor, addr)
            # Offset by one tick so this never lands on the same tick as a
            # DEVICE descriptor (INFO_EVERY divides TX_HZ), which would put
            # three or more datagrams back to back once a second.
            if tick % TX_HZ == 1:
                sock.sendto(box_packet(), addr)
        except OSError:
            pass                                # drop-tolerant by design


def housekeeping_thread(get_wheel, do_command):
    """Publish status and poll for setup commands from the controller."""
    last_seq = 0
    last_keys_seq = 0
    interval = 1.0 / HOUSEKEEP_HZ
    ticks = 0
    while RUNNING:
        reader = KEYS[0]
        if reader is not None:
            with LOCK:
                STATE["keyboard"] = reader.present()
        write_status()
        write_keys_status()
        # The updater's status file only needs reading at the rate we transmit
        # it, not at the 20 Hz this loop runs at.
        if ticks % HOUSEKEEP_HZ == 0:
            refresh_box_flags()
        ticks += 1
        last_seq, cmd = read_command(last_seq)
        if cmd:
            try:
                do_command(cmd)
            except Exception as e:              # a bad command must never kill us
                log("command failed: %s" % e)
        # The panel's open/closed state, straight from the kiosk page. It is the
        # only thing that can tell the reader to let go of the keyboard, because
        # a page that has the keys is a page we cannot hear.
        last_keys_seq, keys_cmd = read_command(last_keys_seq, KEYS_CMD_PATH)
        if keys_cmd and reader is not None:
            try:
                reader.set_panel(bool(keys_cmd.get("open")), keys_cmd.get("tab"))
            except Exception as e:
                log("panel command failed: %s" % e)
        time.sleep(interval)


# ── Main loop ────────────────────────────────────────────────────────────────
def subscriber_live():
    """True while a headset's ASUB keepalives are still arriving.

    The keyboard bridge gates its exclusive grab on this: with no headset there
    is nowhere to send a keystroke, so taking the keyboard away from Chromium
    would cost the Ctrl+Alt+F2 shell and buy nothing."""
    addr = SUBSCRIBER["addr"]
    return bool(addr) and (time.monotonic() - SUBSCRIBER["seen"]) <= SUBSCRIBER_TIMEOUT


def start_keyboard():
    """Bring up the USB keyboard bridge, or explain in the log why not."""
    if adiona_keys is None:
        log("keyboard bridge unavailable: %s" % KEYS_IMPORT_ERROR)
        return
    if not KEYS_ENABLED:
        log("keyboard bridge disabled (KEYS_ENABLED=0)")
        return
    reader = adiona_keys.KeyReader(
        log, repeat_hz=KEYS_REPEAT_HZ, enabled=True,
        update_status_path=UPDATE_STATUS_PATH, subscriber_live=subscriber_live)
    KEYS[0] = reader
    threading.Thread(target=reader.run, daemon=True).start()
    log("keyboard bridge started (repeat %d Hz)" % KEYS_REPEAT_HZ)


def publish_wheel(wheel):
    """Copy the wheel's live values into STATE for the sender and the UI."""
    if wheel is None:
        with LOCK:
            STATE["present"] = False
            STATE["name"] = ""
            STATE["event"] = ""
            STATE["mapped"] = False
            STATE["range_applied"] = False
            STATE["map"] = {r: None for r in ROLES}
            STATE["steer_deg"] = 0.0
            STATE["throttle"] = 0.0
            STATE["brake"] = 0.0
            STATE["buttons"] = 0
            STATE["axes"] = {}
            STATE["caps"] = 0
        return

    steer, throttle, brake = wheel.read_roles()
    mapped = all(wheel.role_code(r) is not None for r in ROLES)
    axes = {}
    for code in wheel.axes:
        axes[ABS_NAMES.get(code, "ABS_%d" % code)] = {
            "value": round(wheel.norm[code], 4),
            "excursion": round(wheel.excursion[code], 3),
            "raw": wheel.raw[code],
        }
    steer_code = wheel.role_code("steer")
    steer_raw = wheel.raw.get(steer_code) if steer_code is not None else None
    with LOCK:
        STATE["present"] = True
        STATE["name"] = wheel.name
        STATE["event"] = wheel.event_name
        STATE["mapped"] = mapped
        STATE["range_deg"] = wheel.range_deg
        STATE["range_applied"] = wheel.range_applied
        STATE["map"] = {r: (wheel.mapping.get(r) if wheel.mapping else None)
                        for r in ROLES}
        STATE["steer_deg"] = round(steer, 3)
        STATE["throttle"] = round(throttle, 4)
        STATE["brake"] = round(brake, 4)
        STATE["buttons"] = wheel.role_buttons()
        STATE["pressed"] = sorted("0x%03x" % c for c in wheel.pressed)
        STATE["last_button"] = ("0x%03x" % wheel.last_button
                                if wheel.last_button is not None else None)
        STATE["button_map"] = dict(wheel.button_map)
        STATE["axes"] = axes
        STATE["centre"] = wheel.centre
        STATE["steer_raw"] = steer_raw
        STATE["autocenter"] = wheel.autocenter_pct
        STATE["ff_available"] = wheel.ff_available
        STATE["caps"] = wheel.capabilities()


def main():
    global RUNNING

    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind(("0.0.0.0", WHEEL_PORT))
    log("listening on 0.0.0.0:%d, tx %d Hz" % (WHEEL_PORT, TX_HZ))
    read_box_info()
    log("box report: v%s on %s (id %04X)" %
        (BOX_INFO["app_str"] or "?", BOX_INFO["os_str"] or "?", BOX_INFO["box_id"]))
    start_keyboard()

    current = {"wheel": None}

    def do_command(cmd):
        """Handle one setup command from the on-TV UI (via the controller)."""
        wheel = current["wheel"]
        action = cmd.get("action")
        if action == "arm":
            if wheel:
                wheel.arm()
            return
        if wheel is None:
            return
        if action == "assign":
            role = cmd.get("role")
            code = cmd.get("code")
            if role in ROLES and code in ABS_CODES:
                wheel.mapping[role] = {"code": code,
                                       "invert": bool(cmd.get("invert", False))}
                log("assigned %s -> %s" % (role, code))
        elif action == "range":
            deg = int(cmd.get("deg", DEFAULT_RANGE_DEG))
            if deg in (270, 900):
                wheel.mapping["hw_range_deg"] = deg
                wheel.apply_range(deg)
        elif action == "centre":
            # Take the wheel's current position as dead ahead. Only meaningful
            # with hands off, so the UI says so.
            code = wheel.role_code("steer")
            if code is not None and code in wheel.axes:
                wheel.centre = wheel.raw[code]
                wheel.mapping["centre"] = wheel.centre
                log("centre captured at raw %d" % wheel.centre)
        elif action == "centre_reset":
            wheel.centre = None
            wheel.mapping.pop("centre", None)
            log("centre reset to the axis midpoint")
        elif action == "autocenter":
            pct = int(cmd.get("pct", DEFAULT_AUTOCENTER_PCT))
            wheel.apply_autocenter(pct)
            wheel.mapping["autocenter"] = wheel.autocenter_pct
        elif action == "assign_button":
            # Bind the last physically pressed button to a role. The UI arms,
            # the operator presses the paddle, the UI assigns — same shape as
            # the axis flow, and for the same reason: the HID descriptor gives
            # the buttons no semantics either, just "Button 1..18".
            role = cmd.get("role")
            if role in BUTTON_ROLES and wheel.last_button is not None:
                wheel.button_map[role] = wheel.last_button
                wheel.mapping["buttons"] = dict(wheel.button_map)
                log("assigned button role %s -> code 0x%03x" % (role, wheel.last_button))
        elif action == "clear_buttons":
            wheel.button_map = {}
            wheel.mapping.pop("buttons", None)
            log("button roles cleared")
        elif action == "save":
            data = load_user_map()
            entry = {r: wheel.mapping.get(r) for r in ROLES}
            entry["hw_range_deg"] = wheel.mapping.get("hw_range_deg",
                                                      wheel.range_deg)
            if wheel.centre is not None:
                entry["centre"] = wheel.centre
            entry["autocenter"] = wheel.autocenter_pct
            if wheel.button_map:
                entry["buttons"] = dict(wheel.button_map)
            data[wheel.name] = entry
            if save_user_map(data):
                log("saved mapping for '%s'" % wheel.name)

    threading.Thread(target=receiver_thread, args=(sock,), daemon=True).start()
    threading.Thread(target=sender_thread, args=(sock,), daemon=True).start()
    threading.Thread(target=housekeeping_thread,
                     args=(lambda: current["wheel"], do_command),
                     daemon=True).start()

    last_scan = 0.0
    try:
        while True:
            wheel = current["wheel"]

            if wheel is None:
                now = time.monotonic()
                if now - last_scan >= RESCAN_INTERVAL:
                    last_scan = now
                    current["wheel"] = open_wheel()
                    publish_wheel(current["wheel"])
                else:
                    time.sleep(0.05)
                continue

            try:
                ready, _, _ = select.select([wheel.fd], [], [], 0.25)
            except OSError:
                ready = []
            if ready and not wheel.pump():
                log("wheel '%s' disconnected" % wheel.name)
                wheel.close()
                current["wheel"] = None
                publish_wheel(None)
                continue
            publish_wheel(wheel)
    except KeyboardInterrupt:
        pass
    finally:
        RUNNING = False
        if current["wheel"]:
            current["wheel"].close()


# ── --dump ───────────────────────────────────────────────────────────────────
def dump():
    """
    Print every input device with absolute axes, then stream live axis values.

    This is step one of bringing up any new wheel: it tells you the exact
    device name to match on and which ABS_* code each control actually uses.
    Watch that gas and brake move *different* axes — if one axis moves for both,
    hid-logitech-hidpp has combined the pedals and they cannot be separated here.
    """
    devs = list(candidate_devices())
    if not devs:
        print("no devices with absolute axes found under /dev/input")
        return
    for path, event_name, fd, name, axes, writable in devs:
        rng = sysfs_range_path(event_name)
        print("\n%s  '%s'" % (path, name))
        print("  sysfs range: %s" % (rng or "(none — range not settable)"))
        print("  force feedback (autocenter): %s"
              % ("available" if writable and dev_has_ff(fd)
                 else ("NO — device reports no EV_FF" if writable
                       else "NO — device not writable")))
        print("  profile:     %s" % (profile_for(name) or "(no built-in match)"))
        for code in sorted(axes):
            a = axes[code]
            print("    %-14s min=%-7d max=%-7d flat=%-5d value=%d" %
                  (ABS_NAMES.get(code, "ABS_%d" % code),
                   a["min"], a["max"], a["flat"], a["value"]))

    print("\nLive values — move each control, Ctrl-C to stop.\n")
    wheels = [Wheel(p, e, fd, n, a, {}, w) for p, e, fd, n, a, w in devs]
    try:
        while True:
            fds = [w.fd for w in wheels]
            ready, _, _ = select.select(fds, [], [], 0.1)
            for w in wheels:
                if w.fd in ready:
                    w.pump()
            line = []
            for w in wheels:
                for code in sorted(w.axes):
                    if abs(w.norm[code]) > 0.02 or w.excursion[code] > 0.05:
                        line.append("%s=%+.2f" %
                                    (ABS_NAMES.get(code, "ABS_%d" % code),
                                     w.norm[code]))
            sys.stdout.write("\r%-100s" % "  ".join(line[:8]))
            sys.stdout.flush()
    except KeyboardInterrupt:
        print()
    finally:
        for w in wheels:
            w.close()


if __name__ == "__main__":
    if "--dump" in sys.argv:
        dump()
    else:
        main()
