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
  4. Serves the kiosk page (web/index.html + jmuxer.js) on loopback for Chromium.

stdlib only — nothing to pip-install on the image.
"""

import json
import mimetypes
import os
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
STREAM_FPS = 15                                         # CastingPlugin FPS (fixed)
PROBE_TIMEOUT = 0.6
UPLINK_IFACE = "eth0"
INTERNET_CHECK_INTERVAL = 15.0

# ── Shared state (guarded by LOCK) ───────────────────────────────────────────
LOCK = threading.Lock()
STATE = {
    "mode": "waiting",          # "live" | "reconnecting" | "waiting"
    "target": None,             # headset IP to stream, or None
    "target_name": None,        # friendly DHCP hostname, if known
    "ssid": "",
    "passphrase": PASSPHRASE,
    "uplink": {"ethernet": None, "internet": None},
    "cast_port": CAST_PORT,
    "fps": STREAM_FPS,
    "version": read_version(),
}


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
            "ssid": None, "saved": [], "scan": []}
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
            STATE["target_name"] = mac_to_host.get(current_mac) if current_mac else None
            STATE["ssid"] = read_ssid()
            STATE["uplink"] = uplink_status()

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

    def do_GET(self):
        path = self.path.split("?", 1)[0]
        if path == "/state":
            with LOCK:
                body = json.dumps(STATE).encode("utf-8")
            self._send(200, body, "application/json")
            return
        if path == "/wifi":
            self._send(200, json.dumps(wifi_info()).encode("utf-8"), "application/json")
            return
        # Serve any asset in WEB_DIR (index.html, jmuxer.js, splash.png, …).
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
        if path != "/wifi":
            self._send(404, b"not found", "text/plain")
            return
        try:
            n = int(self.headers.get("Content-Length", "0"))
            data = json.loads(self.rfile.read(n) or b"{}")
        except (ValueError, json.JSONDecodeError):
            self._send(400, b'{"ok":false,"message":"bad request"}', "application/json")
            return
        action = data.get("action")
        if action == "connect":
            res = wifi_connect(str(data.get("ssid", "")).strip(), str(data.get("password", "")))
        elif action == "forget":
            res = wifi_forget(str(data.get("ssid", "")).strip())
        elif action == "rescan":
            wifi_info(do_rescan=True)
            res = {"ok": True, "message": "rescanned"}
        else:
            res = {"ok": False, "message": "unknown action"}
        self._send(200, json.dumps(res).encode("utf-8"), "application/json")


def main():
    threading.Thread(target=selection_loop, daemon=True).start()
    server = ThreadingHTTPServer(("127.0.0.1", CONTROLLER_PORT), Handler)
    print("[adiona-controller] serving on http://127.0.0.1:%d (web=%s)" %
          (CONTROLLER_PORT, WEB_DIR))
    server.serve_forever()


if __name__ == "__main__":
    main()
