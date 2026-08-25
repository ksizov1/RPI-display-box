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
     axis a control turns about. So the operator turns the wheel left and right
     and the box works out the rest. See discover_axis().

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

# Sanity floors for a finished calibration.
MIN_STEER_RANGE_DEG = 30.0
MIN_PEDAL_TRAVEL_DEG = 5.0

RESCAN_INTERVAL = 1.0        # look for a newly plugged sensor box
SELECT_TIMEOUT = 0.05

# Calibration sample cap. A 900 degree sweep at 67 Hz is a few hundred samples;
# this is a bound on a careless operator leaving the step open, not a target.
MAX_CAL_SAMPLES = 20000


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
CAL_STEPS = {
    "steer": (
        ("centre", "Centre the steering wheel, then press Enter."),
        ("left", "Turn fully LEFT to the stop, then press Enter."),
        ("right", "Turn fully RIGHT to the stop, then press Enter."),
    ),
    "gas": (
        ("rest", "Take your foot off the GAS pedal, then press Enter."),
        ("press", "Press the GAS pedal fully down and hold, then press Enter."),
    ),
    "brake": (
        ("rest", "Take your foot off the BRAKE pedal, then press Enter."),
        ("press", "Press the BRAKE pedal fully down and hold, then press Enter."),
    ),
}


class Calibration(object):
    """What one sensor's calibration consists of, and how to read it back."""

    def __init__(self, role, axis, ref_q, left_deg=0.0, right_deg=0.0,
                 full_deg=0.0):
        self.role = role
        self.axis = axis
        self.ref_q = ref_q            # centre (steer) or rest (pedal)
        self.left_deg = left_deg      # steer only, negative
        self.right_deg = right_deg    # steer only, positive
        self.full_deg = full_deg      # pedal only, signed travel at full press

    def to_json(self):
        d = {"role": self.role, "axis": list(self.axis), "ref_q": list(self.ref_q)}
        if self.role == "steer":
            d["left_deg"] = round(self.left_deg, 3)
            d["right_deg"] = round(self.right_deg, 3)
        else:
            d["full_deg"] = round(self.full_deg, 3)
        return d

    @classmethod
    def from_json(cls, d):
        try:
            axis = tuple(float(v) for v in d["axis"])
            ref = tuple(float(v) for v in d["ref_q"])
            if len(axis) != 3 or len(ref) != 4:
                return None
            return cls(str(d["role"]), axis, ref,
                       float(d.get("left_deg", 0.0)),
                       float(d.get("right_deg", 0.0)),
                       float(d.get("full_deg", 0.0)))
        except (KeyError, TypeError, ValueError):
            return None

    def half_range_deg(self):
        """Usable travel each side of centre — the shorter of the two.

        The headset derives full lock as range_deg / 2 either side of centre, so
        the two sides have to be treated symmetrically somewhere. Doing it here,
        at the shorter side, means full lock is always reachable; taking the
        longer one, or the average, would put the game's full lock past a stop.
        """
        return min(-self.left_deg, self.right_deg)

    def range_deg(self):
        """Symmetric usable travel, for AI02.range_deg.

        This is the figure the headset divides steer_deg by, so it must be twice
        exactly the half-range this file clamps to — otherwise full lock in the
        game and full lock on the wheel are two different places."""
        return int(round(2.0 * self.half_range_deg()))


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


class SensorSet(object):
    """
    Every Drive Square sensor box on this machine, as one input device.

    Owns a thread that reads the ports and keeps each role's live angle current.
    adiona-wheel.py calls read_roles() while packing a packet, exactly as it does
    for a racing wheel.
    """

    def __init__(self, log, cal_path, stale_sec=0.5, pedal_deadzone_deg=1.5,
                 enabled=True):
        self._log = log
        self._cal_path = cal_path
        self._stale = stale_sec
        self._deadzone = pedal_deadzone_deg
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
                if self._job and self._job["role"] == port.role:
                    self._job = None
                    self._message = ("%s box unplugged — calibration cancelled"
                                     % port.role)
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
            if job is not None and job["role"] == port.role and job["index"] > 0:
                if len(job["samples"]) < MAX_CAL_SAMPLES:
                    job["samples"].append(q)

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
        # Clamped to the SYMMETRIC half-range, not to the measured stops: this
        # value is divided by exactly half of range_deg on the headset, so the
        # two have to be the same number. Travel past it on the longer side is
        # already full lock in the game and nothing is lost by stopping here.
        half = cal.half_range_deg()
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

    def range_deg(self, default):
        with self._lock:
            _port, cal, _track = self._entry_locked("steer")
        if cal is None:
            return default
        deg = cal.range_deg()
        return deg if deg > 0 else default

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
    def begin(self, role):
        if role not in CAL_STEPS:
            return "unknown role"
        with self._lock:
            if self._port_locked(role) is None:
                return "no %s box attached" % role
            self._job = {"role": role, "index": 0, "samples": [],
                         "marks": {}, "q0": None}
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
            job = self._job
            if job is None:
                return "not calibrating"
            role = job["role"]
            steps = CAL_STEPS[role]
            port = self._port_locked(role)
            if port is None or port.q is None:
                return "no data from the %s box" % role

            if job["index"] == 0:
                # The reference pose: centre for steering, rest for a pedal.
                job["q0"] = port.q
                job["samples"] = []
                job["index"] = 1
                return None

            # Mark where this sweep ended, then either move on or finish.
            job["marks"][steps[job["index"]][0]] = len(job["samples"])
            job["index"] += 1
            if job["index"] < len(steps):
                return None
            return self._finish_locked(job)

    def _finish_locked(self, job):
        role = job["role"]
        samples = job["samples"]
        port = self._port_locked(role)
        if port is None:
            self._job = None
            self._message = "The %s box was unplugged." % role
            return self._message
        axis = discover_axis(samples)
        if axis is None:
            self._job = None
            self._message = "The %s control did not move enough to measure." % role
            return self._message

        if role == "steer":
            err = self._finish_steer_locked(job, axis, port)
        else:
            err = self._finish_pedal_locked(job, axis, port)
        self._job = None
        return err

    def _finish_steer_locked(self, job, axis, port):
        q0 = job["q0"]
        samples = job["samples"]
        thetas, prev, total = replay(samples, q0, axis)

        # Sign convention: positive is right. The left sweep ran from the start
        # of the recording to the 'left' mark, so its end must be negative.
        left_mark = max(1, job["marks"].get("left", len(thetas)))
        if thetas and thetas[left_mark - 1] > 0.0:
            axis = (-axis[0], -axis[1], -axis[2])
            thetas, prev, total = replay(samples, q0, axis)

        left_deg = min(thetas) if thetas else 0.0
        right_deg = max(thetas) if thetas else 0.0
        if right_deg - left_deg < MIN_STEER_RANGE_DEG:
            self._message = ("Only %.0f degrees of travel measured — turn the "
                             "wheel to both stops." % (right_deg - left_deg))
            return self._message
        if left_deg >= 0.0 or right_deg <= 0.0:
            self._message = ("The wheel only turned one way from centre. Centre "
                             "it first, then go fully left and fully right.")
            return self._message

        cal = Calibration("steer", axis, q0, left_deg=left_deg, right_deg=right_deg)
        self._cal[port.serial] = cal
        track = self._track.setdefault(port.serial, Track())
        track.prev = prev
        track.total = total
        self._dirty = True
        self._message = ("Steering: range %d degrees (left %.0f, right %.0f). "
                         "Press S to save." % (cal.range_deg(), left_deg, right_deg))
        self._log("sensors: steer axis (%.3f, %.3f, %.3f) range %d"
                  % (axis[0], axis[1], axis[2], cal.range_deg()))
        return None

    def _finish_pedal_locked(self, job, axis, port):
        role = job["role"]
        q0 = job["q0"]
        thetas, prev, total = replay(job["samples"], q0, axis)
        if not thetas:
            self._message = "No data from the %s box." % role
            return self._message

        # The pedal's travel is the extreme it reached; its sign tells us which
        # way "pressed" is, and the axis is flipped so pressing always counts up.
        peak = max(thetas, key=abs)
        if peak < 0.0:
            axis = (-axis[0], -axis[1], -axis[2])
            thetas, prev, total = replay(job["samples"], q0, axis)
            peak = -peak
        if peak < MIN_PEDAL_TRAVEL_DEG:
            self._message = ("Only %.1f degrees of travel measured — press the "
                             "%s pedal all the way down." % (peak, role))
            return self._message

        cal = Calibration(role, axis, q0, full_deg=peak)
        self._cal[port.serial] = cal
        track = self._track.setdefault(port.serial, Track())
        track.prev = prev
        track.total = total
        self._dirty = True
        self._message = ("%s: %.0f degrees of travel. Press S to save."
                         % (role.capitalize(), peak))
        self._log("sensors: %s axis (%.3f, %.3f, %.3f) travel %.1f"
                  % (role, axis[0], axis[1], axis[2], peak))
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

    def save(self):
        with self._lock:
            # Everything held in memory, not only what is plugged in right now:
            # a box calibrated earlier and since unplugged has a calibration
            # worth keeping, and losing it at the moment of saving would be a
            # surprising way to lose one.
            for serial, cal in self._cal.items():
                self._stored[serial] = cal.to_json()
            err = self._save()
            if err is None:
                self._dirty = False
                self._message = "Saved."
            else:
                self._message = "Save failed: %s" % err
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
                if role == "steer":
                    entry["value"] = round(self._steer_locked(now), 1)
                    entry["raw_deg"] = round(track.total, 1) if track and track.prev is not None else None
                    if cal:
                        entry["left_deg"] = round(cal.left_deg, 0)
                        entry["right_deg"] = round(cal.right_deg, 0)
                        entry["range_deg"] = cal.range_deg()
                elif role in PEDAL_ROLES:
                    entry["value"] = round(self._pedal_locked(role, now), 3)
                    entry["raw_deg"] = round(track.total, 1) if track and track.prev is not None else None
                    if cal:
                        entry["full_deg"] = round(abs(cal.full_deg), 1)
                else:
                    entry["value"] = None
                sensors.append(entry)

            job = None
            if self._job is not None:
                role = self._job["role"]
                steps = CAL_STEPS[role]
                idx = min(self._job["index"], len(steps) - 1)
                job = {
                    "role": role,
                    "step": steps[idx][0],
                    "prompt": steps[idx][1],
                    "index": self._job["index"],
                    "steps": len(steps),
                    "samples": len(self._job["samples"]),
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
