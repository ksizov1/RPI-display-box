#!/usr/bin/env python3
"""
Adiona-TV display-box controller.

Single-purpose background service for the headless Raspberry Pi cast box. It:

  1. Enumerates the headsets currently joined to the Pi's Wi-Fi AP (from the
     NetworkManager shared-mode DHCP lease file, plus the neighbour table).
  2. Probes each one's :8080 — an Adiona-G headset only serves there *while it is
     actively casting* (CastWebServer starts on startStream / stops on
     stopStream), so a serving :8080 == a live caster. This is how the box knows
     "who is connected" without relying on mDNS (every headset defaults to the
     same `adiona.local` name, so name-based discovery is useless here).
  3. Applies the sticky-session selection rule and exposes the chosen headset +
     display mode (live / reconnecting / waiting) and uplink status at /state.
     Both the kiosk page and kiosk-session.sh (which starts/stops the native RTP
     video player) drive off that endpoint.
  4. Serves the kiosk page (web/index.html + splash.png) on loopback for Chromium.

The video itself never passes through here — the headset pushes RTP/UDP straight
to the box and adiona-player.sh renders it. This service only decides *who* is
the session.

stdlib only — nothing to pip-install on the image.
"""

import json
import mimetypes
import os
import re
import socket
import subprocess
import threading
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

# ── Paths / config ───────────────────────────────────────────────────────────
HERE = os.path.dirname(os.path.abspath(__file__))
CONF_PATH = os.environ.get("ADIONA_CONF", "/etc/adiona/box.conf")
WEB_DIR = os.environ.get("ADIONA_WEB_DIR", "/opt/adiona/web")
SSID_FILE = os.environ.get("ADIONA_SSID_FILE", "/etc/adiona/ssid")
VERSION_FILE = os.environ.get("ADIONA_VERSION_FILE", "/opt/adiona/VERSION")

# Dev fallbacks so the controller runs from a checkout on a workstation.
if not os.path.exists(CONF_PATH):
    CONF_PATH = os.path.join(HERE, "..", "config", "box.conf")
if not os.path.isdir(WEB_DIR):
    WEB_DIR = os.path.join(HERE, "..", "web")
if not os.path.exists(VERSION_FILE):
    VERSION_FILE = os.path.join(HERE, "..", "VERSION")


def read_version():
    try:
        with open(VERSION_FILE) as fh:
            return fh.read().strip()
    except OSError:
        return ""


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
CAST_PORT = int(CONF.get("CAST_PORT", "8080"))
CONTROLLER_PORT = int(CONF.get("CONTROLLER_PORT", "8090"))
LEASE_FILE = CONF.get("DHCP_LEASE_FILE", "/var/lib/NetworkManager/dnsmasq-wlan0.leases")
AP_GATEWAY = CONF.get("AP_GATEWAY", "192.168.50.1")
AP_PREFIX = AP_GATEWAY.rsplit(".", 1)[0] + "."          # e.g. "192.168.50."
SCAN_INTERVAL = float(CONF.get("SCAN_INTERVAL_SECONDS", "2"))
RECONNECT_GRACE = float(CONF.get("RECONNECT_GRACE_SECONDS", "20"))
PASSPHRASE = CONF.get("WIFI_PASSPHRASE", "")
# Band the upstream dongle is pinned to, so it can never share the AP's band and
# desensitise itself against our own transmitter. "" disables the restriction.
UPLINK_BAND = CONF.get("UPLINK_BAND", "bg").strip()
PROBE_TIMEOUT = 0.6
UPLINK_IFACE = "eth0"
INTERNET_CHECK_INTERVAL = 15.0

# ── LAN wheel bridge ─────────────────────────────────────────────────────────
# adiona-wheel.py owns the USB racing wheel. We never touch the device — we only
# relay its status file to the kiosk page and drop setup commands into its
# command file. Two plain files on tmpfs instead of an IPC socket, so neither
# service can take the other down and either can restart independently.
RUN_DIR = os.environ.get("ADIONA_RUN_DIR", "/run/adiona")
WHEEL_STATUS_PATH = os.path.join(RUN_DIR, "wheel.json")
WHEEL_CMD_PATH = os.path.join(RUN_DIR, "wheel-cmd.json")
WHEEL_CMD_SEQ = [0]

# ── USB keyboard bridge ──────────────────────────────────────────────────────
# Same arrangement again, for adiona_keys.py inside the wheel service. It reads
# the box's keyboard, and while it holds that keyboard exclusively (so the
# keystrokes can go to the headset) the kiosk page cannot see a single key —
# including the F12 that opens the settings panel. So F12 arrives here as a
# counter in keys.json, and the page reports back through keys-cmd.json.
KEYS_STATUS_PATH = os.path.join(RUN_DIR, "keys.json")
KEYS_CMD_PATH = os.path.join(RUN_DIR, "keys-cmd.json")
KEYS_CMD_SEQ = [0]

# ── Software update bridge ───────────────────────────────────────────────────
# Identical arrangement to the wheel bridge above, and for the same reason: the
# updater must be able to restart — which applying an update forces it to do —
# without taking this service or the kiosk page with it.
UPDATE_STATUS_PATH = os.path.join(RUN_DIR, "update.json")
UPDATE_CMD_PATH = os.path.join(RUN_DIR, "update-cmd.json")
UPDATE_CMD_SEQ = [0]

# Monotonic time of the last /state fetch by the kiosk page. apply-update.sh
# watches this during an update's soak: it is the only signal that separates
# "adiona-kiosk is active" from "Chromium is parked on its connection-error page",
# which looks identical to systemctl and identical to a black TV.
UI_SEEN = [0.0]

# ── Shared state (guarded by LOCK) ───────────────────────────────────────────
LOCK = threading.Lock()
STATE = {
    "mode": "waiting",          # "live" | "reconnecting" | "waiting"
    "target": None,             # headset IP to stream, or None
    "target_name": None,        # friendly DHCP hostname, if known
    # True while the selected headset is ACTIVELY casting (serving :8080), as
    # opposed to merely still associated to the AP. Distinct from `mode`, which is
    # deliberately sticky so a paused stream holds the last frame instead of
    # flashing the splash. The kiosk session watches this: a false->true edge means
    # a NEW cast session (app restarted, Live Stream re-enabled, resolution
    # changed), and the video player has to be restarted to pick up the new RTP
    # stream — an already-running receiver stays locked to the previous session.
    "casting": False,
    "ssid": "",
    "passphrase": PASSPHRASE,
    "uplink": {"ethernet": None, "internet": None},
    # Summary of the USB wheel, if adiona-wheel.py is running and has one. Kept
    # to a summary here; the full axis dump lives behind /wheel so the /state
    # poll every kiosk page runs stays small.
    "wheel": {"present": False},
    # True while the settings panel is open on the kiosk page. kiosk-session.sh
    # watches this and takes the video player down for the duration: the player's
    # Wayland surface is mapped ON TOP of Chromium, so nothing the page draws is
    # visible — or focusable — while a headset is casting.
    "panel": False,
    "version": read_version(),
}

# Monotonic time of the page's last panel report, and what it said. Held outside
# STATE because the panel is only believed while the page is demonstrably alive:
# a crashed or reloading page must not be able to suppress the video for ever.
PANEL = {"open": False, "tab": "wifi", "at": 0.0}
# Monotonic time of the last /state fetch that identified itself as the PAGE
# (?ui=1). Deliberately not UI_SEEN: kiosk-session.sh polls /state as well, so
# UI_SEEN cannot tell a live page from a live shell loop.
PAGE_SEEN = [0.0]
# How long a panel report is trusted without hearing from the page again. It
# polls at 1 Hz, so this is five missed polls.
PANEL_TTL = 5.0
# How long GET /ui holds a connection open waiting for the F12 counter to move.
# Well inside any proxy or browser idle timeout, and the page re-arms instantly.
UI_POLL_HOLD = 25.0


def wheel_status():
    """Read adiona-wheel.py's status file. Absent service == no wheel."""
    try:
        with open(WHEEL_STATUS_PATH) as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return {"present": False}


def wheel_summary(status):
    """The few fields the waiting screen needs, without the axis dump."""
    return {
        "present": bool(status.get("present")),
        "name": status.get("name", ""),
        "range_deg": status.get("range_deg"),
        "mapped": bool(status.get("mapped")),
        "subscriber": status.get("subscriber"),
    }


def wheel_command(data):
    """
    Hand one setup command to adiona-wheel.py.

    Commands are whitelisted here rather than passed through, so a malformed
    kiosk-page request can never reach the device layer as something unexpected.
    """
    action = str(data.get("action", ""))
    if action not in ("arm", "assign", "range", "centre", "centre_reset",
                      "autocenter", "save"):
        return {"ok": False, "message": "unknown action"}

    cmd = {"action": action}
    if action == "assign":
        role = str(data.get("role", ""))
        if role not in ("steer", "gas", "brake"):
            return {"ok": False, "message": "bad role"}
        cmd["role"] = role
        cmd["code"] = str(data.get("code", ""))
        cmd["invert"] = bool(data.get("invert", False))
    elif action == "range":
        try:
            deg = int(data.get("deg", 0))
        except (TypeError, ValueError):
            deg = 0
        if deg not in (270, 900):
            return {"ok": False, "message": "range must be 270 or 900"}
        cmd["deg"] = deg
    elif action == "autocenter":
        try:
            pct = int(data.get("pct", -1))
        except (TypeError, ValueError):
            pct = -1
        if not 0 <= pct <= 100:
            return {"ok": False, "message": "autocenter must be 0-100"}
        cmd["pct"] = pct

    WHEEL_CMD_SEQ[0] += 1
    cmd["seq"] = WHEEL_CMD_SEQ[0]
    tmp = WHEEL_CMD_PATH + ".tmp"
    try:
        os.makedirs(RUN_DIR, exist_ok=True)
        with open(tmp, "w") as fh:
            json.dump(cmd, fh)
        os.replace(tmp, WHEEL_CMD_PATH)
    except OSError as e:
        return {"ok": False, "message": "wheel service unreachable: %s" % e}
    return {"ok": True, "message": action}


def keys_status():
    """Read the keyboard bridge's status file. Absent service == no keyboard."""
    try:
        with open(KEYS_STATUS_PATH) as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return {"present": False, "panel_seq": 0, "forwarding": False}


def keys_panel_report(data):
    """Record what the kiosk page says its settings panel is doing.

    The page is authoritative about this — it also closes on Esc and on a mouse
    click, neither of which the reader can see once it has released the keyboard.
    Nothing else is accepted here: this endpoint exists solely so the reader
    knows when to take the keyboard back."""
    is_open = bool(data.get("open"))
    tab = str(data.get("tab", "wifi"))
    if tab not in ("wifi", "wheel"):
        tab = "wifi"
    with LOCK:
        PANEL["open"] = is_open
        PANEL["tab"] = tab
        PANEL["at"] = time.monotonic()
        PAGE_SEEN[0] = PANEL["at"]          # a POST is proof the page is alive
        STATE["panel"] = is_open

    KEYS_CMD_SEQ[0] += 1
    cmd = {"open": is_open, "tab": tab, "seq": KEYS_CMD_SEQ[0]}
    tmp = KEYS_CMD_PATH + ".tmp"
    try:
        os.makedirs(RUN_DIR, exist_ok=True)
        with open(tmp, "w") as fh:
            json.dump(cmd, fh)
        os.replace(tmp, KEYS_CMD_PATH)
    except OSError as e:
        # Not fatal: with no wheel service there is no keyboard bridge to tell,
        # and the panel still works because nothing took the keys away.
        return {"ok": False, "message": "keyboard bridge unreachable: %s" % e}
    return {"ok": True}


def update_status():
    """Read adiona-updater.py's status file. Absent service == nothing to say."""
    try:
        with open(UPDATE_STATUS_PATH) as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return {"state": "idle"}


def update_summary(status):
    """The few fields the update modal needs. /state is fetched twice a second —
    by the page and by kiosk-session.sh — so the release list, the schedule and
    the manifest details stay out of it."""
    return {
        "state": status.get("state", "idle"),
        "current": status.get("current", ""),
        # Only set while an apply is in flight; see the updater's STATE.
        "from": status.get("from", ""),
        "available": status.get("available", ""),
        "notes": status.get("notes", ""),
        "progress": status.get("progress", 0.0),
        "expires_in": status.get("prompt_expires_in", 0),
        "message": status.get("message", ""),
    }


def update_command(data):
    """
    Hand one command to adiona-updater.py.

    Whitelisted rather than passed through, exactly like wheel_command(). Note
    what is NOT accepted: no URL, no download location, no version to fetch. The
    page may only answer the question the updater is already asking. `version` is
    echoed back solely so an answer to a superseded offer can be ignored — the
    page reads it from /state and cannot invent one.
    """
    action = str(data.get("action", ""))
    if action not in ("check", "accept", "decline", "cancel"):
        return {"ok": False, "message": "unknown action"}

    cmd = {"action": action, "version": str(data.get("version", ""))[:32]}
    UPDATE_CMD_SEQ[0] += 1
    cmd["seq"] = UPDATE_CMD_SEQ[0]
    tmp = UPDATE_CMD_PATH + ".tmp"
    try:
        os.makedirs(RUN_DIR, exist_ok=True)
        with open(tmp, "w") as fh:
            json.dump(cmd, fh)
        os.replace(tmp, UPDATE_CMD_PATH)
    except OSError as e:
        return {"ok": False, "message": "updater unreachable: %s" % e}
    return {"ok": True, "message": action}


def read_ssid():
    """Resolve the AP SSID: first-boot writes /etc/adiona/ssid; else ask nmcli."""
    try:
        with open(SSID_FILE, "r") as fh:
            ssid = fh.read().strip()
            if ssid:
                return ssid
    except OSError:
        pass
    try:
        out = subprocess.run(
            ["nmcli", "-t", "-f", "ACTIVE,SSID", "dev", "wifi"],
            capture_output=True, text=True, timeout=3).stdout
        for line in out.splitlines():
            if line.startswith("yes:"):
                return line.split(":", 1)[1].strip()
    except (OSError, subprocess.SubprocessError):
        pass
    return ""


# ── Discovering connected headsets ───────────────────────────────────────────
AP_IFACE = "wlan0"


def leased_clients():
    """{ip: (mac, hostname)} for current DHCP leases on the AP subnet."""
    clients = {}
    try:
        with open(LEASE_FILE, "r") as fh:
            for line in fh:
                # dnsmasq lease line: <expiry> <mac> <ip> <hostname> <clientid>
                parts = line.split()
                if len(parts) >= 4:
                    mac, ip, host = parts[1].lower(), parts[2], parts[3]
                    if ip.startswith(AP_PREFIX):
                        clients[ip] = (mac, None if host == "*" else host)
    except OSError:
        pass
    return clients


def associated_macs():
    """MACs currently associated to our Wi-Fi AP (the authoritative 'still on the
    network' signal — independent of whether the headset is actively casting)."""
    macs = set()
    try:
        out = subprocess.run(["iw", "dev", AP_IFACE, "station", "dump"],
                             capture_output=True, text=True, timeout=3).stdout
        for line in out.splitlines():
            line = line.strip()
            if line.startswith("Station "):
                macs.add(line.split()[1].lower())
    except (OSError, subprocess.SubprocessError):
        pass
    return macs


def neighbour_ips():
    """Extra candidate IPs from the kernel neighbour table (covers a headset that
    is associated but whose lease line hasn't been (re)written yet)."""
    ips = set()
    try:
        out = subprocess.run(["ip", "neigh", "show"], capture_output=True,
                             text=True, timeout=3).stdout
        for line in out.splitlines():
            parts = line.split()
            if parts and parts[0].startswith(AP_PREFIX) and "FAILED" not in line:
                ips.add(parts[0])
    except (OSError, subprocess.SubprocessError):
        pass
    return ips


def is_casting(ip):
    """True iff ip:CAST_PORT serves the Adiona viewer page (i.e. is casting)."""
    try:
        req = urllib.request.Request("http://%s:%d/" % (ip, CAST_PORT))
        with urllib.request.urlopen(req, timeout=PROBE_TIMEOUT) as resp:
            body = resp.read(512)
        return b"Adiona Live Stream" in body
    except Exception:
        return False


def scan_casters(candidates):
    """Probe all candidate IPs concurrently; return the set that is casting."""
    if not candidates:
        return set()
    active = set()
    with ThreadPoolExecutor(max_workers=min(8, len(candidates))) as pool:
        for ip, ok in zip(candidates, pool.map(is_casting, candidates)):
            if ok:
                active.add(ip)
    return active


# ── Uplink (Ethernet / Wi-Fi client) + internet ──────────────────────────────
_internet_cache = {"ok": None, "at": 0.0}
AP_CON = "Adiona-AP"


def run_nmcli(args, timeout=15):
    """Run nmcli with an ARGUMENT LIST (never a shell string), so user-supplied
    SSID/password are passed as argv and can't be injected. Returns (rc, out, err)."""
    try:
        p = subprocess.run(["nmcli"] + list(args), capture_output=True,
                           text=True, timeout=timeout)
        return p.returncode, p.stdout, p.stderr
    except (OSError, subprocess.SubprocessError) as e:
        return 1, "", str(e)


def _nmcli_split(line):
    """Split one `nmcli -t` line on unescaped ':' and unescape '\\:' / '\\\\'."""
    out, cur, i = [], "", 0
    while i < len(line):
        c = line[i]
        if c == "\\" and i + 1 < len(line):
            cur += line[i + 1]; i += 2; continue
        if c == ":":
            out.append(cur); cur = ""; i += 1; continue
        cur += c; i += 1
    out.append(cur)
    return out


# NOTE: an earlier harden_uplink() lived here and forced 802.11 power save off
# and USB autosuspend off every 30 s, on the theory that they were behind an
# unstable USB uplink. They were not. Both were measured as already applied on a
# box whose link was still failing, and the real cause turned out to be the AP
# and the dongle sharing 2.4 GHz with their antennas centimetres apart. Keeping
# the radios awake fixed nothing and cost idle power and heat, so radio power
# management is now left entirely at the driver default.


# The two supported band plans. They exist as a pair because the constraint is
# that the AP and the uplink must never share a band (see box.conf); offering the
# four combinations independently would let an operator pick a broken one.
BAND_PLANS = {
    # Default. AP on 5 GHz: it carries the video, so it gets the cleaner band and
    # the better-proven radio. Channel 36 is non-DFS, so the beacon starts at once.
    "ap5":  {"WIFI_BAND": "a",  "WIFI_CHANNEL": "36", "UPLINK_BAND": "bg"},
    # Fallback for range: 2.4 GHz carries further, for a headset well away from
    # the box. Requires an uplink dongle that can do 5 GHz.
    "ap24": {"WIFI_BAND": "bg", "WIFI_CHANNEL": "6",  "UPLINK_BAND": "a"},
}


def current_band_plan():
    return "ap24" if CONF.get("WIFI_BAND", "a") == "bg" else "ap5"


def iface_supports_5ghz(iface):
    """True/False if [iface] can use 5 GHz, None if it cannot be determined.

    Read from the phy's actual channel list rather than assumed from the model
    name: the same USB product id has shipped as both 2.4-only and dual-band, and
    guessing wrong here would offer an operator a plan that cannot work.
    """
    if not iface:
        return None
    try:
        with open("/sys/class/net/%s/phy80211/name" % iface) as fh:
            phy = fh.read().strip()
    except OSError:
        return None
    try:
        out = subprocess.run(["iw", "phy", phy, "info"], capture_output=True,
                             text=True, timeout=8).stdout
    except (OSError, subprocess.SubprocessError):
        return None
    for line in out.splitlines():
        m = re.match(r"\s*\*\s*(\d{4})(?:\.\d+)?\s*MHz", line)
        if m and 5000 <= int(m.group(1)) < 6000 and "disabled" not in line:
            return True
    return False


def set_conf_values(updates):
    """Rewrite KEY="value" lines in box.conf in place, keeping comments intact."""
    try:
        with open(CONF_PATH, "r") as fh:
            lines = fh.readlines()
    except OSError:
        return False
    remaining = dict(updates)
    out = []
    for line in lines:
        s = line.strip()
        if s and not s.startswith("#") and "=" in s:
            key = s.split("=", 1)[0].strip()
            if key in remaining:
                out.append('%s="%s"\n' % (key, remaining.pop(key)))
                continue
        out.append(line)
    for k, v in remaining.items():
        out.append('%s="%s"\n' % (k, v))
    tmp = CONF_PATH + ".tmp"
    try:
        with open(tmp, "w") as fh:
            fh.writelines(out)
        os.replace(tmp, CONF_PATH)          # atomic; never a half-written config
    except OSError:
        return False
    return True


def set_band_plan(plan):
    """Switch the AP/uplink band split and apply it live.

    Drops the headset: the AP moves to a different band, so the BSS it is joined
    to ceases to exist and it has to reassociate. Unavoidable, and the on-screen
    UI says so before the operator commits.
    """
    if plan not in BAND_PLANS:
        return {"ok": False, "message": "Unknown band plan"}
    if plan == current_band_plan():
        return {"ok": True, "message": "Already using that band plan"}

    up = wifi_uplink_iface()
    if plan == "ap24" and up and iface_supports_5ghz(up) is False:
        return {"ok": False,
                "message": "The USB adapter is 2.4 GHz only - it cannot take the uplink"}

    vals = BAND_PLANS[plan]
    if not set_conf_values(vals):
        return {"ok": False, "message": "Could not write /etc/adiona/box.conf"}
    CONF.update(vals)
    global UPLINK_BAND
    UPLINK_BAND = vals["UPLINK_BAND"]

    rc, out, err = run_nmcli(["con", "modify", AP_CON,
                              "802-11-wireless.band", vals["WIFI_BAND"],
                              "802-11-wireless.channel", vals["WIFI_CHANNEL"]],
                             timeout=20)
    if rc != 0:
        return {"ok": False, "message": (err or out).strip() or "AP update failed"}

    # Re-pin saved uplink profiles to the new band BEFORE bouncing anything, so
    # whatever reconnects comes back on the correct side of the split.
    pin_all_uplink_profiles()

    rc, out, err = run_nmcli(["-w", "30", "con", "up", AP_CON], timeout=45)
    if rc != 0:
        return {"ok": False,
                "message": "Saved, but the AP did not restart: %s"
                           % ((err or out).strip() or "unknown error")}

    # Bounce the uplink so it reassociates on its new band rather than sitting on
    # the old one until something else disturbs it. Best-effort.
    info = wifi_info()
    if up and info.get("ssid"):
        run_nmcli(["-w", "25", "con", "up", info["ssid"], "ifname", up], timeout=40)

    return {"ok": True, "message": "Band plan applied - reconnect the headset"}


def profile_ssid(con_name):
    """The SSID a saved profile actually targets (its name need not match)."""
    rc, out, _ = run_nmcli(["-t", "-f", "802-11-wireless.ssid", "con", "show", con_name])
    for line in out.splitlines():
        if line.startswith("802-11-wireless.ssid:"):
            return line.split(":", 1)[1].strip()
    return con_name


def bands_visible_for(ssid, iface):
    """Bands ('bg'/'a') this SSID is currently on air on. Empty set = don't know.

    Scanning is a property of the device, not of a connection profile, so this
    still sees a 5 GHz BSS while the profile is pinned to 2.4 GHz - which is what
    lets a box that has pinned itself into a corner notice and climb back out.
    """
    if not ssid or not iface:
        return set()
    rc, out, _ = run_nmcli(["-t", "-f", "SSID,FREQ", "dev", "wifi", "list", "ifname", iface])
    bands = set()
    for line in out.splitlines():
        p = _nmcli_split(line)
        if len(p) >= 2 and p[0] == ssid:
            m = re.match(r"\s*(\d+)", p[1])
            if m:
                f = int(m.group(1))
                if 2400 <= f < 2500:
                    bands.add("bg")
                elif 5000 <= f < 6000:
                    bands.add("a")
    return bands


def pin_uplink_profile(con_name, iface=None):
    """Apply the uplink policy to one saved Wi-Fi connection profile.

    Band pinning keeps the uplink off the AP's band, where the two antennas are
    centimetres apart and the AP deafens the dongle's receiver.

    But 802-11-wireless.band is a HARD restriction in NetworkManager, not a
    preference: a profile pinned to a band its network is not on simply never
    associates, and NM reports 'ssid-not-found' - which reads on screen as the
    network being out of range while every other device in the room is connected
    to it. So the band is only pinned when the SSID is actually on air there, and
    is otherwise explicitly CLEARED rather than left alone. Clearing matters as
    much as setting: a stored property persists until overwritten, so a box
    already pinned into a corner only recovers if something actively frees it.

    Connectivity wins over interference when the two conflict. An uplink sharing
    the AP's band degrades the stream; an uplink that cannot associate at all
    takes out licence validation and every other internet-dependent feature.

    Also pins the MAC - not a power setting: randomisation makes some APs treat
    every reassociation as a new station.

    powersave is explicitly reset to 0 (driver default), because profiles saved
    by an earlier version have powersave=2 stored in them.
    """
    band = ""                                   # empty = no restriction
    if UPLINK_BAND:
        ssid = profile_ssid(con_name)
        seen = bands_visible_for(ssid, iface or wifi_uplink_iface())
        if UPLINK_BAND in seen:
            band = UPLINK_BAND
        elif seen:
            print("[adiona-controller] '%s' is not on air on %s (seen on: %s) - "
                  "leaving its band unrestricted so it can still connect"
                  % (ssid, UPLINK_BAND, ",".join(sorted(seen))))
        # seen empty: no scan data, so we cannot tell. Fail open.

    run_nmcli(["con", "modify", con_name,
               "802-11-wireless.cloned-mac-address", "permanent",
               "802-11-wireless.powersave", "0",
               "802-11-wireless.band", band], timeout=15)


def pin_all_uplink_profiles():
    """Apply the policy to every saved non-AP Wi-Fi profile, once, at startup.

    Without this, a profile saved before the band plan existed keeps autoconnecting
    on whatever band it likes, and the operator would have to re-enter credentials
    through the on-screen setup to pick up the new policy.

    This runs on EVERY start, which is what makes pin_uplink_profile's clearing
    behaviour the self-repair path: a box left unable to associate by an earlier,
    unconditional pin frees itself on the next controller start.
    """
    iface = wifi_uplink_iface()
    rc, out, _ = run_nmcli(["-t", "-f", "NAME,TYPE", "con", "show"])
    for line in out.splitlines():
        p = _nmcli_split(line)
        if len(p) >= 2 and p[1] == "802-11-wireless" and p[0] != AP_CON:
            pin_uplink_profile(p[0], iface)


def ethernet_up():
    try:
        with open("/sys/class/net/%s/carrier" % UPLINK_IFACE, "r") as fh:
            return fh.read().strip() == "1"
    except OSError:
        return None


def wifi_uplink_iface():
    """The Wi-Fi *client* interface (USB adapter) used for the internet uplink: a
    wifi device that is neither the AP (wlan0) nor a p2p device. None if absent."""
    rc, out, _ = run_nmcli(["-t", "-f", "DEVICE,TYPE", "dev", "status"])
    for line in out.splitlines():
        p = _nmcli_split(line)
        if len(p) >= 2 and p[1] == "wifi" and p[0] != AP_IFACE and not p[0].startswith("p2p"):
            return p[0]
    return None


def default_route_dev():
    try:
        out = subprocess.run(["ip", "route", "show", "default"],
                             capture_output=True, text=True, timeout=3).stdout
        for line in out.splitlines():
            toks = line.split()
            if "dev" in toks:
                return toks[toks.index("dev") + 1]
    except (OSError, subprocess.SubprocessError):
        pass
    return None


def internet_ok():
    """Throttled reachability probe; → False when there is no default route."""
    now = time.monotonic()
    if default_route_dev() is None:
        _internet_cache.update(ok=False, at=now)
        return False
    if now - _internet_cache["at"] < INTERNET_CHECK_INTERVAL and _internet_cache["ok"] is not None:
        return _internet_cache["ok"]
    ok = False
    for host in ("1.1.1.1", "8.8.8.8"):
        try:
            with socket.create_connection((host, 53), timeout=1.0):
                ok = True
                break
        except OSError:
            continue
    _internet_cache.update(ok=ok, at=now)
    return ok


def uplink_status():
    """Internet-uplink summary for the waiting-screen status line."""
    up_if = wifi_uplink_iface()
    wifi_ssid = None
    if up_if:
        rc, out, _ = run_nmcli(["-t", "-f", "GENERAL.CONNECTION", "dev", "show", up_if])
        for line in out.splitlines():
            if line.startswith("GENERAL.CONNECTION:"):
                val = line.split(":", 1)[1].strip()
                if val and val != "--":
                    wifi_ssid = val
    route = default_route_dev()
    via = "ethernet" if route == UPLINK_IFACE else ("wifi" if route and route == up_if else None)
    return {
        "ethernet": ethernet_up(),
        "wifi_present": up_if is not None,
        "wifi_ssid": wifi_ssid,
        "via": via,
        "internet": internet_ok(),
    }


# ── Wi-Fi client setup (the on-screen overlay talks to these) ─────────────────
def wifi_info(do_rescan=False):
    """Status + saved + nearby networks for the uplink Wi-Fi adapter."""
    up = wifi_uplink_iface()
    info = {"present": up is not None, "iface": up, "state": None,
            "ssid": None, "saved": [], "scan": [],
            # Band plan, for the on-screen switch. uplink_5ghz is None when there
            # is no adapter or its capability could not be read; the UI treats
            # only an explicit False as "cannot switch".
            "band_plan": current_band_plan(),
            "uplink_5ghz": iface_supports_5ghz(up)}
    if not up:
        return info
    if do_rescan:
        run_nmcli(["dev", "wifi", "rescan", "ifname", up], timeout=20)

    rc, out, _ = run_nmcli(["-t", "-f", "DEVICE,STATE,CONNECTION", "dev", "status"])
    for line in out.splitlines():
        p = _nmcli_split(line)
        if len(p) >= 3 and p[0] == up:
            info["state"] = p[1]
            if p[1] == "connected" and p[2] != "--":
                info["ssid"] = p[2]

    rc, out, _ = run_nmcli(["-t", "-f", "NAME,TYPE", "con", "show"])
    for line in out.splitlines():
        p = _nmcli_split(line)
        if len(p) >= 2 and p[1] == "802-11-wireless" and p[0] != AP_CON:
            info["saved"].append(p[0])

    ap_ssid = read_ssid()
    rc, out, _ = run_nmcli(["-t", "-f", "SSID,SIGNAL,SECURITY", "dev", "wifi", "list", "ifname", up])
    seen = {}
    for line in out.splitlines():
        c = _nmcli_split(line)
        ssid = c[0] if c else ""
        if not ssid or ssid == ap_ssid:
            continue
        sig = int(c[1]) if len(c) > 1 and c[1].isdigit() else 0
        sec = c[2] if len(c) > 2 else ""
        if ssid not in seen or sig > seen[ssid]["signal"]:
            seen[ssid] = {"ssid": ssid, "signal": sig, "secure": bool(sec and sec != "")}
    info["scan"] = sorted(seen.values(), key=lambda x: -x["signal"])
    return info


def wifi_connect(ssid, password):
    """Join (and save, autoconnect) a network on the uplink adapter."""
    up = wifi_uplink_iface()
    if not up:
        return {"ok": False, "message": "No Wi-Fi adapter present"}
    if not ssid:
        return {"ok": False, "message": "SSID required"}
    args = ["-w", "25", "dev", "wifi", "connect", ssid]
    if password:
        args += ["password", password]
    args += ["ifname", up]
    rc, out, err = run_nmcli(args, timeout=35)
    text = (out or err).strip()
    msg = text.splitlines()[-1] if text else ("Connected" if rc == 0 else "Failed")

    if rc == 0:
        pin_uplink_profile(ssid)

    return {"ok": rc == 0, "message": msg}


def wifi_forget(ssid):
    if not ssid:
        return {"ok": False, "message": "SSID required"}
    rc, out, err = run_nmcli(["con", "delete", ssid], timeout=15)
    return {"ok": rc == 0, "message": (out or err).strip() or ("Removed" if rc == 0 else "Failed")}


# ── Selection loop ───────────────────────────────────────────────────────────
def selection_loop():
    # Selection is keyed on the headset's MAC, and "stay vs leave" is decided by
    # Wi-Fi ASSOCIATION (not by whether it's actively casting). So once a headset
    # is shown, it stays selected as long as it's on the AP — even if it pauses
    # casting (headset taken off, app backgrounded). The page then just freezes
    # the last frame. Only when the headset LEAVES the Wi-Fi do we drop to the
    # waiting screen (or switch to another headset that is casting).
    current_mac = None
    first_seen = {}             # mac -> monotonic time it became an active caster

    while True:
        leases = leased_clients()                 # {ip: (mac, host)}
        mac_to_ip = {mac: ip for ip, (mac, _h) in leases.items() if mac}
        mac_to_host = {mac: h for _ip, (mac, h) in leases.items() if mac}

        assoc = associated_macs()
        present_macs = set(mac_to_ip) & assoc      # leased AND associated to the AP
        casting_ips = scan_casters([mac_to_ip[m] for m in present_macs])
        caster_macs = {m for m in present_macs if mac_to_ip[m] in casting_ips}

        # Track when each headset first started casting (for "most recent" choice).
        now = time.monotonic()
        for m in caster_macs:
            first_seen.setdefault(m, now)
        for m in list(first_seen):
            if m not in caster_macs:
                del first_seen[m]

        if current_mac in present_macs:
            # Sticky: current headset is still on the network → keep showing it,
            # casting or not (frozen last frame while paused).
            mode = "live"
        elif caster_macs:
            # Current headset left (or none yet) AND another is casting → switch to
            # the most-recently-connected caster.
            current_mac = max(caster_macs, key=lambda m: first_seen[m])
            mode = "live"
        else:
            # Current headset left the Wi-Fi and nothing else is casting → wait.
            current_mac = None
            mode = "waiting"

        target = mac_to_ip.get(current_mac) if current_mac else None
        with LOCK:
            STATE["mode"] = mode
            STATE["target"] = target
            STATE["casting"] = bool(target) and target in casting_ips
            STATE["target_name"] = mac_to_host.get(current_mac) if current_mac else None
            STATE["ssid"] = read_ssid()
            # Re-read rather than trusting the value captured at import: an OTA
            # swaps the release under this process, and the page's "Updated to
            # v1.6.0" confirmation would otherwise report the old version until
            # something happened to restart the controller.
            STATE["version"] = read_version()
            STATE["uplink"] = uplink_status()
            STATE["wheel"] = wheel_summary(wheel_status())

        time.sleep(SCAN_INTERVAL)


# ── HTTP server (loopback only) ──────────────────────────────────────────────
class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *_):           # quiet
        pass

    def _send(self, code, body, ctype):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _ui_wait(self, query):
        """
        Long poll for the F12 counter.

        Returns as soon as adiona_keys' panel_seq differs from the value the page
        already has, or after UI_POLL_HOLD seconds. A long poll rather than a
        fast repeating one because this box sits on its waiting screen for hours
        at a time and has no business generating traffic then — the same reason
        the wheel overlay only polls while it is on screen.

        The page follows the COUNTER, not an open/closed flag: when the keyboard
        is not grabbed (no headset, panel already open, update prompt) Chromium
        receives the very same F12 we do, and a flag would then be toggled twice.

        `reader` says whether that counter can move at all — false when the wheel
        service is down, when adiona_keys failed to import, or when KEYS_ENABLED
        is 0. The page falls back to acting on the F12 keypress itself then, so a
        broken keyboard bridge cannot make the settings unreachable. It is
        returned as a change trigger of its own, so the page learns within a poll
        that the reader has appeared and stops handling F12 twice.
        """
        want = -1
        want_reader = None
        for part in query.split("&"):
            key, _, val = part.partition("=")
            if key == "seq":
                try:
                    want = int(val)
                except ValueError:
                    want = -1
            elif key == "reader":
                want_reader = val == "1"
        deadline = time.monotonic() + UI_POLL_HOLD
        while True:
            status = keys_status()
            seq = int(status.get("panel_seq", 0))
            reader = bool(status.get("enabled"))
            if seq != want or reader != want_reader or time.monotonic() >= deadline:
                return {
                    "panel_seq": seq,
                    "reader": reader,
                    "present": bool(status.get("present")),
                    "forwarding": bool(status.get("forwarding")),
                    "devices": status.get("devices", []),
                }
            time.sleep(0.05)

    def do_GET(self):
        path, _, query = self.path.partition("?")
        if path == "/state":
            now = time.monotonic()
            seen_ago = (now - UI_SEEN[0]) if UI_SEEN[0] else None
            UI_SEEN[0] = now
            with LOCK:
                # ?ui=1 marks a fetch by the kiosk PAGE rather than by
                # kiosk-session.sh, which polls this endpoint too. Only the page
                # can close the settings panel, so only the page's own polls may
                # keep `panel` alive — otherwise a crashed or reloading page
                # would suppress the video until someone power-cycled the box.
                if "ui=1" in query:
                    PAGE_SEEN[0] = now
                if STATE["panel"] and (now - PAGE_SEEN[0]) > PANEL_TTL:
                    STATE["panel"] = False
                    PANEL["open"] = False
                snapshot = dict(STATE)
            # Built per request, not in selection_loop: SCAN_INTERVAL is 2 s but
            # the page polls at 1 Hz, so an update countdown assembled in the loop
            # would tick 60, 60, 58, 58 instead of counting down smoothly.
            snapshot["update"] = update_summary(update_status())
            snapshot["ui_seen_ago"] = round(seen_ago, 2) if seen_ago is not None else None
            self._send(200, json.dumps(snapshot).encode("utf-8"), "application/json")
            return
        if path == "/wifi":
            self._send(200, json.dumps(wifi_info()).encode("utf-8"), "application/json")
            return
        if path == "/wheel":
            # Full status including the live axis dump — this is what the setup
            # overlay polls at 10 Hz while the operator is mapping the wheel.
            self._send(200, json.dumps(wheel_status()).encode("utf-8"),
                       "application/json")
            return
        if path == "/ui":
            self._send(200, json.dumps(self._ui_wait(query)).encode("utf-8"),
                       "application/json")
            return
        # Serve any asset in WEB_DIR (index.html, splash.png, …).
        # basename() strips directories, so there's no path traversal.
        name = "index.html" if path == "/" else os.path.basename(path)
        fpath = os.path.join(WEB_DIR, name)
        if name and os.path.isfile(fpath):
            ctype = mimetypes.guess_type(name)[0] or "application/octet-stream"
            try:
                with open(fpath, "rb") as fh:
                    self._send(200, fh.read(), ctype)
            except OSError:
                self._send(404, b"not found", "text/plain")
            return
        self._send(404, b"not found", "text/plain")

    def do_POST(self):
        path = self.path.split("?", 1)[0]
        if path not in ("/wifi", "/wheel", "/update", "/ui"):
            self._send(404, b"not found", "text/plain")
            return
        try:
            n = int(self.headers.get("Content-Length", "0"))
            data = json.loads(self.rfile.read(n) or b"{}")
        except (ValueError, json.JSONDecodeError):
            self._send(400, b'{"ok":false,"message":"bad request"}', "application/json")
            return
        if path == "/wheel":
            self._send(200, json.dumps(wheel_command(data)).encode("utf-8"),
                       "application/json")
            return
        if path == "/update":
            self._send(200, json.dumps(update_command(data)).encode("utf-8"),
                       "application/json")
            return
        if path == "/ui":
            self._send(200, json.dumps(keys_panel_report(data)).encode("utf-8"),
                       "application/json")
            return
        action = data.get("action")
        if action == "connect":
            res = wifi_connect(str(data.get("ssid", "")).strip(), str(data.get("password", "")))
        elif action == "forget":
            res = wifi_forget(str(data.get("ssid", "")).strip())
        elif action == "rescan":
            wifi_info(do_rescan=True)
            res = {"ok": True, "message": "rescanned"}
        elif action == "band_plan":
            res = set_band_plan(str(data.get("plan", "")).strip())
        else:
            res = {"ok": False, "message": "unknown action"}
        self._send(200, json.dumps(res).encode("utf-8"), "application/json")


def main():
    # Bring saved uplink profiles in line with the current band plan before
    # anything else; a profile saved under an older policy would otherwise keep
    # autoconnecting on the AP's band.
    try:
        pin_all_uplink_profiles()
    except Exception as e:                                  # never fatal
        print("[adiona-controller] uplink profile pin skipped: %s" % e)

    threading.Thread(target=selection_loop, daemon=True).start()
    server = ThreadingHTTPServer(("127.0.0.1", CONTROLLER_PORT), Handler)
    print("[adiona-controller] serving on http://127.0.0.1:%d (web=%s)" %
          (CONTROLLER_PORT, WEB_DIR))
    server.serve_forever()


if __name__ == "__main__":
    main()
