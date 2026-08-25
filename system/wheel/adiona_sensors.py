#!/usr/bin/env python3
"""
Adiona-TV USB vehicle sensor bridge — a Drive Square sensor rig becomes a wheel.

Adiona-G already supports Drive Square BLE sensor boxes: small boxes clamped to a
real car's steering wheel and pedals that turn the actual controls into sim
inputs. A new generation of the same hardware attaches over USB instead, and the
Quest cannot take USB peripherals in the field — so, exactly like the racing
wheel, they plug into this box and their readings are streamed to the headset.

Design notes, in the order they matter:

  1. IT IS A WHEEL, AS FAR AS THE WIRE IS CONCERNED. This module produces
     (steer_deg, throttle, brake) — the same triple as Wheel.read_roles() — and
     adiona-wheel.py packs it into the same AW02 packet. The rig announces itself
     as DEVICE_TYPE_SENSORS in AI02 and nothing else differs, so the headset needs
     no new code path and every existing consumer works unchanged.

  2. THE MOUNTING IS MEASURED, NOT DECREED. The BLE steering box had to be bolted
     in a known orientation, because a bare accelerometer reading can only be
     interpreted against an assumed mounting: it read the gravity vector's angle
     in the box's X-Z plane and called that steering. These boxes send a full
     Kalman-fused quaternion, which carries enough information to DISCOVER the
     axis a control turns about, from about 30 degrees of any motion. So the
     operator just moves the controls and the box works out the rest.

  2a. NOTHING MEASURES A LIMIT. The angle comes out of the quaternion directly,
     so there is nothing to learn from hauling a wheel against its stops — and
     plenty to lose, since an operator who stops short sets a limit that is
     silently too small for the rest of the event. Steering travel is declared
     in box.conf; a pedal's is an ENVELOPE that only ever grows while the
     calibration is open, so a first half-hearted press costs nothing.

  3. THE ANGLE IS ABSOLUTE, THE TURN COUNT IS NOT. Each sample's angle is derived
     from the sensor's own attitude relative to a stored centre, so we add no
     integration drift of our own. The only integrated state in this file is the
     turn count, which is what makes >360 degrees of travel representable at all,
     and it is also the only thing that can go wrong — see recentre().

  4. NO pyserial. The boxes enumerate as USB CDC-ACM, which is a tty, so
     os.open() + termios is the whole driver. Same stdlib-only rule as the rest
     of the image: nothing here needs a pip install.

  5. A FAULT HERE COSTS THE SENSORS AND NOTHING ELSE. adiona-wheel.py imports this
     module defensively, exactly as it does adiona_keys.py, so a racing wheel
     plugged into the same box keeps working whatever happens in this file.

Run `adiona-wheel.py --dump` with a rig attached to see every port, its role, its
rate and its live quaternion.
"""

import json
import math
import os
import re
import select
import threading
import time

# POSIX-only, and this module only ever opens a tty on the box itself. Imported
# softly so the maths above stays importable from a Windows checkout — the same
# reason adiona_controller.py and adiona-wheel.py carry dev fallbacks for their
# /etc and /opt paths. With no termios there are no ports to open either.
try:
    import termios
except ImportError:                                # pragma: no cover - not Linux
    termios = None

# ── Device discovery ─────────────────────────────────────────────────────────
# udev builds these from the USB descriptors, so they carry the role AND a
# per-unit serial:
#   usb-Drive_Square_D2_Steering_Sensor_7519AEA050304B4B542E3120FF17110E-if00
# Measured on the hardware: VID 1b4f, PIDs 9d2f/9d3f/9d4f, all cdc_acm. Matching
# on the NAME rather than the product id is deliberate — it is the rule the
# hardware is specified by ("the name contains Steering/Gas/Brake"), and a new
# revision with a new pid will keep working without a change here.
# Overridable only so tools/test-sensor-loopback.py can point the whole discovery
# path at a directory of ptys and exercise a real calibration without a rig on
# the desk. Same convention as every other ADIONA_* path in this repo.
BY_ID_DIR = os.environ.get("ADIONA_SENSOR_BY_ID", "/dev/serial/by-id")
DEVICE_MATCH = "drive_square"

# Role detection, checked in this order. EVO first because it is the shortest
# token and the least likely to appear by accident in a longer one.
ROLE_MATCH = (
    ("evo", "evo"),
    ("steer", "steering"),
    ("gas", "gas"),
    ("brake", "brake"),
)
ROLES = ("steer", "gas", "brake", "evo")
PEDAL_ROLES = ("gas", "brake")

# The rig counts as present with these attached. Brake is optional — a two-pedal
# rig is a legitimate setup and the third box simply reads zero.
REQUIRED_ROLES = ("steer", "gas")

# ── Wire format of the sensor stream ─────────────────────────────────────────
# "<t_ms>, <qw>, <qx>, <qy>, <qz>\r\n", e.g.
#   17126192, 0.9993, -0.0017, -0.0263, 0.0265
# Measured rates differ per box and are not guaranteed: steering 67 Hz, gas and
# brake 33 Hz. Nothing here assumes a rate; every calculation is per-sample.
LINE_FIELDS = 5
# A line that never terminates means the port is emitting something that is not
# this protocol. Drop the buffer rather than growing it without limit.
MAX_LINE_BYTES = 4096

# ── Tuning ───────────────────────────────────────────────────────────────────
# Motion below this per-sample angle is treated as noise during axis discovery.
# The boxes report 4 decimal places and sit within ~0.02 degrees of themselves at
# rest, so this is comfortably above the floor while still admitting a slow,
# careful sweep.
NOISE_FLOOR_DEG = 0.05

# Minimum accumulated |sum of sin(theta/2) . axis| for an axis estimate to be
# believed. 0.05 is about 6 degrees of total swept angle: enough to reject "the
# operator pressed Enter twice without touching anything", far below any real
# sweep (a 900 degree wheel accumulates ~7.9).
AXIS_MIN_NORM = 0.05

# Sanity floor for a pedal: below this the "press" was not a press.
MIN_PEDAL_TRAVEL_DEG = 5.0

# How much turning is needed before an axis is believed. A quaternion makes this
# small — 30 degrees of motion about a hinge pins the hinge down — and it must
# stay small: a motorcycle's bars, a kart wheel or a forklift tiller may not
# offer a full turn, and a calibration must never demand travel the vehicle does
# not have. More sweep is still better, and AXIS_REFINE_SEC keeps using it.
MIN_AXIS_SWEEP_DEG = 30.0

# How far a control must get from its zero before the direction it moved in is
# taken as the positive one — right for steering, pressed for a pedal.
#
# PER ROLE, and the difference is not cosmetic. A steering wheel is turned
# deliberately through tens of degrees, so 10 is both unambiguous and reached
# instantly. A PEDAL BOX MAY ONLY ROTATE 8-12 DEGREES IN TOTAL: measured on real
# hardware, a gas box swept 942 degrees of pumping without its angle ever
# reaching 10, so the sign never resolved, the travel envelope never opened, and
# the row sat on "measuring" for ever with no clue why. Both figures are still
# hundreds of times the sensors' ~0.02 degrees of rest noise.
SIGN_DEG = {"steer": 10.0, "evo": 10.0, "gas": 3.0, "brake": 3.0}
SIGN_DEG_DEFAULT = 3.0

# Below this, the motion was not about ONE axis: the increments pointed all over
# and their vector sum largely cancelled. In practice it means a box being waved
# by hand rather than turning on a mount, and it is worth saying out loud —
# without it a poorly-mounted sensor produces a plausible-looking axis that
# nothing measured against will ever move.
MIN_COHERENCE = 0.80

# How often the axis is re-derived from everything seen so far while a sweep is
# still running. Cheap (one normalise), and it is what lets a long sweep produce
# a better axis than the 30 degrees that first satisfied MIN_AXIS_SWEEP_DEG.
AXIS_REFINE_SEC = 0.5

RESCAN_INTERVAL = 1.0        # look for a newly plugged sensor box
SELECT_TIMEOUT = 0.05


# ── Quaternion maths ─────────────────────────────────────────────────────────
# Quaternions are plain (w, x, y, z) tuples. No numpy on the image, and none
# needed: everything below is a handful of multiplies.

def q_conj(q):
    return (q[0], -q[1], -q[2], -q[3])


def q_mul(a, b):
    aw, ax, ay, az = a
    bw, bx, by, bz = b
    return (
        aw * bw - ax * bx - ay * by - az * bz,
        aw * bx + ax * bw + ay * bz - az * by,
        aw * by - ax * bz + ay * bw + az * bx,
        aw * bz + ax * by - ay * bx + az * bw,
    )


def q_canon(q):
    """Flip to the w >= 0 hemisphere.

    q and -q are the SAME rotation, and every angle extracted below is only
    single-valued once a hemisphere is chosen. Doing it here is what keeps
    twist_deg() inside (-180, 180] instead of (-360, 360]."""
    return (-q[0], -q[1], -q[2], -q[3]) if q[0] < 0.0 else q


def q_angle_deg(q):
    """The rotation angle of q, 0..180 degrees, ignoring its axis."""
    q = q_canon(q)
    v = math.sqrt(q[1] * q[1] + q[2] * q[2] + q[3] * q[3])
    return 2.0 * math.degrees(math.atan2(v, q[0]))


def swept_deg(a, b):
    """How far the sensor turned between two samples, whatever the direction.

    Path length in rotation space. Deliberately UNSIGNED and axis-free: it is
    what the calibration watches to decide the operator has moved enough, and at
    that moment there is no axis yet to measure a signed angle against.
    """
    return q_angle_deg(q_mul(q_conj(a), b))


def twist_deg(q0, q, axis):
    """
    Signed rotation of q relative to q0 about `axis`, in (-180, 180].

    The swing-twist decomposition, reduced to the one component we want: for a
    control constrained to a hinge the swing is zero by construction, so the
    twist is the whole story and there is no need to build the full split.

    This is the generalisation of what the BLE boxes did with atan2(-fx, fz) —
    same idea, but the axis is the measured one instead of one the mounting was
    required to provide.
    """
    r = q_canon(q_mul(q_conj(q0), q))
    dot = r[1] * axis[0] + r[2] * axis[1] + r[3] * axis[2]
    return 2.0 * math.degrees(math.atan2(dot, r[0]))


def wrap180(deg):
    """Fold an angle difference into (-180, 180]."""
    deg = math.fmod(deg + 180.0, 360.0)
    if deg <= 0.0:
        deg += 360.0
    return deg - 180.0


def discover_axis(samples):
    """
    The axis a control turns about, in the SENSOR'S OWN frame, from a sweep.

    THE REFERENCE IMPLEMENTATION, not the one that runs on the box: Collector
    does this incrementally, over a stream, so a sweep can run as long as the
    operator likes without storing a sample. This batch version states the maths
    in one readable piece, and tools/test-sensor-math.py checks the two against
    each other on the same data so they cannot drift apart.

    Between consecutive samples the relative rotation conj(q[i-1]) * q[i] has a
    vector part equal to sin(theta/2) * axis. For a control on a fixed hinge that
    axis is constant, so summing those vector parts is a mean weighted by how far
    the control moved: deliberate motion dominates and sensor noise averages out.

    The sign of each increment is meaningless on its own — turning back the other
    way negates it — so each is folded onto the same side as the first
    significant one before being added.

    Returns a unit 3-tuple, or None if the control never actually moved.
    """
    ref = None
    sx = sy = sz = 0.0
    prev = None
    for q in samples:
        if prev is None:
            prev = q
            continue
        dq = q_canon(q_mul(q_conj(prev), q))
        prev = q
        vx, vy, vz = dq[1], dq[2], dq[3]
        n = math.sqrt(vx * vx + vy * vy + vz * vz)
        if 2.0 * math.degrees(math.atan2(n, dq[0])) < NOISE_FLOOR_DEG:
            continue
        if ref is None:
            ref = (vx, vy, vz)
        elif vx * ref[0] + vy * ref[1] + vz * ref[2] < 0.0:
            vx, vy, vz = -vx, -vy, -vz
        sx += vx
        sy += vy
        sz += vz
    n = math.sqrt(sx * sx + sy * sy + sz * sz)
    if n < AXIS_MIN_NORM:
        return None
    return (sx / n, sy / n, sz / n)


def replay(samples, q0, axis):
    """
    Unwrap a recorded sweep into a continuous multi-turn angle series.

    Returns (thetas, prev_wrapped, total) — the tail values being exactly what a
    live tracker needs to carry straight on from where the sweep ended.
    """
    thetas = []
    prev = None
    total = 0.0
    for q in samples:
        w = twist_deg(q0, q, axis)
        if prev is None:
            total = w
        else:
            total += wrap180(w - prev)
        prev = w
        thetas.append(total)
    return thetas, prev, total


# ── One sensor box ───────────────────────────────────────────────────────────
_SERIAL_RE = re.compile(r"_([0-9A-Fa-f]{8,})-if\d+$")


def parse_by_id(basename):
    """(role, serial, pretty_name) for a by-id entry, or None if not ours."""
    low = basename.lower()
    if DEVICE_MATCH not in low:
        return None
    role = None
    for name, token in ROLE_MATCH:
        if token in low:
            role = name
            break
    if role is None:
        return None
    m = _SERIAL_RE.search(basename)
    serial = m.group(1) if m else basename
    # "usb-Drive_Square_D2_Steering_Sensor_7519AE...-if00" -> the human half.
    pretty = basename
    if pretty.startswith("usb-"):
        pretty = pretty[4:]
    pretty = _SERIAL_RE.sub("", pretty).replace("_", " ").strip()
    return role, serial, pretty or basename


class SensorPort(object):
    """One open /dev/ttyACM*, parsing quaternion lines."""

    def __init__(self, link, dev, role, serial, name, fd):
        self.link = link          # the stable /dev/serial/by-id path
        self.dev = dev            # what it resolved to, for the log and --dump
        self.role = role
        self.serial = serial
        self.name = name
        self.fd = fd
        self.buf = b""
        self.q = None             # last good quaternion
        self.t_ms = 0             # the box's own timestamp, milliseconds
        self.last_rx = 0.0        # monotonic, for staleness
        self.samples = 0
        self.rate_at = 0.0
        self.rate_n = 0
        self.hz = 0.0

    def fresh(self, now, stale_sec):
        return self.last_rx > 0.0 and (now - self.last_rx) <= stale_sec

    def close(self):
        try:
            os.close(self.fd)
        except OSError:
            pass


def open_port(link):
    """Open a by-id link raw, or return None. Never raises."""
    if termios is None:
        return None
    parsed = parse_by_id(os.path.basename(link))
    if parsed is None:
        return None
    role, serial, name = parsed
    try:
        dev = os.path.realpath(link)
    except OSError:
        dev = link
    fd = None
    for mode in (os.O_RDWR, os.O_RDONLY):
        try:
            fd = os.open(link, mode | os.O_NOCTTY | os.O_NONBLOCK)
            break
        except OSError:
            fd = None
    if fd is None:
        return None
    try:
        attr = termios.tcgetattr(fd)
        # Raw: no input processing (so \r is not translated), no output
        # processing, no canonical mode, no echo. CLOCAL because there is no
        # modem here and we must not block waiting for carrier. The baud rate is
        # nominal — CDC-ACM carries no UART on the far side and ignores it — but
        # it still has to be a legal value.
        attr[0] = 0
        attr[1] = 0
        attr[2] = termios.CS8 | termios.CREAD | termios.CLOCAL
        attr[3] = 0
        attr[4] = termios.B115200
        attr[5] = termios.B115200
        attr[6][termios.VMIN] = 0
        attr[6][termios.VTIME] = 0
        termios.tcsetattr(fd, termios.TCSANOW, attr)
        termios.tcflush(fd, termios.TCIFLUSH)
    except (termios.error, OSError):
        os.close(fd)
        return None
    return SensorPort(link, dev, role, serial, name, fd)


# ── Calibration ──────────────────────────────────────────────────────────────
# The guided flows. Held here rather than in the page because this box owns all
# device knowledge — the same rule that makes supporting a new wheel a deploy
# instead of an APK rebuild. Adding a role later is a change to this table.
# ONE CALIBRATION FOR THE WHOLE RIG, AND TWO KEYPRESSES.
#
# Not three separate procedures: everything is zeroed together in one pose, then
# everything is swept together in one go, and the box says which sensors it has
# enough on as they qualify. Someone sitting in a car seat can turn the wheel
# with both hands and work the pedals with both feet without reaching for a
# keyboard between each one.
#
# Nothing here measures a limit. The quaternion gives every angle outright, so
# the sweep exists only to find each control's hinge — and the FIRST direction
# each control moves in, which is what says right from left and pressed from
# released.
CAL_STEPS = (
    ("zero", "Take your feet off both pedals and centre the steering wheel, "
             "then press Enter."),
    ("collect", "Turn the steering wheel RIGHT first, then left as far as it "
                "goes. Press the gas and brake all the way down."),
)


class Calibration(object):
    """What one sensor's calibration consists of, and how to read it back."""

    def __init__(self, role, axis, ref_q, full_deg=0.0, sweep_deg=0.0):
        self.role = role
        self.axis = axis
        self.ref_q = ref_q            # centre (steer) or rest (pedal)
        self.full_deg = full_deg      # pedal only, signed travel at full press
        # Diagnostics only, shown on the tab: how far the control was actually
        # turned while its axis was being measured. Nothing reads it to compute
        # anything — a big sweep just means a well-determined axis.
        self.sweep_deg = sweep_deg

    def to_json(self):
        d = {"role": self.role, "axis": list(self.axis),
             "ref_q": list(self.ref_q), "sweep_deg": round(self.sweep_deg, 1)}
        if self.role != "steer":
            d["full_deg"] = round(self.full_deg, 3)
        return d

    @classmethod
    def from_json(cls, d):
        try:
            axis = tuple(float(v) for v in d["axis"])
            ref = tuple(float(v) for v in d["ref_q"])
            if len(axis) != 3 or len(ref) != 4:
                return None
            # left_deg/right_deg from a pre-1.8.1 file are ignored on purpose:
            # the steering limits are no longer measured, and a stale pair would
            # otherwise look like a calibration that had been done properly.
            return cls(str(d["role"]), axis, ref,
                       float(d.get("full_deg", 0.0)),
                       float(d.get("sweep_deg", 0.0)))
        except (KeyError, TypeError, ValueError):
            return None


class Track(object):
    """The live unwrapped angle for one calibrated sensor."""

    def __init__(self):
        self.prev = None
        self.total = 0.0

    def reset(self):
        self.prev = None
        self.total = 0.0

    def advance(self, wrapped):
        if self.prev is None:
            self.total = wrapped
        else:
            self.total += wrap180(wrapped - self.prev)
        self.prev = wrapped


class Collector(object):
    """
    Watches one sensor through a calibration sweep and keeps the answer current.

    Everything here is INCREMENTAL — the axis is a running vector sum, the travel
    a running maximum. No sample is stored, so a sweep can run for as long as the
    operator wants without the box accumulating anything, and the result is
    available continuously rather than at the end. That is what lets the screen
    say "calibrated" the moment a control has moved enough, with nobody pressing
    anything.

    It also means the answer only ever IMPROVES while the sweep runs: a first
    pedal press that did not reach the floor is not a mistake to undo, it is
    simply a smaller envelope than the next press will give.
    """

    def __init__(self, role, ref_q):
        self.role = role
        self.ref_q = ref_q            # centre, or pedal at rest
        self.prev_q = ref_q
        self.sum = [0.0, 0.0, 0.0]    # running axis estimate, unnormalised
        self.mag = 0.0                # sum of the increments' LENGTHS
        self.ref_v = None             # first significant increment, fixes the fold
        self.sweep = 0.0              # total path length turned, degrees
        self.axis = None
        self.flipped = False          # the sign convention, once resolved
        self.sign_locked = False
        self.track = Track()
        self.travel = 0.0             # furthest from zero in the positive sense
        self.refined_at = 0.0

    # ── The sweep ────────────────────────────────────────────────────────────
    def feed(self, q, now):
        step = swept_deg(self.prev_q, q)
        if step >= NOISE_FLOOR_DEG:
            dq = q_canon(q_mul(q_conj(self.prev_q), q))
            v = [dq[1], dq[2], dq[3]]
            if self.ref_v is None:
                self.ref_v = list(v)
            elif sum(a * b for a, b in zip(v, self.ref_v)) < 0.0:
                v = [-c for c in v]     # turning back is the same hinge
            for i in range(3):
                self.sum[i] += v[i]
            self.mag += math.sqrt(sum(c * c for c in v))
            self.sweep += step
        self.prev_q = q

        if self.axis is None:
            if self.sweep < MIN_AXIS_SWEEP_DEG:
                return
            axis = self._axis()
            if axis is None:
                return
            # Start the angle here rather than at the zero pose. Only
            # MIN_AXIS_SWEEP_DEG of travel has happened, so "here" is within
            # that of the zero and the unwrap cannot already have missed a
            # half-turn.
            self.axis = axis
            self.track.reset()
            self.track.advance(twist_deg(self.ref_q, q, axis))
            self.refined_at = now
            return

        if now - self.refined_at >= AXIS_REFINE_SEC:
            self.refined_at = now
            self._adopt(self._axis(), q)

        self.track.advance(twist_deg(self.ref_q, q, self.axis))

        if not self.sign_locked:
            # The first real excursion from zero defines positive: the operator
            # was asked to turn RIGHT first, and a pedal can only be pressed.
            if abs(self.track.total) >= SIGN_DEG.get(self.role, SIGN_DEG_DEFAULT):
                if self.track.total < 0.0:
                    self.flipped = not self.flipped
                    self.axis = tuple(-c for c in self.axis)
                    self.track.total = -self.track.total
                    self.track.prev = -self.track.prev
                self.sign_locked = True
        if self.sign_locked and self.track.total > self.travel:
            self.travel = self.track.total

    def _axis(self):
        n = math.sqrt(sum(c * c for c in self.sum))
        if n < AXIS_MIN_NORM:
            return None
        axis = tuple(c / n for c in self.sum)
        return tuple(-c for c in axis) if self.flipped else axis

    def _adopt(self, axis, q):
        """Take a better axis, and let the angle it reports move with it.

        A refined axis measures the same rotation slightly differently. That
        difference is a CORRECTION, not noise, so it is left for the next
        advance() to apply — swallowing it instead (by re-seeding `prev` every
        time) loses a fraction of a degree at each refinement, and those add up
        into a real drift over a long sweep.

        The exception is a refinement that would move the angle more than a
        quarter turn. That is not a correction, it is an early estimate having
        been badly wrong, and carrying it into the turn count would corrupt the
        count rather than fix it — so that one is absorbed.
        """
        if axis is None or self.axis is None:
            return
        prev = self.track.prev
        self.axis = axis
        if prev is not None:
            jump = wrap180(twist_deg(self.ref_q, q, axis) - prev)
            if abs(jump) > 90.0:
                self.track.prev = twist_deg(self.ref_q, q, axis)

    # ── What it has ──────────────────────────────────────────────────────────
    def ready(self):
        if self.axis is None or not self.sign_locked:
            return False
        if self.role in PEDAL_ROLES:
            return self.travel >= MIN_PEDAL_TRAVEL_DEG
        return True

    def result(self):
        if not self.ready():
            return None
        return Calibration(self.role, self.axis, self.ref_q,
                           full_deg=self.travel, sweep_deg=self.sweep)

    def coherence(self):
        """How much of the movement was about ONE axis, 0..1.

        The increments were folded onto a common side before being summed, so a
        control turning on a real hinge gives |sum| == sum of lengths and this
        reads 1. A box being waved by hand gives increments pointing everywhere,
        they cancel, and this collapses — which is the difference between "keep
        pressing" and "that is not mounted to anything", and the box has no
        other way to tell an operator which one they are looking at.
        """
        if self.mag <= 0.0:
            return 0.0
        return math.sqrt(sum(c * c for c in self.sum)) / self.mag

    def progress(self):
        """What the screen says about this sensor while the sweep is running."""
        p = {"swept": round(self.sweep), "need": round(MIN_AXIS_SWEEP_DEG),
             "travel": round(self.travel) if self.sign_locked else 0,
             "coherence": round(self.coherence(), 2),
             "one_axis": self.mag <= 0.0 or self.coherence() >= MIN_COHERENCE}
        if self.axis is None:
            p["state"] = "waiting"
        elif not self.sign_locked:
            p["state"] = "measuring"
        else:
            p["state"] = "ready" if self.ready() else "measuring"
        return p


class SensorSet(object):
    """
    Every Drive Square sensor box on this machine, as one input device.

    Owns a thread that reads the ports and keeps each role's live angle current.
    adiona-wheel.py calls read_roles() while packing a packet, exactly as it does
    for a racing wheel.
    """

    def __init__(self, log, cal_path, stale_sec=0.5, pedal_deadzone_deg=1.5,
                 steer_range_deg=900, enabled=True):
        self._log = log
        self._cal_path = cal_path
        self._stale = stale_sec
        self._deadzone = pedal_deadzone_deg
        # The hardware's full travel, DECLARED rather than measured — a real
        # car's wheel is about 900 degrees and the box has no need to discover
        # that by having someone haul it against both stops. It bounds what goes
        # on the wire and it is what the headset is told the rig can do; it is
        # not a sensitivity control and it does not scale anything.
        self._steer_range = max(1, int(steer_range_deg))
        self._enabled = enabled

        self._lock = threading.Lock()
        self._ports = {}              # by-id link -> SensorPort
        # Both keyed by the box's own SERIAL, not by its role. Two things fall
        # out of that and both matter at an event: a box whose cable is knocked
        # out keeps its calibration when it comes back, and a DIFFERENT box put
        # in the same role gets its own calibration or none at all — never the
        # previous one's, which would be silently, plausibly wrong.
        self._cal = {}                # serial -> Calibration
        self._track = {}              # serial -> Track
        self._dirty = False
        self._message = ""
        self._last_scan = 0.0

        # The running calibration, or None.
        self._job = None              # {"role","index","samples","marks","q0"}

        self._stored = self._load()

    # ── Persistence ──────────────────────────────────────────────────────────
    def _load(self):
        try:
            with open(self._cal_path) as fh:
                data = json.load(fh)
        except (OSError, ValueError):
            return {}
        return data if isinstance(data, dict) else {}

    def _save(self):
        """Atomic write, same shape as the wheel map's save_user_map()."""
        body = json.dumps(self._stored, indent=2, sort_keys=True)
        tmp = self._cal_path + ".tmp"
        try:
            os.makedirs(os.path.dirname(self._cal_path), exist_ok=True)
            with open(tmp, "w") as fh:
                fh.write(body + "\n")
            os.replace(tmp, self._cal_path)
        except OSError as e:
            return str(e)
        return None

    # ── Discovery ────────────────────────────────────────────────────────────
    def _scan(self):
        """Open newly plugged boxes, drop unplugged ones.

        The by-id directory is the source of truth: udev removes the link the
        moment the device goes, so a directory diff is the whole hotplug story —
        the same 1 Hz poll the wheel and the keyboard already use."""
        try:
            names = sorted(os.listdir(BY_ID_DIR))
        except OSError:
            names = []
        seen = set()
        for name in names:
            link = os.path.join(BY_ID_DIR, name)
            if parse_by_id(name) is None:
                continue
            seen.add(link)
            if link in self._ports:
                continue
            port = open_port(link)
            if port is None:
                continue
            with self._lock:
                # Two boxes claiming one role (a spare left plugged in) would
                # otherwise both write the same track. First one wins, and the
                # second is reported so the operator can see why.
                if any(p.role == port.role for p in self._ports.values()):
                    self._log("sensors: ignoring duplicate %s box on %s"
                              % (port.role, port.dev))
                    port.close()
                    continue
                self._ports[link] = port
                self._apply_stored(port)
            self._log("sensors: %s box '%s' on %s" % (port.role, port.name, port.dev))

        for link in [p for p in self._ports if p not in seen]:
            with self._lock:
                port = self._ports.pop(link)
                # The calibration is deliberately NOT dropped — see _cal above.
                # The turn count is, because a wheel can be turned while its box
                # is unplugged and an integrated count cannot survive that.
                self._track.pop(port.serial, None)
                # A box that goes away mid-calibration takes its own collector
                # with it and leaves the rest of the sweep running: the others
                # are still being measured and there is no reason to make an
                # operator start over because a cable was nudged.
                if self._job is not None:
                    self._job["collectors"].pop(port.serial, None)
                    self._message = ("%s box unplugged — still calibrating the "
                                     "others" % port.role)
            self._log("sensors: %s box removed (%s)" % (port.role, port.dev))
            port.close()

    def _apply_stored(self, port):
        """Give a newly-seen box its calibration. Caller holds the lock.

        Either one already in memory from this session, or one off disk. A spare
        gas box therefore carries its calibration with it between rigs, and
        swapping one in is a plug rather than a recalibration."""
        self._track[port.serial] = Track()
        if port.serial in self._cal:
            return                             # still ours from earlier this run
        entry = self._stored.get(port.serial)
        if not isinstance(entry, dict):
            return
        cal = Calibration.from_json(entry)
        if cal is None or cal.role != port.role:
            return
        self._cal[port.serial] = cal

    def _entry_locked(self, role):
        """(port, calibration, track) for a role, or (None, None, None)."""
        port = self._port_locked(role)
        if port is None:
            return None, None, None
        return port, self._cal.get(port.serial), self._track.get(port.serial)

    # ── Reading ──────────────────────────────────────────────────────────────
    def run(self):
        """Thread entry point. Never returns, never propagates."""
        while True:
            try:
                self._tick()
            except Exception as e:                 # a fault here must not stop
                self._log("sensors: %s" % e)       # the wheel service
                time.sleep(0.5)

    def _tick(self):
        now = time.monotonic()
        if now - self._last_scan >= RESCAN_INTERVAL:
            self._last_scan = now
            self._scan()

        with self._lock:
            fds = [p.fd for p in self._ports.values()]
        if not fds:
            time.sleep(0.1)
            return
        try:
            ready, _, _ = select.select(fds, [], [], SELECT_TIMEOUT)
        except OSError:
            # A dead descriptor makes select() return instantly forever. Rescan
            # at once rather than spinning until the next scheduled one.
            self._last_scan = 0.0
            time.sleep(0.05)
            return
        if not ready:
            return
        with self._lock:
            ports = [p for p in self._ports.values() if p.fd in ready]
        for port in ports:
            self._drain(port)

    def _drain(self, port):
        while True:
            try:
                data = os.read(port.fd, 4096)
            except BlockingIOError:
                return
            except OSError:
                self._last_scan = 0.0             # unplugged mid-read
                return
            if not data:
                return
            port.buf += data
            if len(port.buf) > MAX_LINE_BYTES:
                # Not our protocol, or a burst of line noise. Keep the tail so a
                # real line starting inside it can still complete.
                port.buf = port.buf[-128:]
            while True:
                nl = port.buf.find(b"\n")
                if nl < 0:
                    break
                line = port.buf[:nl]
                port.buf = port.buf[nl + 1:]
                self._on_line(port, line)
            if len(data) < 4096:
                return

    def _on_line(self, port, line):
        parts = line.strip().split(b",")
        if len(parts) != LINE_FIELDS:
            return
        try:
            t_ms = int(parts[0])
            q = (float(parts[1]), float(parts[2]),
                 float(parts[3]), float(parts[4]))
        except ValueError:
            return
        n = math.sqrt(sum(v * v for v in q))
        if not (0.9 < n < 1.1):
            return                                # not a unit quaternion
        if n != 1.0:
            q = (q[0] / n, q[1] / n, q[2] / n, q[3] / n)

        now = time.monotonic()
        with self._lock:
            # Rate estimate over a one-second window, for --dump and the UI.
            port.rate_n += 1
            if port.rate_at == 0.0:
                port.rate_at = now
            elif now - port.rate_at >= 1.0:
                port.hz = port.rate_n / (now - port.rate_at)
                port.rate_at = now
                port.rate_n = 0

            port.q = q
            port.t_ms = t_ms
            port.last_rx = now
            port.samples += 1

            cal = self._cal.get(port.serial)
            if cal is not None:
                track = self._track.setdefault(port.serial, Track())
                track.advance(twist_deg(cal.ref_q, q, cal.axis))

            job = self._job
            if job is not None and job["index"] > 0:
                col = job["collectors"].get(port.serial)
                if col is not None:
                    col.feed(q, now)

    # ── What adiona-wheel.py consumes ────────────────────────────────────────
    def read_roles(self):
        """
        (steer_deg, throttle, brake) — the same triple as Wheel.read_roles().

        Every channel fails to NEUTRAL: an uncalibrated, stale or absent sensor
        reads zero rather than holding its last value. A pedal box knocked off
        its mount mid-drive must not leave the throttle open.
        """
        now = time.monotonic()
        with self._lock:
            return (self._steer_locked(now),
                    self._pedal_locked("gas", now),
                    self._pedal_locked("brake", now))

    def sample(self):
        """
        (ready, steer_deg, throttle, brake) in ONE lock acquisition.

        What the packet sender calls, every tick. Separate from read_roles() +
        usable() because those are two locks and two moments: the rig could go
        stale between them and the sender would ship a "ready" packet full of
        the neutral values staleness had just produced.
        """
        now = time.monotonic()
        with self._lock:
            return (self._ready_locked(now),
                    self._steer_locked(now),
                    self._pedal_locked("gas", now),
                    self._pedal_locked("brake", now))

    def _ready_locked(self, now):
        if self._job is not None:              # mid-calibration: not a car input
            return False
        for role in REQUIRED_ROLES:
            port, cal, _track = self._entry_locked(role)
            if port is None or cal is None or not port.fresh(now, self._stale):
                return False
        return True

    def _steer_locked(self, now):
        port, cal, track = self._entry_locked("steer")
        if port is None or cal is None or track is None:
            return 0.0
        if not port.fresh(now, self._stale) or track.prev is None:
            return 0.0
        # DEGREES FROM CENTRE, which is what goes on the wire — not a fraction of
        # anything. What the game does with them is the game's business: a
        # tractor, a forklift and a tiller truck's rear steer all want the same
        # number interpreted differently, and only the vehicle model knows how.
        #
        # The clamp is a hardware bound, not a mapping. It stops a slipped turn
        # count (the thing Ctrl+S exists for) from sending a car to a lock it
        # could never physically reach.
        half = self._steer_range / 2.0
        return max(-half, min(half, track.total))

    def _pedal_locked(self, role, now):
        port, cal, track = self._entry_locked(role)
        if port is None or cal is None or track is None:
            return 0.0
        if not port.fresh(now, self._stale) or track.prev is None:
            return 0.0
        span = abs(cal.full_deg) - self._deadzone
        if span <= 0.0:
            return 0.0
        # full_deg carries the sign of the press, so this is always positive for
        # a pedal going down and negative for one coming back past rest.
        travel = track.total * (1.0 if cal.full_deg >= 0.0 else -1.0)
        return max(0.0, min(1.0, (travel - self._deadzone) / span))

    def _collector_steer(self, col):
        """What a still-being-calibrated steering sensor reads, in degrees.

        Exactly what _steer_locked() would report once the calibration is
        committed, so nothing changes on screen at the moment it is."""
        if col.axis is None or not col.sign_locked:
            return 0.0
        half = self._steer_range / 2.0
        return max(-half, min(half, col.track.total))

    def _collector_pedal(self, col):
        """The same for a pedal, against the envelope measured SO FAR — so the
        deepest press of the sweep reads 1.0 as it happens, and a deeper one
        rescales what came before it."""
        if col.axis is None or not col.sign_locked:
            return 0.0
        span = col.travel - self._deadzone
        if span <= 0.0:
            return 0.0
        return max(0.0, min(1.0, (col.track.total - self._deadzone) / span))

    def _port_locked(self, role):
        for port in self._ports.values():
            if port.role == role:
                return port
        return None

    def present(self):
        """True with at least a steering and a gas box attached."""
        with self._lock:
            roles = set(p.role for p in self._ports.values())
        return all(r in roles for r in REQUIRED_ROLES)

    def usable(self):
        """True when the rig is present, fresh and calibrated enough to drive.

        Steering and gas must both be calibrated; brake is optional and simply
        reads zero when it is not."""
        with self._lock:
            return self._ready_locked(time.monotonic())

    def caps(self, cap_bits):
        """The CAP_SENSOR_* word for what is attached. cap_bits maps role -> bit."""
        word = 0
        with self._lock:
            for port in self._ports.values():
                word |= cap_bits.get(port.role, 0)
        return word

    def cal_flags(self, flag_bits):
        """The AI02 flag word for what is calibrated. flag_bits maps role -> bit.

        Only ATTACHED boxes count: a calibration held for one that is unplugged
        is worth keeping, but telling the headset the pedal is calibrated when
        there is no pedal would be a lie it acts on."""
        word = 0
        with self._lock:
            for port in self._ports.values():
                if port.serial in self._cal:
                    word |= flag_bits.get(port.role, 0)
        return word

    def range_deg(self, _default=None):
        """The hardware's full travel, for AI02.range_deg.

        Configured, not measured, and the same whether or not anything has been
        calibrated — it describes the wheel, and calibrating it does not change
        how far it turns."""
        return self._steer_range

    def device_name(self):
        with self._lock:
            n = len(self._ports)
        return "Drive Square USB sensors (%d)" % n if n else "Drive Square USB sensors"

    # ── Ctrl+S ───────────────────────────────────────────────────────────────
    def recentre(self):
        """
        Zero the steering turn count, with the wheel where it now stands.

        The turn count is the only integrated state in this file, and sharp
        erratic steering — the near-crash case this exists for — is exactly what
        can slip it. The BLE boxes answer the same problem with the same key.

        Deliberately does NOT touch the calibrated axis or range: a recovery must
        not cost a calibration. Deliberately does not mark the calibration dirty
        either — this is a live correction, not a new centre worth saving, and
        the stored centre is still the right one after a restart.
        """
        with self._lock:
            port, cal, _track = self._entry_locked("steer")
            if port is None or cal is None or port.q is None:
                return False
            cal.ref_q = port.q
            track = self._track.setdefault(port.serial, Track())
            track.reset()
            track.advance(0.0)
            self._message = ("Steering recentred — this position is now dead "
                             "ahead.")
        self._log("sensors: steering recentred")
        return True

    # ── Calibration commands ─────────────────────────────────────────────────
    def begin(self, _role=None):
        """Start the rig-wide calibration. The role argument is vestigial: there
        is one calibration and it covers everything attached."""
        with self._lock:
            if not self._ports:
                return "no sensor boxes attached"
            self._job = {"index": 0, "collectors": {}}
            self._message = ""
        return None

    def cancel(self):
        with self._lock:
            self._job = None
            self._message = "Calibration cancelled."
        return None

    def advance(self):
        """Enter: complete the current step and move on. Returns an error or None."""
        with self._lock:
            return self._advance_locked()

    def _advance_locked(self):
        """Enter. Two presses is the whole procedure: one to set the zero pose,
        one to accept what the sweep found."""
        job = self._job
        if job is None:
            return "not calibrating"

        if job["index"] == 0:
            live = [p for p in self._ports.values() if p.q is not None]
            if not live:
                return "no data from the sensor boxes"
            # One pose, every sensor. Wheel centred, pedals up.
            job["collectors"] = dict(
                (p.serial, Collector(p.role, p.q)) for p in live)
            job["index"] = 1
            self._message = "Zero positions set."
            return None

        return self._finish_locked(job)

    def _finish_locked(self, job):
        """Accept the sweep. Saves, because this is the confirming keypress."""
        done, rezeroed, missed = [], [], []
        for serial, col in job["collectors"].items():
            cal = col.result()
            if cal is not None:
                self._cal[serial] = cal
                track = self._track.setdefault(serial, Track())
                track.prev = col.track.prev
                track.total = col.track.total
                done.append(col.role)
                self._log("sensors: %s axis (%.3f, %.3f, %.3f) from %.0f deg "
                          "swept, travel %.0f"
                          % ((col.role,) + tuple(col.axis) +
                             (col.sweep, col.travel)))
                continue
            # It never moved enough to measure. If it already had a calibration
            # that is not a failure — the operator has just re-zeroed it, which
            # is a useful thing to be able to do on its own (a pedal box that
            # has settled, a wheel that needs its turn count cleared) and it
            # would be perverse to throw away a good axis for it.
            old = self._cal.get(serial)
            if old is not None:
                old.ref_q = col.ref_q
                t = self._track.setdefault(serial, Track())
                t.reset()
                t.advance(0.0)
                rezeroed.append(col.role)
            else:
                missed.append(col.role)

        self._job = None
        self._dirty = True
        err = self._persist_locked()
        if err is not None:
            self._message = "Save failed: %s" % err
            return self._message

        parts = []
        if done:
            parts.append("calibrated " + ", ".join(sorted(done)))
        if rezeroed:
            parts.append("re-zeroed " + ", ".join(sorted(rezeroed)))
        if missed:
            parts.append("no movement from " + ", ".join(sorted(missed)))
        self._message = ("Saved — " + "; ".join(parts) + "."
                         if parts else "Nothing to save.")
        return None

    def clear(self, role):
        with self._lock:
            port = self._port_locked(role)
            if port is None:
                return "no %s box attached" % role
            self._cal.pop(port.serial, None)
            self._track.pop(port.serial, None)
            self._stored.pop(port.serial, None)
            self._dirty = True
            self._message = "%s calibration cleared." % role.capitalize()
        return None

    def _persist_locked(self):
        """Write every calibration held in memory. Caller holds the lock.

        Everything, not only what is plugged in right now: a box calibrated
        earlier and since unplugged has a calibration worth keeping, and losing
        it at the moment of saving would be a surprising way to lose one."""
        for serial, cal in self._cal.items():
            self._stored[serial] = cal.to_json()
        err = self._save()
        if err is None:
            self._dirty = False
        return err

    def save(self):
        """Explicit S.

        MID-CALIBRATION, S CONFIRMS. The screen shows a "Save (S)" button
        throughout, so S is the obvious key to reach for at the end of a sweep —
        and writing the committed set at that moment would write everything the
        sweep has NOT yet produced, i.e. nothing, and then report success. That
        happened, silently, and cost a calibration."""
        with self._lock:
            if self._job is not None:
                return self._advance_locked()
            err = self._persist_locked()
            self._message = "Saved." if err is None else ("Save failed: %s" % err)
        return err

    # ── Published state ──────────────────────────────────────────────────────
    def status(self, licensed=True, active=False):
        now = time.monotonic()
        with self._lock:
            sensors = []
            for role in ROLES:
                port, cal, track = self._entry_locked(role)
                if port is None:
                    continue
                entry = {
                    "role": role,
                    "name": port.name,
                    "serial": port.serial,
                    "dev": port.dev,
                    "fresh": port.fresh(now, self._stale),
                    "hz": round(port.hz, 1),
                    "q": [round(v, 4) for v in port.q] if port.q else None,
                    "calibrated": cal is not None,
                    "axis": [round(v, 4) for v in cal.axis] if cal else None,
                }
                # While a sweep is running, the row reports what THIS sensor is
                # doing RIGHT NOW rather than its saved state — which is usually
                # nothing, since a first calibration has none. That live readout
                # is the whole interface for a step with no keypress in it, and
                # it is the only chance to notice the steering reading backwards
                # before it is committed.
                col = None
                if self._job is not None and self._job["index"] > 0:
                    col = self._job["collectors"].get(port.serial)
                    if col is not None:
                        entry["progress"] = col.progress()

                if role == "steer":
                    entry["range_deg"] = self._steer_range
                    if col is not None:
                        entry["value"] = round(self._collector_steer(col), 1)
                        entry["raw_deg"] = round(col.track.total, 1)
                    else:
                        entry["value"] = round(self._steer_locked(now), 1)
                        entry["raw_deg"] = (round(track.total, 1)
                                            if track and track.prev is not None else None)
                    if cal:
                        entry["sweep_deg"] = round(cal.sweep_deg, 0)
                elif role in PEDAL_ROLES:
                    if col is not None:
                        entry["value"] = round(self._collector_pedal(col), 3)
                        entry["raw_deg"] = round(col.track.total, 1)
                    else:
                        entry["value"] = round(self._pedal_locked(role, now), 3)
                        entry["raw_deg"] = (round(track.total, 1)
                                            if track and track.prev is not None else None)
                    if cal:
                        entry["full_deg"] = round(abs(cal.full_deg), 1)
                else:
                    entry["value"] = None
                sensors.append(entry)

            job = None
            if self._job is not None:
                idx = min(self._job["index"], len(CAL_STEPS) - 1)
                cols = self._job["collectors"]
                job = {
                    "step": CAL_STEPS[idx][0],
                    "prompt": CAL_STEPS[idx][1],
                    "index": self._job["index"],
                    "steps": len(CAL_STEPS),
                    # True once every attached sensor has what it needs, which is
                    # the moment the operator can stop and press Enter.
                    "all_ready": bool(cols) and all(c.ready() for c in cols.values()),
                }

            roles = set(p.role for p in self._ports.values())
            return {
                "enabled": self._enabled,
                "present": all(r in roles for r in REQUIRED_ROLES),
                "attached": len(self._ports),
                "licensed": bool(licensed),
                "active": bool(active),
                "dirty": self._dirty,
                "message": self._message,
                "calibrating": job,
                "sensors": sensors,
            }

    def close(self):
        with self._lock:
            for port in self._ports.values():
                port.close()
            self._ports.clear()
