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
     the wheel, maps its axes to steer/gas/brake, and sends a fixed 28-byte
     packet of *game* quantities (steering degrees, throttle, brake). The
     headset carries no per-device table at all. That is deliberate — adding a
     new wheel model is a 30-second `deploy.ps1` to one box, not an APK rebuild
     and a sideload to every headset.

  2. THE HEADSET SUBSCRIBES, THE BOX STREAMS. The headset sends an 8-byte ASUB
     keepalive to <gateway>:5010 every 500 ms; we stream to whatever source
     address that arrived from. No discovery, no handshake, no session state
     that a dropped packet could corrupt.

  3. FIXED-RATE ABSOLUTE SNAPSHOTS. We send the latest complete state every
     8.3 ms whether or not the wheel moved. A lost packet therefore costs 8.3 ms
     of staleness rather than sticking a stale value until the next physical
     movement — which is what an event-driven design would do on a link with no
     retransmission. 28 B x 120 Hz is ~3.4 KB/s, nothing next to the video.

  4. NO IPC SOCKETS. Status goes to /run/adiona/wheel.json, commands arrive via
     /run/adiona/wheel-cmd.json. adiona_controller.py reads/writes those files
     for the on-TV setup UI. Neither service can take the other down, and either
     can restart independently.

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
TX_HZ = int(CONF.get("WHEEL_TX_HZ", "120"))
DEFAULT_RANGE_DEG = int(CONF.get("WHEEL_DEFAULT_RANGE_DEG", "900"))
DEFAULT_AUTOCENTER_PCT = int(CONF.get("WHEEL_AUTOCENTER_PCT", "50"))

TX_INTERVAL = 1.0 / TX_HZ
INFO_EVERY = max(1, TX_HZ // 2)          # INFO at ~2 Hz
SUBSCRIBER_TIMEOUT = 3.0                 # drop the subscriber after this silence
RESCAN_INTERVAL = 1.0                    # look for a newly plugged wheel
HOUSEKEEP_HZ = 20                        # status write + command poll rate

STATUS_PATH = os.path.join(RUN_DIR, "wheel.json")
CMD_PATH = os.path.join(RUN_DIR, "wheel-cmd.json")
# Written by adiona-updater.py. Read-only here, and entirely optional: a box with
# no updater service just reports zero flags.
UPDATE_STATUS_PATH = os.path.join(RUN_DIR, "update.json")

# ── Wire format ──────────────────────────────────────────────────────────────
# STATE, 28 bytes, little-endian. A `reserved` u16 sits after `flags` purely so
# steer_deg lands on a 4-byte boundary; it is not optional padding, it is part
# of the format and must stay zero.
#   magic 'AW01' | seq u32 | flags u16 | reserved u16
#   steer_deg f32 | throttle u16 | brake u16 | buttons u32 | t_us i32
STATE_FMT = struct.Struct("<4sIHHfHHIi")
STATE_MAGIC = b"AW01"

# INFO, 56 bytes, ~2 Hz. Tells the headset the configured hardware range so it
# can derive its own full-lock angle (range / 2) without a device table.
#   magic 'AI01' | range_deg u16 | flags u16 | name 48s (NUL-padded)
# 48 bytes because "Logitech G920 Driving Force Racing Wheel" is 39 and a
# truncated name on the XR status line looks like a fault.
NAME_LEN = 48
INFO_FMT = struct.Struct("<4sHH%ds" % NAME_LEN)
INFO_MAGIC = b"AI01"

# BOX, 64 bytes, 1 Hz. What this box is running, so the headset can decide
# whether the wheel stream is usable and how to interpret it. Deliberately NOT
# folded into INFO: INFO's 56-byte layout is already parsed by shipped APKs, and
# widening it would be a breaking change needing a coordinated headset release.
#   magic 'AB01' | flags u16 | box_id u16
#   app_major u8 | app_minor u8 | app_patch u8 | os_id u8
#   os_major u16 | os_minor u16 | uptime_s u32
#   app_str 20s (NUL-padded) | os_str 24s (NUL-padded)
#
# The version lives in the magic, as with AW01/AI01 — there is no separate wire
# version field. The headset dispatches on SIZE first, so any field added here
# changes the size and is already a new dispatch case.
#
# PACKET SIZES 28 (STATE), 56 (INFO) and 64 (BOX) ARE TAKEN. Any future message
# must avoid all three, or the headset's size-then-magic dispatch will confuse it.
BOX_APP_LEN = 20
BOX_OS_LEN = 24
BOX_FMT = struct.Struct("<4sHHBBBBHHI%ds%ds" % (BOX_APP_LEN, BOX_OS_LEN))
BOX_MAGIC = b"AB01"

BOX_FLAG_UPDATE_AVAILABLE = 1 << 0
BOX_FLAG_UPDATE_IN_PROGRESS = 1 << 1
BOX_FLAG_JUST_UPDATED = 1 << 2       # applied within the last 10 minutes
BOX_FLAG_UPDATER_PRESENT = 1 << 3
BOX_FLAG_INTERNET_OK = 1 << 4

BOX_OS_UNKNOWN = 0
BOX_OS_RASPBIAN = 1
BOX_OS_DEBIAN = 2

# Subscribe keepalive from the headset.
SUB_FMT = struct.Struct("<4sI")
SUB_MAGIC = b"ASUB"

FLAG_WHEEL_PRESENT = 1 << 0
FLAG_PEDALS_MAPPED = 1 << 1
FLAG_RANGE_APPLIED = 1 << 2
FLAG_BUTTONS_MAPPED = 1 << 3   # at least one button role is bound on this wheel

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
    # No G29 entry on purpose. It is the same driver family, but its pedal axis
    # order has not been measured, and a WRONG profile is far worse than none:
    # an unknown wheel falls through to the on-TV mapping UI (press C on the
    # box), whereas a wrong one silently puts the throttle on the brake pedal.
    # Add it here only after confirming it the same way this one was.
]

ROLES = ("steer", "gas", "brake")


def profile_for(name):
    lowered = name.lower()
    for prof in WHEEL_PROFILES:
        if prof["name_match"] in lowered:
            return {r: dict(prof[r]) for r in ROLES} | {
                "hw_range_deg": prof.get("hw_range_deg", DEFAULT_RANGE_DEG)}
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


# ── Box report (AB01) ────────────────────────────────────────────────────────
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


def refresh_box_flags():
    """Fold adiona-updater.py's status file into the AB01 flag bits. An absent or
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
        uptime_seconds(),
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
    "axes": {},               # code -> {"value": norm, "excursion": float, "raw": int}
    "subscriber": None,
    "tx_hz": TX_HZ,
    "seq": 0,
}

RUNNING = True


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
        self.range_deg = (mapping or {}).get("hw_range_deg", DEFAULT_RANGE_DEG)
        self.range_applied = False
        # Raw steering value treated as dead ahead. None = use the axis midpoint.
        self.centre = (mapping or {}).get("centre")
        self.autocenter_pct = int((mapping or {}).get("autocenter", DEFAULT_AUTOCENTER_PCT))
        self.ff_available = writable

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
            log("no write access to %s — autocenter unavailable" % self.path)
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
            "(press C on the kiosk screen)." % (name, path))
    # Applied whether or not the axes are mapped: centring is a property of the
    # wheel, not of whether we know which axis is the throttle.
    wheel.apply_autocenter()
    return wheel


# ── Status + command files ───────────────────────────────────────────────────
def write_status():
    try:
        os.makedirs(RUN_DIR, exist_ok=True)
    except OSError:
        return
    with LOCK:
        body = json.dumps(STATE)
    tmp = STATUS_PATH + ".tmp"
    try:
        with open(tmp, "w") as fh:
            fh.write(body)
        os.replace(tmp, STATUS_PATH)
    except OSError:
        pass


def read_command(last_seq):
    """Return (seq, command dict) if a newer command is waiting, else (last_seq, None)."""
    try:
        with open(CMD_PATH) as fh:
            cmd = json.load(fh)
    except (OSError, ValueError):
        return last_seq, None
    seq = int(cmd.get("seq", 0))
    if seq <= last_seq:
        return last_seq, None
    return seq, cmd


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
        if len(data) < SUB_FMT.size or data[:4] != SUB_MAGIC:
            continue
        with LOCK:
            if STATE["subscriber"] != addr[0]:
                log("subscriber %s" % addr[0])
            STATE["subscriber"] = addr[0]
        SUBSCRIBER["addr"] = addr
        SUBSCRIBER["seen"] = time.monotonic()


SUBSCRIBER = {"addr": None, "seen": 0.0}


def sender_thread(sock):
    """Fixed-rate STATE at TX_HZ, INFO at ~2 Hz, BOX at 1 Hz. Never blocks on the
    wheel.

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
            steer = STATE["steer_deg"]
            throttle = int(round(STATE["throttle"] * 65535))
            brake = int(round(STATE["brake"] * 65535))
            buttons = STATE["buttons"]
            range_deg = STATE["range_deg"]
            name = STATE["name"]
            STATE["seq"] = seq

        t_us = int(time.monotonic_ns() // 1000) & 0x7FFFFFFF
        pkt = STATE_FMT.pack(STATE_MAGIC, seq, flags, 0, steer,
                             throttle, brake, buttons, t_us)
        try:
            sock.sendto(pkt, addr)
            if tick % INFO_EVERY == 0:
                sock.sendto(INFO_FMT.pack(INFO_MAGIC, range_deg, flags,
                                          name.encode("utf-8")[:NAME_LEN]), addr)
            # Offset by one tick so this never lands on the same tick as INFO
            # (INFO_EVERY divides TX_HZ), which would put three datagrams
            # back to back once a second.
            if tick % TX_HZ == 1:
                sock.sendto(box_packet(), addr)
        except OSError:
            pass                                # drop-tolerant by design


def housekeeping_thread(get_wheel, do_command):
    """Publish status and poll for setup commands from the controller."""
    last_seq = 0
    interval = 1.0 / HOUSEKEEP_HZ
    ticks = 0
    while RUNNING:
        write_status()
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
        time.sleep(interval)


# ── Main loop ────────────────────────────────────────────────────────────────
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


def main():
    global RUNNING

    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind(("0.0.0.0", WHEEL_PORT))
    log("listening on 0.0.0.0:%d, tx %d Hz" % (WHEEL_PORT, TX_HZ))
    read_box_info()
    log("box report: v%s on %s (id %04X)" %
        (BOX_INFO["app_str"] or "?", BOX_INFO["os_str"] or "?", BOX_INFO["box_id"]))

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
              % ("available" if writable else "NO — device not writable"))
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
