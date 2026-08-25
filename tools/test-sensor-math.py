#!/usr/bin/env python3
"""
Synthetic checks for the quaternion maths in system/wheel/adiona_sensors.py.

Runs anywhere, including a Windows checkout — no Pi, no sensors, no serial port.
That is the point: the axis discovery and the multi-turn unwrap are the parts of
this feature that are hardest to judge by eye on real hardware (a wrong axis
still produces a plausible-looking angle), and easiest to be certain about
against a control whose true motion is known exactly.

    python3 tools/test-sensor-math.py

Each case builds the quaternion stream a box bolted to a control WOULD send —
an arbitrary mounting orientation composed with a rotation about the control's
own hinge — and checks that the code recovers the hinge and the travel from it.
"""

import math
import os
import random
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "system", "wheel"))
import adiona_sensors as S                                   # noqa: E402

FAIL = []


def check(name, cond, detail=""):
    print(("  ok   " if cond else "  FAIL ") + name + (("  " + detail) if detail else ""))
    if not cond:
        FAIL.append(name)


def q_axis_angle(axis, deg):
    h = math.radians(deg) / 2.0
    s = math.sin(h)
    return (math.cos(h), axis[0] * s, axis[1] * s, axis[2] * s)


def norm3(v):
    n = math.sqrt(sum(c * c for c in v))
    return tuple(c / n for c in v)


def axis_error_deg(got, want):
    """Angle between two axes, ignoring sign (an axis and its negative are the
    same hinge; the sign convention is fixed separately, from the sweep)."""
    d = abs(sum(a * b for a, b in zip(got, want)))
    return math.degrees(math.acos(max(-1.0, min(1.0, d))))


def sweep(mount, hinge, angles, noise=0.0, rng=None):
    """What a box mounted at `mount` reports as its control turns about `hinge`."""
    out = []
    for a in angles:
        q = S.q_mul(mount, q_axis_angle(hinge, a))
        if noise:
            q = tuple(c + rng.gauss(0.0, noise) for c in q)
            n = math.sqrt(sum(c * c for c in q))
            q = tuple(c / n for c in q)
        out.append(q)
    return out


def ramp(lo, hi, step):
    n = max(1, int(abs(hi - lo) / step))
    return [lo + (hi - lo) * i / float(n) for i in range(n + 1)]


def steer_sign(qs, q0, left_end):
    """Replay a steering sweep with the calibration's own sign convention:
    positive is right, so the left sweep must end negative."""
    axis = S.discover_axis(qs)
    thetas, _, _ = S.replay(qs, q0, axis)
    if thetas[left_end] > 0.0:
        axis = tuple(-c for c in axis)
        thetas, _, _ = S.replay(qs, q0, axis)
    return axis, thetas


print("\n1. axis discovery from an arbitrary mounting")
rng = random.Random(7)
for i, (mount_axis, mount_deg, hinge_axis) in enumerate((
        ((0, 1, 0), 0.0, (1, 0, 0)),
        ((1, 0, 0), 37.0, (0, 0, 1)),
        ((0.3, 0.5, -0.8), 121.0, (0.2, -0.9, 0.35)),
)):
    mount = q_axis_angle(norm3(mount_axis), mount_deg)
    hinge = norm3(hinge_axis)
    err = axis_error_deg(S.discover_axis(sweep(mount, hinge, ramp(0, 450, 3.0))), hinge)
    check("mounting %d: axis within 1 deg" % i, err < 1.0, "err %.4f deg" % err)

print("\n2. axis discovery survives sensor noise")
mount = q_axis_angle(norm3((0.3, 0.5, -0.8)), 121.0)
hinge = norm3((0.2, -0.9, 0.35))
qs = sweep(mount, hinge, ramp(0, 450, 3.0), noise=1e-3, rng=rng)
err = axis_error_deg(S.discover_axis(qs), hinge)
check("axis within 2 deg at 1e-3 per component", err < 2.0, "err %.4f deg" % err)

print("\n3. multi-turn unwrap: a 900 degree wheel, lock to lock")
mount = q_axis_angle(norm3((1, 2, 3)), 63.0)
hinge = norm3((0.1, 0.2, 0.97))
left = ramp(0, -450, 2.0)
angles = left + ramp(-450, 450, 2.0)
qs = sweep(mount, hinge, angles)
axis, thetas = steer_sign(qs, qs[0], len(left) - 1)
maxerr = max(abs(t - a) for t, a in zip(thetas, angles))
check("tracks past +-360 with no jump", maxerr < 0.5, "max err %.4f deg" % maxerr)
check("left stop lands on -450", abs(min(thetas) + 450) < 0.5, "%.2f" % min(thetas))
check("right stop lands on +450", abs(max(thetas) - 450) < 0.5, "%.2f" % max(thetas))
cal = S.Calibration("steer", axis, qs[0], min(thetas), max(thetas))
check("reported range_deg is 900", cal.range_deg() == 900, str(cal.range_deg()))

print("\n4. multi-turn unwrap: four full turns")
left = ramp(0, -720, 2.0)
angles = left + ramp(-720, 720, 2.0)
qs = sweep(mount, hinge, angles)
_, thetas = steer_sign(qs, qs[0], len(left) - 1)
maxerr = max(abs(t - a) for t, a in zip(thetas, angles))
check("tracks past +-720", maxerr < 0.5, "max err %.4f deg" % maxerr)

print("\n5. coarse sampling still unwraps")
# The real boxes send 33-67 Hz, so a sample is a degree or two apart. This is
# 30 degrees a sample: the unwrap only fails past 180.
angles = ramp(0, 900, 30.0)
qs = sweep(mount, hinge, angles)
axis = S.discover_axis(qs)
thetas, _, _ = S.replay(qs, qs[0], axis)
if thetas[-1] < 0:
    axis = tuple(-c for c in axis)
    thetas, _, _ = S.replay(qs, qs[0], axis)
check("900 degrees reached", abs(thetas[-1] - 900) < 1.0, "%.2f" % thetas[-1])

print("\n6. wrap180")
for a, want in ((0, 0), (90, 90), (-90, -90), (270, -90), (-270, 90), (359, -1)):
    check("wrap180(%d) == %d" % (a, want), abs(S.wrap180(a) - want) < 1e-9,
          "%.6f" % S.wrap180(a))

print("\n7. a control that never moved is rejected, not guessed at")
qs = sweep(mount, hinge, [0.0] * 200, noise=1e-4, rng=rng)
check("discover_axis returns None", S.discover_axis(qs) is None)

print("\n8. pedal: rest, full press, back to rest")
mount = q_axis_angle(norm3((0.7, -0.2, 0.1)), 88.0)
hinge = norm3((-0.4, 0.9, 0.15))
# Deliberately hinged the negative way round, which is the case the sign flip in
# _finish_pedal_locked() exists for.
angles = ramp(0, -42, 0.8) + ramp(-42, 0, 0.8)
qs = sweep(mount, hinge, angles)
axis = S.discover_axis(qs)
thetas, _, _ = S.replay(qs, qs[0], axis)
peak = max(thetas, key=abs)
if peak < 0:
    axis = tuple(-c for c in axis)
    thetas, _, _ = S.replay(qs, qs[0], axis)
    peak = -peak
check("travel measured as 42 deg", abs(peak - 42) < 0.5, "%.2f" % peak)

cal = S.Calibration("gas", axis, qs[0], full_deg=peak)
dead = 1.5
span = abs(cal.full_deg) - dead
vals = [max(0.0, min(1.0, (t - dead) / span)) for t in thetas]
check("rest reads exactly 0.0", vals[0] == 0.0, "%.4f" % vals[0])
check("full press reads 1.0", abs(max(vals) - 1.0) < 1e-9, "%.4f" % max(vals))
check("released reads exactly 0.0", vals[-1] == 0.0, "%.4f" % vals[-1])

print("\n9. a calibration survives the JSON round trip it is stored as")
back = S.Calibration.from_json(cal.to_json())
check("round trip", back is not None
      and back.role == cal.role
      and max(abs(a - b) for a, b in zip(back.axis, cal.axis)) < 1e-3
      and abs(back.full_deg - cal.full_deg) < 1e-2)
check("a truncated entry is rejected, not half-loaded",
      S.Calibration.from_json({"role": "gas", "axis": [1, 0]}) is None)

print("\n10. by-id parsing, against the real names from a box")
for base, role, serial in (
    ("usb-Drive_Square_D2_Steering_Sensor_7519AEA050304B4B542E3120FF17110E-if00",
     "steer", "7519AEA050304B4B542E3120FF17110E"),
    ("usb-Drive_Square_D2_Gas_Sensor_4680306350304A46432E3120FF10302B-if00",
     "gas", "4680306350304A46432E3120FF10302B"),
    ("usb-Drive_Square_D2_Brake_Sensor_DC078ED750304A46432E3120FF102A27-if00",
     "brake", "DC078ED750304A46432E3120FF102A27"),
):
    got = S.parse_by_id(base)
    check("%s box recognised" % role,
          got is not None and got[0] == role and got[1] == serial, str(got))
check("a non-Drive-Square serial device is ignored",
      S.parse_by_id("usb-FTDI_FT232R_USB_UART_A50285BI-if00-port0") is None)
check("the name reads back cleanly for the UI",
      S.parse_by_id(
          "usb-Drive_Square_D2_Steering_Sensor_7519AEA050304B4B542E3120FF17110E-if00"
      )[2] == "Drive Square D2 Steering Sensor")

print("\n%s\n" % ("ALL PASS" if not FAIL else "FAILURES: %s" % ", ".join(FAIL)))
sys.exit(1 if FAIL else 0)
