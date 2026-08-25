#!/usr/bin/env python3
"""
End-to-end test of the USB vehicle sensor path, with no sensors on the desk.

tools/test-sensor-math.py checks the quaternion maths in isolation. This checks
everything wrapped around it: device discovery through /dev/serial/by-id, role
assignment from the device name, opening and configuring the tty, parsing the
line protocol, hotplug in both directions, the calibration state machine, and
what read_roles() finally hands the packet sender.

It does that by standing up three PTYs, symlinking them under a fake by-id
directory with the names the real boxes enumerate as, and writing exactly the
line format measured on the hardware. From adiona_sensors.py's point of view
there is no difference — which is the point, and the reason this can test a
SUCCESSFUL calibration, the one thing no amount of staring at a still rig will.

Linux (or WSL) only: it needs PTYs and symlinks. Run it after touching anything
in system/wheel/adiona_sensors.py.

    python3 tools/test-sensor-loopback.py
"""

import math
import os
import shutil
import sys
import tempfile
import threading
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "system", "wheel"))

if not hasattr(os, "openpty"):
    sys.exit("needs PTYs — run this on Linux or WSL, not Windows")

TMP = tempfile.mkdtemp(prefix="adiona-sensors-")
os.environ["ADIONA_SENSOR_BY_ID"] = os.path.join(TMP, "by-id")
os.makedirs(os.environ["ADIONA_SENSOR_BY_ID"])

import adiona_sensors as S                                       # noqa: E402

FAIL = []
QUIET = type("Q", (), {"__call__": lambda *a: None})()


def check(name, cond, detail=""):
    print(("  ok   " if cond else "  FAIL ") + name + (("  " + detail) if detail else ""))
    if not cond:
        FAIL.append(name)


# ── A fake sensor box ────────────────────────────────────────────────────────
NAMES = {
    "steer": "usb-Drive_Square_D2_Steering_Sensor_7519AEA050304B4B542E3120FF17110E-if00",
    "gas": "usb-Drive_Square_D2_Gas_Sensor_4680306350304A46432E3120FF10302B-if00",
    "brake": "usb-Drive_Square_D2_Brake_Sensor_DC078ED750304A46432E3120FF102A27-if00",
}


class FakeBox(object):
    """A PTY that writes the real line format: '<t_ms>, qw, qx, qy, qz\\r\\n'."""

    def __init__(self, role, mount, hinge, hz):
        self.role, self.mount, self.hinge, self.hz = role, mount, hinge, hz
        self.master, self.slave = os.openpty()
        self.link = os.path.join(os.environ["ADIONA_SENSOR_BY_ID"], NAMES[role])
        os.symlink(os.ttyname(self.slave), self.link)
        self.angle = 0.0
        self.t_ms = 1000
        self.alive = True
        threading.Thread(target=self._run, daemon=True).start()

    def _run(self):
        period = 1.0 / self.hz
        while self.alive:
            q = S.q_mul(self.mount, axis_angle(self.hinge, self.angle))
            line = "%d, %.4f, %.4f, %.4f, %.4f\r\n" % (
                self.t_ms, q[0], q[1], q[2], q[3])
            try:
                os.write(self.master, line.encode())
            except OSError:
                return
            self.t_ms += int(period * 1000)
            time.sleep(period)

    def sweep_to(self, target, step=3.0):
        """Move as a real control does — through every angle in between, at the
        box's own rate. Jumping straight there would skip the unwrap entirely."""
        while abs(target - self.angle) > step:
            self.angle += step if target > self.angle else -step
            time.sleep(1.2 / self.hz)
        self.angle = target
        time.sleep(0.15)

    def unplug(self):
        self.alive = False
        os.unlink(self.link)
        os.close(self.master)
        os.close(self.slave)


def axis_angle(axis, deg):
    h = math.radians(deg) / 2.0
    s = math.sin(h)
    return (math.cos(h), axis[0] * s, axis[1] * s, axis[2] * s)


def norm3(v):
    n = math.sqrt(sum(c * c for c in v))
    return tuple(c / n for c in v)


def wait_for(pred, timeout=6.0, why=""):
    end = time.time() + timeout
    while time.time() < end:
        if pred():
            return True
        time.sleep(0.05)
    if why:
        print("      (timed out waiting for %s)" % why)
    return False


def role_of(st, role):
    for s in st["sensors"]:
        if s["role"] == role:
            return s
    return None


# ── The rig ──────────────────────────────────────────────────────────────────
# Arbitrary mountings and arbitrary hinges, because that is the whole premise:
# nothing is bolted in a known orientation any more.
boxes = {
    "steer": FakeBox("steer", axis_angle(norm3((1, 2, 3)), 63.0),
                     norm3((0.1, 0.2, 0.97)), 67),
    "gas": FakeBox("gas", axis_angle(norm3((0.7, -0.2, 0.1)), 88.0),
                   norm3((-0.4, 0.9, 0.15)), 33),
    "brake": FakeBox("brake", axis_angle(norm3((-0.3, 0.6, 0.5)), 145.0),
                     norm3((0.8, 0.1, -0.59)), 33),
}

CAL = os.path.join(TMP, "sensor-cal.json")
st = S.SensorSet(QUIET, CAL, stale_sec=0.5, pedal_deadzone_deg=1.5)
threading.Thread(target=st.run, daemon=True).start()

try:
    print("\n1. discovery, roles and the line protocol")
    check("all three boxes found", wait_for(lambda: st.status()["attached"] == 3,
                                            why="3 boxes"),
          "attached=%d" % st.status()["attached"])
    s = st.status()
    check("rig reports present (steering + gas)", s["present"])
    for role in ("steer", "gas", "brake"):
        e = role_of(s, role)
        check("%s box parsed and fresh" % role, e is not None and e["fresh"],
              str(e and e["q"]))
    check("nothing drives the stream uncalibrated", st.sample()[0] is False,
          str(st.sample()))

    print("\n2. rates are measured from the stream, not assumed")
    wait_for(lambda: role_of(st.status(), "steer")["hz"] > 0, why="a rate")
    hz = role_of(st.status(), "steer")["hz"]
    check("steering rate near 67 Hz", 55 < hz < 80, "%.1f Hz" % hz)

    print("\n3. ONE calibration for the whole rig, in two keypresses")
    check("begin accepted", st.begin() is None)
    check("step 1 is the shared zero pose",
          st.status()["calibrating"]["step"] == "zero")
    st.advance()                                    # keypress 1: zero everything
    check("advanced to the sweep",
          st.status()["calibrating"]["step"] == "collect")
    check("nothing is ready before anything has moved",
          st.status()["calibrating"]["all_ready"] is False)

    # Everything moves during ONE step, in any order. No keypresses in here.
    boxes["steer"].sweep_to(200.0)                  # right first: sets the sign
    boxes["steer"].sweep_to(-160.0)                 # then back across centre
    check("steering reports itself calibrated, unprompted",
          wait_for(lambda: (role_of(st.status(), "steer").get("progress") or {})
                   .get("state") == "ready", why="the steering"))
    check("...but the rig is not ready while the pedals have not moved",
          st.status()["calibrating"]["all_ready"] is False)

    boxes["gas"].sweep_to(-20.0, step=1.0)          # a half-hearted first press
    boxes["gas"].sweep_to(-42.0, step=1.0)          # then a real one
    boxes["gas"].sweep_to(0.0, step=1.0)
    boxes["brake"].sweep_to(30.0, step=1.0)
    boxes["brake"].sweep_to(0.0, step=1.0)
    check("all three report ready, still with no keypress",
          wait_for(lambda: st.status()["calibrating"]["all_ready"], timeout=8.0,
                   why="every sensor"))

    # The bars have to move DURING the sweep, or there is no way to notice the
    # steering reading backwards while there is still time to redo it.
    boxes["steer"].sweep_to(200.0)
    wait_for(lambda: role_of(st.status(), "steer")["value"] > 150, why="a live angle")
    check("steering reads live while still calibrating",
          role_of(st.status(), "steer")["value"] > 150,
          "%.0f deg" % role_of(st.status(), "steer")["value"])
    boxes["gas"].sweep_to(-42.0, step=1.0)
    wait_for(lambda: role_of(st.status(), "gas")["value"] > 0.9, why="a live pedal")
    check("gas reads live against the envelope so far",
          abs(role_of(st.status(), "gas")["value"] - 1.0) < 0.05,
          "%.3f" % role_of(st.status(), "gas")["value"])
    boxes["gas"].sweep_to(0.0, step=1.0)
    boxes["steer"].sweep_to(0.0)

    # S, not Enter. The card shows a "Save (S)" button throughout, so this is
    # what a hand actually reaches for — and it used to write the empty
    # committed set, report "Saved.", and leave the sweep running and lost.
    err = st.save()                                 # keypress 2: confirm + save
    check("S at the end of a sweep CONFIRMS it", err is None, str(err))
    check("not calibrating any more", st.status()["calibrating"] is None)
    check("it saved itself — no separate keypress needed", os.path.isfile(CAL))
    check("and the file is not an empty object",
          len(open(CAL).read().strip()) > 50, "%d bytes" % len(open(CAL).read()))

    e = role_of(st.status(), "steer")
    check("steering is calibrated", e["calibrated"], str(st.status()["message"]))
    check("range is the DECLARED one, not a measured lock",
          e["range_deg"] == 900, "%d deg" % e["range_deg"])
    axis_err = math.degrees(math.acos(min(1.0, abs(sum(
        a * b for a, b in zip(e["axis"], boxes["steer"].hinge))))))
    check("discovered axis matches the real hinge", axis_err < 2.0,
          "%.3f deg" % axis_err)
    g = role_of(st.status(), "gas")
    check("the gas envelope is the DEEPEST press, not the first",
          abs(g["full_deg"] - 42) < 3.0, "%.1f deg" % g["full_deg"])

    print("\n4. steering sign: right is positive, and it holds past a full turn")
    boxes["steer"].sweep_to(370.0)                  # more than one whole turn
    wait_for(lambda: st.read_roles()[0] > 300, why="a right reading")
    v = st.read_roles()[0]
    check("370 deg right reads +370", abs(v - 370) < 8, "%.1f" % v)
    boxes["steer"].sweep_to(-370.0)
    wait_for(lambda: st.read_roles()[0] < -300, why="a left reading")
    v = st.read_roles()[0]
    check("370 deg left reads -370", abs(v + 370) < 8, "%.1f" % v)
    boxes["steer"].sweep_to(0.0)
    wait_for(lambda: abs(st.read_roles()[0]) < 5, why="centre")
    check("back to centre reads ~0", abs(st.read_roles()[0]) < 5,
          "%.1f" % st.read_roles()[0])

    print("\n5. pedals: the 0..1 that comes out of the envelope measured above")
    for role, travel, idx in (("gas", -42.0, 1), ("brake", 30.0, 2)):
        boxes[role].sweep_to(travel, step=1.0)      # press to the envelope
        wait_for(lambda: st.read_roles()[idx] > 0.9, why="a full press")
        check("%s fully pressed reads 1.0" % role,
              abs(st.read_roles()[idx] - 1.0) < 0.03,
              "%.3f" % st.read_roles()[idx])
        boxes[role].sweep_to(travel * 0.5, step=1.0)
        wait_for(lambda: st.read_roles()[idx] < 0.7, why="half travel")
        check("%s half pressed reads about half" % role,
              abs(st.read_roles()[idx] - 0.5) < 0.08,
              "%.3f" % st.read_roles()[idx])
        boxes[role].sweep_to(0.0, step=1.0)
        wait_for(lambda: st.read_roles()[idx] < 0.02, why="release")
        check("%s released reads exactly 0.0" % role,
              st.read_roles()[idx] == 0.0, "%.3f" % st.read_roles()[idx])

    print("\n6. the rig now drives the stream")
    ready, steer, throttle, brake = st.sample()
    check("sample() reports ready", ready)
    check("all three channels calibrated",
          all(role_of(st.status(), r)["calibrated"] for r in ("steer", "gas", "brake")))

    print("\n7. Ctrl+S recentres without destroying the calibration")
    boxes["steer"].sweep_to(430.0)                  # well off centre
    wait_for(lambda: st.read_roles()[0] > 400, why="an off-centre reading")
    before = role_of(st.status(), "steer")
    check("recentre accepted", st.recentre())
    wait_for(lambda: abs(st.read_roles()[0]) < 5, why="the reset")
    check("reads zero where it stands", abs(st.read_roles()[0]) < 5,
          "%.1f" % st.read_roles()[0])
    after = role_of(st.status(), "steer")
    check("range survived the reset", after["range_deg"] == before["range_deg"],
          "%d -> %d" % (before["range_deg"], after["range_deg"]))
    check("axis survived the reset", after["axis"] == before["axis"])
    # And the new centre is real: a quarter turn from here reads a quarter turn.
    boxes["steer"].sweep_to(430.0 + 90.0)
    wait_for(lambda: st.read_roles()[0] > 80, why="a quarter turn")
    check("a quarter turn from the new centre reads ~90",
          abs(st.read_roles()[0] - 90) < 8, "%.1f" % st.read_roles()[0])

    print("\n7b. the wire carries degrees from centre, clamped only by hardware")
    boxes["steer"].sweep_to(430.0 + 500.0)          # past the declared half-range
    wait_for(lambda: st.read_roles()[0] >= 449.0, timeout=10.0, why="a big angle")
    check("clamped at half the declared range, not at a measured lock",
          abs(st.read_roles()[0] - 450.0) < 1.0, "%.1f" % st.read_roles()[0])
    boxes["steer"].sweep_to(430.0 + 200.0)
    wait_for(lambda: st.read_roles()[0] < 260, why="coming back")
    check("inside the range it is the true angle, not a fraction",
          abs(st.read_roles()[0] - 200.0) < 8, "%.1f" % st.read_roles()[0])

    print("\n8. failing to neutral when a box goes away")
    boxes["gas"].sweep_to(-42.0, step=1.0)
    wait_for(lambda: st.read_roles()[1] > 0.9, why="the throttle open")
    check("throttle is open before the unplug", st.read_roles()[1] > 0.9,
          "%.3f" % st.read_roles()[1])
    boxes["gas"].unplug()
    check("throttle falls to zero", wait_for(lambda: st.read_roles()[1] == 0.0,
                                             timeout=3.0, why="neutral"),
          "%.3f" % st.read_roles()[1])
    check("the rig stops driving the stream",
          wait_for(lambda: st.sample()[0] is False, timeout=3.0))
    check("the box is dropped from the status",
          wait_for(lambda: st.status()["attached"] == 2, timeout=3.0),
          "attached=%d" % st.status()["attached"])

    print("\n9. saving, and a calibration that comes back by serial")
    check("save succeeded", st.save() is None)
    check("calibration file written", os.path.isfile(CAL))
    saved = open(CAL).read()
    check("keyed by the box's own serial",
          "7519AEA050304B4B542E3120FF17110E" in saved)

    st2 = S.SensorSet(QUIET, CAL, stale_sec=0.5, pedal_deadzone_deg=1.5)
    threading.Thread(target=st2.run, daemon=True).start()
    check("a fresh service adopts the saved calibration",
          wait_for(lambda: (role_of(st2.status(), "steer") or {}).get("calibrated"),
                   why="the saved calibration"))
    e2 = role_of(st2.status(), "steer")
    check("with the same range", e2 and e2["range_deg"] == after["range_deg"],
          "%s vs %s" % (e2 and e2["range_deg"], after["range_deg"]))
    st2.close()

    print("\n10. hotplug: the gas box comes back")
    boxes["gas"] = FakeBox("gas", boxes["gas"].mount, boxes["gas"].hinge, 33)
    check("re-detected without a restart",
          wait_for(lambda: st.status()["attached"] == 3, timeout=4.0),
          "attached=%d" % st.status()["attached"])
    check("and its saved calibration came with it",
          wait_for(lambda: role_of(st.status(), "gas")["calibrated"], timeout=3.0))
    check("so the rig drives again", wait_for(lambda: st.sample()[0], timeout=3.0))

finally:
    st.close()
    for b in boxes.values():
        try:
            b.unplug()
        except OSError:
            pass
    shutil.rmtree(TMP, ignore_errors=True)

print("\n%s\n" % ("ALL PASS" if not FAIL else "FAILURES: %s" % ", ".join(FAIL)))
sys.exit(1 if FAIL else 0)
