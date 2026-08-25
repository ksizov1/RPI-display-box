#!/usr/bin/env python3
"""
Adiona-TV USB keyboard bridge — the box's keyboard becomes the headset's keyboard.

Adiona-G already accepts a keyboard paired to the Quest over Bluetooth, and every
one of its shortcuts is a plain keycode check. So an operator running an event
needed TWO keyboards: one plugged into this box for the setup screens, and a
second one paired to the headset. This module reads the box's keyboard and hands
each keystroke to adiona-wheel.py, which carries it to the headset inside the
wheel packet it is already sending 90 times a second.

Design notes, in the order they matter:

  1. COMMANDS, NOT A KEY CHANNEL. The game uses the keyboard only to issue
     commands — open a menu, toggle a view, nudge a mirror — never to drive the
     car. So there is no held-key model on the wire and no state to resynchronise:
     each keystroke is a self-contained (key, modifiers, down/repeat/up) record.
     A lost record is a missed command, never a stuck key. The modifier keys
     themselves are never sent as keystrokes; they ride as flags on the keystroke
     they modify, which is the only form the game ever looks at.

  2. NO PACKET OF OUR OWN. The last four keystrokes ride in every AW02 wheel
     packet. That is why there is no send scheduling, no burst and no retransmit
     logic anywhere in this file: a keystroke stays in the ring until four newer
     ones push it out, so it is transmitted ~90 times before it could be missed,
     and the headset — which keeps only the newest packet of each drain — still
     sees every one of them.

  3. WE GRAB THE KEYBOARD, BUT ONLY WHEN THERE IS SOMEWHERE TO SEND IT.
     EVIOCGRAB takes the device away from cage and Chromium. That is required,
     not tidy: the game binds Ctrl+R, Ctrl+W, Ctrl+P and Ctrl+C, which to a kiosk
     browser mean reload, close-window, print and copy. But we grab ONLY while a
     headset is subscribed, so a box with no headset behaves exactly as it always
     has — including the Ctrl+Alt+F2 shell that 99-adiona-no-pointer.rules
     documents as the way in when the screen is wrong.

  4. F12 NEVER LEAVES THE BOX. It is filtered here, before a keystroke can enter
     the ring, so there is exactly one place that can get it wrong. It toggles
     the on-TV settings panel instead.

     Ctrl+S is the one key the box acts on WITHOUT keeping: it re-centres the USB
     sensor rig's steering here and is still forwarded, because the headset has
     its own centring to do. Two keys, two behaviours, both in _on_key().

  5. THE WIRE CARRIES GODOT KEYCODES, NOT EVDEV CODES. Same principle as the
     wheel's axis mapping: this box owns the device knowledge and the headset
     carries no translation table. See GODOT_KEYS below for the 16-bit encoding.

The evdev ioctl helpers below are deliberately DUPLICATED from adiona-wheel.py
rather than shared. adiona-wheel.py imports this module defensively, so that a
fault in the keyboard bridge cannot stop the wheel; moving shared helpers here
would give it a hard dependency on this file and undo exactly that.
"""

import fcntl
import json
import os
import select
import struct
import threading
import time
from collections import deque

# ── evdev, via ioctl ─────────────────────────────────────────────────────────
# Linux asm-generic ioctl encoding: dir << 30 | size << 16 | type << 8 | nr
_IOC_WRITE = 1
_IOC_READ = 2


def _ior(type_ch, nr, size):
    return (_IOC_READ << 30) | (size << 16) | (ord(type_ch) << 8) | nr


def _iow(type_ch, nr, size):
    return (_IOC_WRITE << 30) | (size << 16) | (ord(type_ch) << 8) | nr


def EVIOCGNAME(length):
    return _ior("E", 0x06, length)


def EVIOCGBIT(ev, length):
    return _ior("E", 0x20 + ev, length)


# Take the device exclusively. Tied to the open file description, which is the
# property that makes this safe to deploy: if this process dies or is killed,
# the kernel releases the grab and the keyboard is Chromium's again.
EVIOCGRAB = _iow("E", 0x90, 4)

EV_SYN, EV_KEY, EV_ABS = 0x00, 0x01, 0x03
EV_MAX = 0x1F
KEY_MAX = 0x2FF
ABS_X = 0x00
ABS_MT_SLOT = 0x2F                        # presence => touchscreen, not a keyboard

# struct input_event on 64-bit: timeval{s64,s64}, u16 type, u16 code, s32 value
INPUT_EVENT = struct.Struct("=qqHHi")

# EV_KEY values.
KEY_UP_VALUE, KEY_DOWN_VALUE, KEY_REPEAT_VALUE = 0, 1, 2

# ── evdev key codes we care about ────────────────────────────────────────────
# evdev numbers keys by physical position, not alphabetically, so these cannot be
# derived and are simply listed. Names match linux/input-event-codes.h.
KEY_ESC = 1
KEY_BACKSPACE, KEY_TAB = 14, 15
KEY_ENTER = 28
KEY_LEFTCTRL, KEY_LEFTSHIFT, KEY_RIGHTSHIFT = 29, 42, 54
KEY_LEFTALT, KEY_SPACE = 56, 57
KEY_F1, KEY_F11, KEY_F12 = 59, 87, 88
KEY_KPENTER, KEY_RIGHTCTRL, KEY_RIGHTALT = 96, 97, 100
KEY_HOME, KEY_UP, KEY_PAGEUP = 102, 103, 104
KEY_LEFT, KEY_RIGHT = 105, 106
KEY_END, KEY_DOWN, KEY_PAGEDOWN = 107, 108, 109
KEY_INSERT, KEY_DELETE = 110, 111
KEY_LEFTMETA, KEY_RIGHTMETA = 125, 126
KEY_A, KEY_S, KEY_Z = 30, 31, 44

# ── The wire's key encoding ──────────────────────────────────────────────────
# Godot's Key enum is two disjoint ranges: printable keys ARE their uppercase
# ASCII value (KEY_A == 65, KEY_SPACE == 32), and everything else is
# KEY_SPECIAL (0x400000) plus a small ordinal. Sending the full 32-bit value
# would cost 2 extra bytes per slot in a packet that has to stay under 56, so a
# special key is sent as 0x8000 | ordinal and a printable one as itself. The
# headset undoes it in two lines; see LanKeyboard.gd.
SPECIAL = 0x8000


def _sp(ordinal):
    return SPECIAL | ordinal


# evdev code -> wire code. This is the whole set of keys that can reach the
# headset; anything absent is dropped here rather than on the headset, so adding
# a key later is a box-only change. F12 is deliberately NOT in this table — see
# design note 4 — and neither are the modifier keys, which are only ever sent as
# flags on another keystroke.
GODOT_KEYS = {
    KEY_ESC: _sp(0x01), KEY_TAB: _sp(0x02), KEY_BACKSPACE: _sp(0x04),
    KEY_ENTER: _sp(0x05), KEY_KPENTER: _sp(0x06),
    KEY_INSERT: _sp(0x07), KEY_DELETE: _sp(0x08),
    KEY_HOME: _sp(0x0D), KEY_END: _sp(0x0E),
    KEY_LEFT: _sp(0x0F), KEY_UP: _sp(0x10),
    KEY_RIGHT: _sp(0x11), KEY_DOWN: _sp(0x12),
    KEY_PAGEUP: _sp(0x13), KEY_PAGEDOWN: _sp(0x14),
    KEY_SPACE: ord(" "),
}

# F1..F11 are contiguous in both encodings (evdev 59.., Godot special 0x1C..).
# F12 is evdev 88, and its absence from this loop is the point.
for _i in range(10):
    GODOT_KEYS[KEY_F1 + _i] = _sp(0x1C + _i)      # F1..F10
GODOT_KEYS[KEY_F11] = _sp(0x1C + 10)

# Letters, digits and the printable punctuation, by physical evdev position.
for _code, _ch in (
        (2, "1"), (3, "2"), (4, "3"), (5, "4"), (6, "5"),
        (7, "6"), (8, "7"), (9, "8"), (10, "9"), (11, "0"),
        (12, "-"), (13, "="),
        (16, "Q"), (17, "W"), (18, "E"), (19, "R"), (20, "T"),
        (21, "Y"), (22, "U"), (23, "I"), (24, "O"), (25, "P"),
        (26, "["), (27, "]"),
        (30, "A"), (31, "S"), (32, "D"), (33, "F"), (34, "G"),
        (35, "H"), (36, "J"), (37, "K"), (38, "L"),
        (39, ";"), (40, "'"), (41, "`"), (43, "\\"),
        (44, "Z"), (45, "X"), (46, "C"), (47, "V"),
        (48, "B"), (49, "N"), (50, "M"),
        (51, ","), (52, "."), (53, "/")):
    GODOT_KEYS[_code] = ord(_ch)

# Modifier bits carried with every keystroke. Must match LanKeyboard.gd.
MOD_SHIFT = 1 << 0
MOD_CTRL = 1 << 1
MOD_ALT = 1 << 2
MOD_META = 1 << 3

MODIFIER_BITS = {
    KEY_LEFTSHIFT: MOD_SHIFT, KEY_RIGHTSHIFT: MOD_SHIFT,
    KEY_LEFTCTRL: MOD_CTRL, KEY_RIGHTCTRL: MOD_CTRL,
    KEY_LEFTALT: MOD_ALT, KEY_RIGHTALT: MOD_ALT,
    KEY_LEFTMETA: MOD_META, KEY_RIGHTMETA: MOD_META,
}

# Ring depth. Four covers ~400 ms of auto-repeat and far more than anyone can
# type between two of the headset's frames; the packet has room for no more.
RING_SLOTS = 4

# Actions, as they go on the wire.
ACTION_UP, ACTION_DOWN, ACTION_REPEAT = 0, 1, 2

RESCAN_INTERVAL = 1.0        # look for a newly plugged keyboard
GATE_INTERVAL = 0.2          # re-evaluate whether to hold the grab
UPDATE_STATES_INTERACTIVE = ("prompting", "ok", "failed")


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


def is_keyboard(fd):
    """
    True for a device that can type.

    Requires the letters and Enter, which excludes the many devices that carry a
    few EV_KEY codes without being keyboards: the wheel (BTN_* only), the Pi's
    gpio-keys power button, and a mouse's buttons. Rejects anything with absolute
    axes so the wheel cannot be grabbed even if a future model reports letters,
    and so a touchscreen is left alone.
    """
    ev_mask = bytearray(EV_MAX // 8 + 1)
    try:
        fcntl.ioctl(fd, EVIOCGBIT(0, len(ev_mask)), ev_mask)
    except OSError:
        return False
    if not _bitmask_has(ev_mask, EV_KEY):
        return False
    if _bitmask_has(ev_mask, EV_ABS):
        abs_mask = bytearray((ABS_MT_SLOT // 8) + 1)
        try:
            fcntl.ioctl(fd, EVIOCGBIT(EV_ABS, len(abs_mask)), abs_mask)
        except OSError:
            return False
        if _bitmask_has(abs_mask, ABS_X) or _bitmask_has(abs_mask, ABS_MT_SLOT):
            return False

    key_mask = bytearray(KEY_MAX // 8 + 1)
    try:
        fcntl.ioctl(fd, EVIOCGBIT(EV_KEY, len(key_mask)), key_mask)
    except OSError:
        return False
    return all(_bitmask_has(key_mask, c) for c in (KEY_A, KEY_Z, KEY_ENTER))


class Keyboard(object):
    """One open keyboard device."""

    def __init__(self, path, fd, name):
        self.path = path
        self.fd = fd
        self.name = name
        self.grabbed = False

    def set_grab(self, on):
        """Take or release exclusive access. Returns True if the state changed."""
        if on == self.grabbed:
            return False
        try:
            fcntl.ioctl(self.fd, EVIOCGRAB, 1 if on else 0)
        except OSError:
            return False
        self.grabbed = on
        return True

    def close(self):
        try:
            self.set_grab(False)
        except OSError:
            pass
        try:
            os.close(self.fd)
        except OSError:
            pass


class KeyReader(object):
    """
    Reads every keyboard on the box, filters, and publishes a keystroke ring.

    Owns no socket and no timers of its own: adiona-wheel.py calls snapshot()
    while packing each wheel packet.
    """

    def __init__(self, log, repeat_hz=10, enabled=True, update_status_path=None,
                 subscriber_live=None, on_recentre=None):
        self._log = log
        self._enabled = enabled
        # Ctrl+S, tapped in passing. See _on_key().
        self._on_recentre = on_recentre or (lambda: None)
        # Kernel auto-repeat is ~33 Hz, which is far too fast for keys that step
        # a value: a held Shift+Left would slew a mirror across its whole travel
        # in a blink, and a held Tab would toggle the game's debug display 33
        # times a second. Repeats below this rate are simply dropped.
        self._repeat_interval = 1.0 / max(1, repeat_hz)
        self._update_status_path = update_status_path
        self._subscriber_live = subscriber_live or (lambda: False)

        self._lock = threading.Lock()
        self._ring = deque(maxlen=RING_SLOTS)
        self._key_seq = 0

        self._devices = {}            # path -> Keyboard
        self._held = set()            # evdev codes physically down, all devices
        self._mods = 0
        self._last_repeat = {}        # evdev code -> monotonic of last emitted repeat
        self._last_scan = 0.0
        self._last_gate = 0.0
        self._want_grab = False
        self._forwarding = False

        # Panel state. panel_seq is the only thing the kiosk page follows: it
        # counts F12 presses and the page toggles on its parity. The page cannot
        # be told "open" directly, because when we are ungrabbed Chromium sees
        # the same F12 we do and would toggle a second time.
        self._panel_seq = 0
        self._panel_open = False
        self._panel_tab = "wifi"

    # ── Published state ──────────────────────────────────────────────────────
    def snapshot(self):
        """(key_seq, [(code, mods, action), ...]) oldest first, at most RING_SLOTS."""
        with self._lock:
            return self._key_seq, list(self._ring)

    def status(self):
        with self._lock:
            return {
                "present": bool(self._devices),
                "devices": sorted(d.name for d in self._devices.values()),
                "forwarding": self._forwarding,
                "panel_seq": self._panel_seq,
                "panel_open": self._panel_open,
                "tab": self._panel_tab,
                "key_seq": self._key_seq,
            }

    def present(self):
        with self._lock:
            return bool(self._devices)

    def set_panel(self, is_open, tab=None):
        """Adopt the kiosk page's view of the panel. The page is authoritative —
        it also closes on Esc and on a mouse click, neither of which we can see
        once we have released the grab."""
        with self._lock:
            self._panel_open = bool(is_open)
            if tab in ("wifi", "wheel", "sensors"):
                self._panel_tab = tab

    # ── Main loop ────────────────────────────────────────────────────────────
    def run(self):
        """Thread entry point. Never returns, never propagates."""
        while True:
            try:
                self._tick()
            except Exception as e:                  # a fault here must not stop
                self._log("keyboard: %s" % e)       # the wheel service
                time.sleep(0.5)

    def _tick(self):
        now = time.monotonic()

        if now - self._last_scan >= RESCAN_INTERVAL:
            self._last_scan = now
            self._scan()
        if now - self._last_gate >= GATE_INTERVAL:
            self._last_gate = now
            self._update_gate()

        fds = [d.fd for d in self._devices.values()]
        if not fds:
            time.sleep(0.1)
            return
        try:
            ready, _, _ = select.select(fds, [], [], 0.1)
        except OSError:
            # One of the fds is gone. select() returns instantly on a dead
            # descriptor, so without both of these this loop spins at full tilt
            # until the next scan reaps it.
            self._last_scan = 0.0
            time.sleep(0.05)
            return
        for dev in list(self._devices.values()):
            if dev.fd in ready:
                self._drain(dev)

    def _scan(self):
        """Open newly plugged keyboards, drop unplugged ones."""
        try:
            names = sorted(n for n in os.listdir("/dev/input")
                           if n.startswith("event"))
        except OSError:
            return
        seen = set()
        for name in names:
            path = os.path.join("/dev/input", name)
            seen.add(path)
            if path in self._devices:
                continue
            try:
                fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK)
            except OSError:
                continue
            if not is_keyboard(fd):
                os.close(fd)
                continue
            dev = Keyboard(path, fd, dev_name(fd))
            with self._lock:
                self._devices[path] = dev
            self._log("keyboard '%s' on %s" % (dev.name, path))
            # A keyboard plugged in mid-session must join the current regime.
            if self._want_grab:
                dev.set_grab(True)

        for path in [p for p in self._devices if p not in seen]:
            with self._lock:
                dev = self._devices.pop(path)
            self._log("keyboard '%s' removed" % dev.name)
            dev.close()
            # Anything it was holding went with it, including its modifiers.
            # Held keys are not tracked per device — two keyboards holding the
            # same code is not a case worth the bookkeeping, and the only cost of
            # clearing everything is that a modifier held on a *second* keyboard
            # is forgotten. Nothing sticks, which is the property that matters.
            self._held.clear()
            self._mods = 0
            self._last_repeat.clear()

    def _update_gate(self):
        """
        Decide whether to hold the keyboard exclusively.

            grab = a keyboard exists
                   AND a headset is subscribed      (somewhere to send to)
                   AND the settings panel is closed (the page needs real keys)
                   AND the updater is not asking a question

        The panel and the update prompt are answered by Chromium, which cannot
        see a single keystroke while we hold the grab. The subscriber term is
        what keeps a box with no headset behaving exactly as it always has.
        """
        with self._lock:
            panel_open = self._panel_open
            have_devices = bool(self._devices)
        want = (self._enabled and have_devices and not panel_open
                and self._subscriber_live() and not self._update_interactive())

        # Never change the grab while a key is physically down: the side about to
        # lose the device would never see that key's release and would hold it
        # forever. F12 is itself held at the moment the panel opens, so this is
        # the normal case, not a corner one — the transition simply happens a few
        # milliseconds later, when the key comes back up.
        if want != self._want_grab and not self._held:
            self._want_grab = want
            for dev in list(self._devices.values()):
                if dev.set_grab(want):
                    self._log("keyboard '%s' %s" %
                              (dev.name, "grabbed" if want else "released"))

        # Forward only while we actually hold the device. Tying this to the grab
        # rather than to `want` is what stops a keystroke reaching Chromium and
        # the headset at the same time during a deferred transition.
        with self._lock:
            self._forwarding = want and self._want_grab

    def _update_interactive(self):
        """True while the updater has a question or a result banner on the TV.

        Only those states: ungrabbing for the whole apply/health window would
        leave the headset without a keyboard for two minutes for no reason."""
        if not self._update_status_path:
            return False
        try:
            with open(self._update_status_path) as fh:
                return json.load(fh).get("state", "") in UPDATE_STATES_INTERACTIVE
        except (OSError, ValueError):
            return False

    # ── Event handling ───────────────────────────────────────────────────────
    def _drain(self, dev):
        while True:
            try:
                data = os.read(dev.fd, INPUT_EVENT.size * 64)
            except BlockingIOError:
                return
            except OSError:
                # Unplugged mid-read. Rescan at once rather than reading a dead
                # fd for up to a second — select() keeps calling it readable.
                self._last_scan = 0.0
                return
            if not data:
                return
            for off in range(0, len(data) - INPUT_EVENT.size + 1,
                             INPUT_EVENT.size):
                _s, _us, etype, code, value = INPUT_EVENT.unpack_from(data, off)
                if etype == EV_KEY:
                    self._on_key(code, value)
            if len(data) < INPUT_EVENT.size * 64:
                return

    def _on_key(self, code, value):
        # Track what is physically down first: the grab gate and the modifier
        # flags both depend on it, and both must stay right for keys we do not
        # forward at all.
        if value == KEY_DOWN_VALUE:
            self._held.add(code)
        elif value == KEY_UP_VALUE:
            self._held.discard(code)
            self._last_repeat.pop(code, None)

        bit = MODIFIER_BITS.get(code)
        if bit is not None:
            if value == KEY_DOWN_VALUE:
                self._mods |= bit
            elif value == KEY_UP_VALUE:
                self._mods &= ~bit
            return                              # never a keystroke of its own

        if code == KEY_F12:
            if value == KEY_DOWN_VALUE:
                self._toggle_panel()
            return                              # never reaches the headset

        # Ctrl+S: re-centre the steering. THE ONE NON-CONSUMING TAP IN THIS FILE,
        # and the difference from F12 above is the whole point — F12 returns, so
        # the headset never sees it, while this falls through and is forwarded as
        # usual. Both ends act on it: the box zeroes the USB sensor rig's turn
        # count at the source (a racing wheel has none to lose, so the callback
        # is a no-op then), and the headset re-captures its steering centre as it
        # always has. They cannot disagree, because read_roles() and the
        # keystroke ring are packed from the same snapshot: the reset and the
        # keystroke that caused it ride in the SAME packet, so the headset sees a
        # steer_deg already at zero and captures an offset of ~0.
        #
        # Down only. On auto-repeat a held Ctrl+S would re-zero the wheel
        # KEYS_REPEAT_HZ times a second, under the driver's hands.
        if code == KEY_S and value == KEY_DOWN_VALUE and (self._mods & MOD_CTRL):
            try:
                self._on_recentre()
            except Exception as e:              # never cost anyone a keystroke
                self._log("recentre: %s" % e)

        if not self._forwarding:
            return

        wire = GODOT_KEYS.get(code)
        if wire is None:
            return

        if value == KEY_REPEAT_VALUE:
            now = time.monotonic()
            if now - self._last_repeat.get(code, 0.0) < self._repeat_interval:
                return
            self._last_repeat[code] = now
            action = ACTION_REPEAT
        elif value == KEY_DOWN_VALUE:
            action = ACTION_DOWN
        else:
            action = ACTION_UP

        with self._lock:
            self._key_seq = (self._key_seq + 1) & 0xFFFF
            self._ring.append((wire, self._mods, action))

    def _toggle_panel(self):
        """F12: bump the counter the kiosk page follows, and predict the result.

        The prediction matters: the page's answer takes a round trip through the
        controller, and until it arrives the grab gate would otherwise still
        think the panel is closed and forward the next keystrokes to the headset.
        set_panel() overwrites this the moment the page reports in."""
        with self._lock:
            self._panel_seq = (self._panel_seq + 1) & 0xFFFFFFFF
            self._panel_open = not self._panel_open
        self._last_gate = 0.0                   # re-evaluate the grab now

    def close(self):
        for dev in list(self._devices.values()):
            dev.close()
        self._devices.clear()
