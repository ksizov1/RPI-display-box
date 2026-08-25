#!/usr/bin/env python3
"""
Adiona-TV software updater.

Checks our licence server for a newer box release, offers it to whoever is
standing at the TV, downloads it, and hands a complete release directory to
apply-update.sh. It never swaps anything itself — see that script for the layout
and the rollback machinery.

Design notes, in the order they matter:

  1. ONCE PER POWER-UP, NEVER MID-SESSION. The box is switched on at a venue,
     used, and switched off. It asks the server exactly once per boot, offers
     whatever it finds, and then goes quiet — so a prompt can never interrupt an
     event hours in, and there is no background polling on somebody's hotspot.
     "Once per boot" means once it MANAGES to ask: at power-up the uplink is still
     associating, so the check retries until it gets a verified answer and only
     then stops. Power-cycling is the way to ask again; `--check` forces it.

  2. NOTHING HAPPENS WITHOUT A HUMAN. The check is free (a few hundred bytes); the
     download is not, and only happens after someone presses Y on the TV. No
     answer within UPDATE_PROMPT_SECONDS means no, and that version is not
     offered again until the next power-up.

  3. THE PROMPT ONLY EXISTS WHEN NOBODY IS WATCHING A HEADSET. While a headset is
     casting, the native video surface covers the kiosk page entirely, so a modal
     drawn underneath it would be invisible — and interrupting a demo to ask about
     an update is the wrong thing to do anyway. We gate on "no headset associated
     at all", not merely "not casting right now", because a headset that is
     between streams can start casting a second after the operator presses Y.

  4. A SEPARATE PROCESS, NOT A THREAD IN THE CONTROLLER. A 30-second DNS stall
     inside the controller's selection_loop would freeze /state, and both the
     kiosk page and kiosk-session.sh's poll loop drive off /state. The controller
     must never block on the internet.

  5. NO IPC SOCKETS. Status goes to /run/adiona/update.json, commands arrive via
     /run/adiona/update-cmd.json — the same two-files-on-tmpfs pattern
     adiona-wheel.py uses, for the same reason: neither service can take the
     other down and either can restart independently. That matters more here than
     anywhere else, because applying an update restarts this very process.

  6. THE SERVER IS NOT TRUSTED, ONLY THE SIGNATURE IS. These boxes join customer
     venue Wi-Fi, where whoever controls DNS would otherwise control root on the
     fleet. The manifest is verified against a public key baked into the image
     before a single field of it is acted on, and the tarball is verified against
     the sha256 inside that signed manifest.

stdlib only — nothing to pip-install on the image (same rule as the rest).

Manual operation, for when something misbehaves in the field:

    adiona-updater.py --status        what it thinks is going on
    adiona-updater.py --check         ask again without rebooting
    adiona-updater.py --apply 1.6.0   download and install, no prompt
    adiona-updater.py --rollback      go back to the previous release
    adiona-updater.py --migrate       flat layout -> release layout
    adiona-updater.py --merge-conf F  merge a shipped box.conf into the live one
"""

import argparse
import fcntl
import hashlib
import json
import os
import shutil
import socket
import ssl
import subprocess
import sys
import tarfile
import tempfile
import threading
import time
import urllib.error
import urllib.request

# ── Paths / config ───────────────────────────────────────────────────────────
HERE = os.path.dirname(os.path.abspath(__file__))
CONF_PATH = os.environ.get("ADIONA_CONF", "/etc/adiona/box.conf")
RUN_DIR = os.environ.get("ADIONA_RUN_DIR", "/run/adiona")
ETC_DIR = os.environ.get("ADIONA_ETC", "/etc/adiona")
STATE_DIR = os.environ.get("ADIONA_STATE_DIR", "/var/lib/adiona")
ROOT = os.environ.get("ADIONA_ROOT", "/opt/adiona")
SSID_FILE = os.environ.get("ADIONA_SSID_FILE", os.path.join(ETC_DIR, "ssid"))
PUBKEY = os.environ.get("ADIONA_UPDATE_KEY", os.path.join(ETC_DIR, "update-key.pub"))

# Dev fallback so this runs from a checkout on a workstation.
if not os.path.exists(CONF_PATH):
    CONF_PATH = os.path.join(HERE, "..", "..", "config", "box.conf")


def load_conf(path):
    """Parse the box.conf KEY="VALUE" shell file into a dict (no shell needed)."""
    conf = {}
    try:
        with open(path, "r", encoding="utf-8") as fh:
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
ENABLED = CONF.get("UPDATE_ENABLED", "1") not in ("0", "no", "false", "")
MANIFEST_URL = CONF.get("UPDATE_MANIFEST_URL",
                        "https://license.drivingsimulator.com/api/box/updates")
CHANNEL = CONF.get("UPDATE_CHANNEL", "stable")
PROMPT_SECONDS = max(10, int(CONF.get("UPDATE_PROMPT_SECONDS", "60")))
KEEP_RELEASES = max(2, int(CONF.get("UPDATE_KEEP_RELEASES", "3")))
ALLOW_PACKAGES = CONF.get("UPDATE_ALLOW_PACKAGES", "1") not in ("0", "no", "false", "")
CONTROLLER_PORT = int(CONF.get("CONTROLLER_PORT", "8090"))

STATUS_PATH = os.path.join(RUN_DIR, "update.json")
CMD_PATH = os.path.join(RUN_DIR, "update-cmd.json")
PHASE_PATH = os.path.join(RUN_DIR, "apply.phase")
LOCK_PATH = os.path.join(RUN_DIR, "updater.lock")
MARKER_PATH = os.path.join(ETC_DIR, "update-pending")
IMAGE_VERSION_PATH = os.path.join(ETC_DIR, "image-version")
RESULT_PATH = os.path.join(STATE_DIR, "last-apply.json")
DOWNLOAD_DIR = os.path.join(STATE_DIR, "downloads")
RELEASES_DIR = os.path.join(ROOT, "releases")
APPLY_SCRIPT = os.path.join(ROOT, "updater", "apply-update.sh")
STATE_URL = "http://127.0.0.1:%d/state" % CONTROLLER_PORT

APPLY_PROTOCOL = 1                  # bumped only when apply-update.sh's contract changes
HTTP_TIMEOUT = 10.0
MANIFEST_MAX_BYTES = 256 * 1024
# The box checks ONCE per power-up, so these govern only how long it keeps trying
# to land that one check when the network is not ready yet — not a polling cycle.
RETRY_MIN = 30.0
RETRY_MAX = 15 * 60.0
# How long after launching the applier we keep saying "Installing" before its
# marker appears. It writes the marker a few seconds in (after the package and
# box.conf phases, plus a deliberate pause so the screen can paint), and until
# then the absence of a marker means "not started", not "finished".
APPLY_START_GRACE = 120.0
# How long the result banner stays up before the waiting screen comes back.
# Success is a confirmation - the operator already watched the install and only
# needs to see that it finished, and a couple of those seconds are spent on the
# page reload that the new version triggers. Failure is different: it is the only
# place the reason is shown, nobody is necessarily watching when it happens, and
# whoever walks up next has to be able to read it.
RESULT_HOLD_OK = 6.0
RESULT_HOLD_FAIL = 60.0
# Refuse to download unless the card has room for the tarball, the unpacked tree,
# and headroom. Running /opt out of space mid-unpack is recoverable but ugly.
SPACE_MULTIPLIER = 3
SPACE_HEADROOM = 200 * 1024 * 1024
TICK = 1.0

RUNNING = True
# False for every CLI subcommand, True only for the daemon — see write_status().
PUBLISH = [False]


def log(msg):
    print("[adiona-updater] %s" % msg, flush=True)


# ── Shared state ─────────────────────────────────────────────────────────────
LOCK = threading.Lock()

STATE = {
    "schema": 1,
    "seq": 0,
    # idle | checking | available | prompting | downloading | verifying |
    # staged | applying | health | ok | failed | rolled_back | blocked
    "state": "idle",
    "current": "",
    # The version an in-flight apply is replacing. It cannot be derived from
    # "current": the swap flips VERSION well before the apply finishes, so a
    # screen built from current_version() reads "v1.6.14 -> v1.6.14" for most of
    # the install. The applier's marker is the only thing that still knows.
    "from": "",
    "available": "",
    "notes": "",
    "size": 0,
    "progress": 0.0,
    "prompt_expires_in": 0,
    "last_check_at": 0,
    # True once this boot's single check has produced a verified answer. There is
    # no "next check": the box asks once per power-up and then goes quiet.
    "checked": False,
    "last_result": None,
    "layout": "flat",
    "internet": False,
    "releases": [],
    "blocked_reason": "",
    "message": "",
}


def set_state(**kw):
    with LOCK:
        STATE["seq"] += 1
        STATE.update(kw)


def write_status():
    # Only the long-running service owns /run/adiona/update.json. The wheel
    # service reads it once a second for the AB02 flags and the controller relays
    # it into /state, so a one-shot CLI run publishing its own view over it would
    # briefly blank whatever the operator is reading on the TV — including a live
    # update prompt. Every CLI subcommand clears this; main() sets it back before
    # entering the daemon loop.
    if not PUBLISH[0]:
        return
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
    """Return (seq, command dict) if a NEW command is waiting, else (last_seq, None).

    New means the sequence CHANGED, not that it went up — see the same function
    in adiona-wheel.py for why. Here the command being lost is the operator's Y
    or N to an update prompt, answered once and never repeated."""
    try:
        with open(CMD_PATH) as fh:
            cmd = json.load(fh)
    except (OSError, ValueError):
        return last_seq, None
    seq = int(cmd.get("seq", 0))
    if seq == last_seq:
        return last_seq, None
    return seq, cmd


# There is deliberately NO persistent decline/blacklist state here any more.
#
# It existed to bridge PERIODIC checks: with the box checking every few hours, a
# refusal had to be remembered or the same prompt would reappear all afternoon.
# The box now checks once per power-up, so "declined" simply means "no more
# offers until the next boot", which needs no file — and power-cycling is then
# the obvious, discoverable way to ask again.
#
# A release that fails to download is likewise just not retried until the next
# boot, and nothing is downloaded at all until somebody answers the prompt.

# ── Box identity ─────────────────────────────────────────────────────────────

def marker_field(name):
    """Read one KEY=value line out of /etc/adiona/update-pending.

    The applier owns that file; it names the version being installed, which is
    how this process knows what it is watching after being restarted mid-apply.
    """
    try:
        with open(MARKER_PATH, encoding="utf-8") as fh:
            for line in fh:
                key, _, val = line.strip().partition("=")
                if key == name:
                    return val
    except OSError:
        pass
    return ""


def read_first_line(path, default=""):
    try:
        with open(path, encoding="utf-8") as fh:
            return fh.read().strip().splitlines()[0].strip()
    except (OSError, IndexError):
        return default


def box_id():
    """The 4 hex chars adiona-firstboot.sh derives from the MAC. The only
    fleet-unique identifier the box has — its IP is the same on every box."""
    ssid = read_first_line(SSID_FILE)
    if not ssid:
        return ""
    return ssid.rsplit("-", 1)[-1].upper()


def current_version():
    return read_first_line(os.path.join(ROOT, "VERSION"))


def image_version():
    """The VERSION the card was flashed with. Distinct from the running version
    once OTA is in play, and the only way a release can say 'this one needs a
    reflash' — nothing here can change the kernel cmdline, config.txt, the
    Plymouth theme registration or the tty1 masking, which live only in the
    image build."""
    return read_first_line(IMAGE_VERSION_PATH)


def os_release():
    fields = {}
    try:
        with open("/etc/os-release") as fh:
            for line in fh:
                key, _, val = line.strip().partition("=")
                if key:
                    fields[key] = val.strip().strip('"').strip("'")
    except OSError:
        pass
    return fields.get("ID", ""), fields.get("VERSION_ID", "")


def layout():
    """"release" once migrated, "flat" as the image build leaves it."""
    return "release" if os.path.islink(os.path.join(ROOT, "current")) else "flat"


def list_releases():
    try:
        return sorted(d for d in os.listdir(RELEASES_DIR)
                      if os.path.isdir(os.path.join(RELEASES_DIR, d)))
    except OSError:
        return []


# ── The controller is the source of truth for uplink and headset state ───────

def controller_state():
    """The controller already decides what "the box has internet" means, caches
    it, and shows it on the waiting screen. Asking it rather than probing again
    keeps one definition — the alternative is a splash saying "No internet" while
    the update modal says "Downloading"."""
    try:
        req = urllib.request.Request(STATE_URL)
        with urllib.request.urlopen(req, timeout=2.0) as resp:
            return json.loads(resp.read(64 * 1024))
    except Exception:
        return {}


def box_is_idle(state):
    """True when nobody is using this box: no headset associated at all.

    Deliberately stricter than "not casting". A headset that is on the AP but
    between streams can start casting a second after the operator presses Y, and
    killing the video mid-demo to install an update is the worst possible moment.
    """
    return not state.get("target") and not state.get("casting")


# ── Manifest ─────────────────────────────────────────────────────────────────

def fetch_manifest():
    """POST what we are to the licence server; get back the signed release list.

    Certificate verification is explicit rather than relied upon: urllib has
    verified by default since PEP 476, but this is the one request on the box
    where silently losing that would matter, so it says so out loud. Never
    _create_unverified_context here, whatever a captive portal does.
    """
    os_id, os_ver = os_release()
    payload = json.dumps({
        "box_id": box_id(),
        "current_version": current_version(),
        "image_version": image_version(),
        "os_id": os_id,
        "os_version": os_ver,
        "channel": CHANNEL,
        "protocol": APPLY_PROTOCOL,
    }).encode("utf-8")
    req = urllib.request.Request(
        MANIFEST_URL, data=payload,
        headers={"Content-Type": "application/json",
                 "User-Agent": "adiona-tv-box/%s" % (current_version() or "0")})
    ctx = ssl.create_default_context()
    with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT, context=ctx) as resp:
        raw = resp.read(MANIFEST_MAX_BYTES + 1)
    if len(raw) > MANIFEST_MAX_BYTES:
        raise ValueError("manifest larger than %d bytes" % MANIFEST_MAX_BYTES)
    return json.loads(raw)


def verify_manifest(body):
    """Check the detached signature over the canonical manifest bytes.

    HTTPS proves we reached whoever holds the certificate for that name; it does
    not prove the payload is ours, and these boxes join networks run by strangers.
    Signing is the difference between "trust the venue's DNS" and not.

    openssl is used rather than a Python library because the image installs no pip
    packages, and openssl is on any Debian.
    """
    sig_b64 = body.get("signature")
    payload = body.get("manifest")
    if not sig_b64 or payload is None:
        return False, "manifest is not signed"
    if not os.path.exists(PUBKEY):
        return False, "no update key at %s" % PUBKEY

    import base64
    try:
        sig = base64.b64decode(sig_b64)
    except Exception:
        return False, "signature is not valid base64"

    # Canonical form must match exactly what the server signed: compact
    # separators, sorted keys, UTF-8.
    canon = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")

    with tempfile.TemporaryDirectory() as td:
        sig_path = os.path.join(td, "sig")
        msg_path = os.path.join(td, "msg")
        with open(sig_path, "wb") as fh:
            fh.write(sig)
        with open(msg_path, "wb") as fh:
            fh.write(canon)
        try:
            rc = subprocess.run(
                ["openssl", "dgst", "-sha256", "-verify", PUBKEY,
                 "-signature", sig_path, msg_path],
                capture_output=True, timeout=15)
        except (OSError, subprocess.SubprocessError) as e:
            return False, "openssl failed: %s" % e
    if rc.returncode != 0:
        return False, "signature does not verify"
    return True, ""


def semver(text):
    """(1, 5, 0) from "1.5.0". Anything unparseable sorts lowest — a version we
    cannot compare must never look newer than what we are running."""
    parts = str(text).strip().split(".")
    out = []
    for p in parts[:3]:
        digits = ""
        for ch in p:
            if not ch.isdigit():
                break
            digits += ch
        out.append(int(digits) if digits else 0)
    while len(out) < 3:
        out.append(0)
    return tuple(out)


def choose_release(manifest):
    """Pick the release this box should be running, or None.

    Returns (release_dict, blocked_reason). A blocked_reason with no release means
    there IS something newer but this box cannot take it — worth saying on screen,
    because "needs re-imaging" is a thing a field tech can act on and silence is not.
    """
    cur = current_version()
    releases = manifest.get("releases") or []
    if not releases:
        return None, ""

    bid = box_id()
    pinned = (manifest.get("pins") or {}).get(bid)
    if pinned:
        for rel in releases:
            if rel.get("version") == pinned:
                # A pin may name an OLDER version: that is the recall path, and it
                # must be honoured rather than filtered out as "not newer".
                if rel.get("version") == cur:
                    return None, ""
                return rel, ""
        return None, "pinned to %s, which the server did not offer" % pinned

    best = None
    blocked = ""
    for rel in releases:
        ver = rel.get("version", "")
        if not ver or semver(ver) <= semver(cur):
            continue
        if rel.get("channel") and rel["channel"] != CHANNEL:
            continue
        if semver(cur) < semver(rel.get("min_from", "0.0.0")):
            blocked = "v%s cannot be installed over v%s directly" % (ver, cur)
            continue
        # Unknown counts as too old, deliberately. An absent or 0.0.0
        # image-version means a box that predates the file or was upgraded by a
        # live deploy, and in both cases we genuinely cannot tell what boot
        # configuration the card carries. A release that declares min_image is
        # saying it depends on something only a reflash can provide, so "we do not
        # know" has to mean "do not install" — the alternative fails open on
        # exactly the boxes least able to cope with it.
        if rel.get("min_image"):
            img = image_version()
            if not img or semver(img) < semver(rel["min_image"]):
                blocked = ("v%s needs a card imaged with v%s or newer (this one reports %s)"
                           % (ver, rel["min_image"], img or "nothing"))
                continue
        if int(rel.get("rollout", 100)) < 100 and not in_rollout(bid, rel):
            continue
        if best is None or semver(ver) > semver(best.get("version", "")):
            best = rel
    return best, ("" if best else blocked)


def in_rollout(bid, rel):
    """Deterministic per-box canary selection, so a box either is or is not in a
    staged rollout and does not flip on every check."""
    try:
        pct = int(rel.get("rollout", 100))
    except (TypeError, ValueError):
        return True
    if pct >= 100:
        return True
    if pct <= 0:
        return False
    key = ("%s|%s" % (bid, rel.get("version", ""))).encode("utf-8")
    return (int(hashlib.sha256(key).hexdigest()[:8], 16) % 100) < pct


# ── Download and staging ─────────────────────────────────────────────────────

def enough_space(size):
    try:
        st = os.statvfs(ROOT)
    except OSError:
        return True, 0
    free = st.f_bavail * st.f_frsize
    need = size * SPACE_MULTIPLIER + SPACE_HEADROOM
    return free >= need, free


def download(url, size, sha256, dest, on_progress):
    """Fetch to `dest`, hashing as we go. No Range resume: restarting from zero is
    simpler and the artifact is small, and a hard cap at 110% of the declared size
    means a misconfigured or hostile server cannot fill the card."""
    limit = int(size * 1.1) + 4096 if size else 64 * 1024 * 1024
    digest = hashlib.sha256()
    got = 0
    ctx = ssl.create_default_context()
    req = urllib.request.Request(url, headers={"User-Agent": "adiona-tv-box"})
    with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT, context=ctx) as resp, \
            open(dest, "wb") as fh:
        while True:
            chunk = resp.read(64 * 1024)
            if not chunk:
                break
            got += len(chunk)
            if got > limit:
                raise ValueError("download exceeded the declared size")
            digest.update(chunk)
            fh.write(chunk)
            if size:
                on_progress(min(1.0, got / float(size)))
    if size and got != size:
        raise ValueError("expected %d bytes, got %d" % (size, got))
    actual = digest.hexdigest()
    if sha256 and actual.lower() != str(sha256).lower():
        raise ValueError("sha256 mismatch (expected %s, got %s)" % (sha256, actual))
    return actual


def stage(tarball, version, packages):
    """Unpack the OTA tarball into a complete release directory.

    The tarball carries the REPO layout (web/ controller/ system/ config/ VERSION);
    the box runs a flattened one (web/ controller/ kiosk/ wheel/ first-boot/
    updater/). That translation happens here so apply-update.sh only ever deals
    with a directory that is already correct — see its header for the contract.
    """
    final = os.path.join(RELEASES_DIR, version)
    partial = final + ".partial"
    shutil.rmtree(partial, ignore_errors=True)
    shutil.rmtree(final, ignore_errors=True)
    os.makedirs(partial, exist_ok=True)

    with tempfile.TemporaryDirectory(dir=STATE_DIR) as td:
        with tarfile.open(tarball, "r:gz") as tf:
            # filter="data" is not optional. An unfiltered extractall running as
            # root is an arbitrary-write primitive: absolute paths, ../ escapes and
            # symlink targets all land wherever the archive says.
            tf.extractall(td, filter="data")

        def need(rel_path, dest_name):
            src = os.path.join(td, *rel_path)
            if not os.path.exists(src):
                raise ValueError("tarball is missing %s" % "/".join(rel_path))
            dst = os.path.join(partial, dest_name)
            if os.path.isdir(src):
                shutil.copytree(src, dst)
            else:
                shutil.copy2(src, dst)

        need(("web",), "web")
        need(("controller",), "controller")
        need(("system", "kiosk"), "kiosk")
        need(("system", "wheel"), "wheel")
        need(("system", "first-boot"), "first-boot")
        need(("system", "updater"), "updater")
        need(("VERSION",), "VERSION")
        os.makedirs(os.path.join(partial, "config"), exist_ok=True)
        need(("config", "box.conf"), os.path.join("config", "box.conf"))

        # Every .service the release ships, from wherever under system/ it lives.
        units = os.path.join(partial, "units")
        os.makedirs(units, exist_ok=True)
        sysdir = os.path.join(td, "system")
        for dirpath, _dirs, files in os.walk(sysdir):
            for name in files:
                if name.endswith(".service"):
                    shutil.copy2(os.path.join(dirpath, name), os.path.join(units, name))

    staged_version = read_first_line(os.path.join(partial, "VERSION"))
    if staged_version != version:
        raise ValueError("tarball says v%s but the manifest offered v%s"
                         % (staged_version, version))

    with open(os.path.join(partial, "packages"), "w") as fh:
        for pkg in (packages or []):
            if isinstance(pkg, str) and pkg.strip():
                fh.write(pkg.strip() + "\n")

    # tar preserves modes, but a tarball built on a checkout that lost the exec
    # bit (a Windows clone, say) would produce a release nothing can start.
    for subdir in ("kiosk", "first-boot", "updater", "wheel"):
        d = os.path.join(partial, subdir)
        if not os.path.isdir(d):
            continue
        for name in os.listdir(d):
            if name.endswith((".sh", ".py")):
                os.chmod(os.path.join(d, name), 0o755)

    os.rename(partial, final)          # only now is the release dir real
    return final


# ── box.conf merge ───────────────────────────────────────────────────────────

def merge_box_conf(shipped_path):
    """Add keys a new release introduced, without touching anything the operator set.

    config/box.conf is ~110 lines of comment to ~40 of setting; the comments ARE
    the product for that file, so a new key is carried in with the run of comment
    lines directly above it in the shipped file. Existing values are never
    overwritten — the band plan and the wheel settings are the operator's, not
    ours — and keys upstream has dropped are left alone, because deleting
    somebody's line is worse than carrying a dead one.

    NOTE FOR ANYONE EXTENDING THIS: the manifest must never be able to set
    box.conf values. This file is `source`d as shell by three root scripts and
    values are written unescaped, so a server-settable value would be remote root
    on the whole fleet. Only the tarball's shipped box.conf — reviewed and tagged
    — feeds this merge.
    """
    try:
        with open(shipped_path, encoding="utf-8") as fh:
            shipped_lines = fh.readlines()
    except OSError as e:
        log("cannot read shipped box.conf %s: %s" % (shipped_path, e))
        return False

    live = load_conf(CONF_PATH)
    try:
        with open(CONF_PATH, encoding="utf-8") as fh:
            live_lines = fh.readlines()
    except OSError as e:
        log("cannot read %s: %s" % (CONF_PATH, e))
        return False

    additions = []
    changed_defaults = []
    pending_comments = []
    for line in shipped_lines:
        stripped = line.strip()
        if stripped.startswith("#"):
            pending_comments.append(line)
            continue
        if not stripped:
            pending_comments = []
            continue
        if "=" not in stripped:
            pending_comments = []
            continue
        key = stripped.split("=", 1)[0].strip()
        value = stripped.split("=", 1)[1].strip().strip('"').strip("'")
        if key in live:
            if live[key] != value:
                changed_defaults.append((key, value, live[key]))
        else:
            additions.append((key, pending_comments[:], line))
        pending_comments = []

    for key, new_default, mine in changed_defaults:
        log("box.conf: upstream default for %s is now '%s'; yours stays '%s'"
            % (key, new_default, mine))
    if not additions:
        log("box.conf: no new keys")
        _append_conf_log(changed_defaults, [])
        return True

    out = list(live_lines)
    if out and not out[-1].endswith("\n"):
        out.append("\n")
    out.append("\n")
    for key, comments, line in additions:
        log("box.conf: adding %s" % key)
        out.extend(comments)
        out.append(line if line.endswith("\n") else line + "\n")

    try:
        shutil.copyfile(CONF_PATH, CONF_PATH + ".prev")
    except OSError:
        pass
    tmp = CONF_PATH + ".tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as fh:
            fh.writelines(out)
        os.replace(tmp, CONF_PATH)      # atomic; never a half-written config
    except OSError as e:
        log("could not write %s: %s" % (CONF_PATH, e))
        return False
    _append_conf_log(changed_defaults, [k for k, _c, _l in additions])
    return True


def _append_conf_log(changed_defaults, added):
    try:
        os.makedirs(STATE_DIR, exist_ok=True)
        with open(os.path.join(STATE_DIR, "box.conf.ota.log"), "a", encoding="utf-8") as fh:
            stamp = time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime())
            for key in added:
                fh.write("%s added %s\n" % (stamp, key))
            for key, new_default, mine in changed_defaults:
                fh.write("%s default %s: '%s' upstream, yours '%s' (kept)\n"
                         % (stamp, key, new_default, mine))
    except OSError:
        pass


# ── Handing off to apply-update.sh ───────────────────────────────────────────

def run_apply(args, detached=True):
    """Launch apply-update.sh.

    Detached through systemd-run, not as a child: applying an update restarts
    adiona-updater, and `systemctl restart` kills the unit's whole cgroup. A
    subprocess (even with setsid) is in that cgroup and dies mid-swap.

    Every flag below is load-bearing, and two of them were wrong in a way that
    only showed up on hardware:

      --collect                unloads the transient unit whatever its exit
                               status; without it the next run with the same
                               name fails with "unit already exists".
      TimeoutStartSec=infinity NOT 0. In systemd, 0 means "time out
                               IMMEDIATELY", not "no timeout" - `infinity` is
                               the documented way to disable it. With 0 the unit
                               was SIGTERMed in the same second it started, and
                               the whole apply never ran a single line. Verified
                               on the box: `systemd-run … TimeoutStartSec=0
                               /bin/sleep 3` fails.
      --no-block               systemd-run otherwise WAITS for the start job,
                               and for Type=oneshot that job only completes when
                               the script exits - i.e. it would block for the
                               entire apply (two minutes plus a 90 s soak),
                               blowing the timeout below and making the updater
                               report failure while the apply was still running.
                               Measured: without it, a `sleep 5` unit takes 5 s
                               to return; with it, ~0 s.
    """
    if not os.path.exists(APPLY_SCRIPT):
        return False, "apply script missing at %s" % APPLY_SCRIPT
    if not detached:
        rc = subprocess.run(["bash", APPLY_SCRIPT] + args)
        return rc.returncode == 0, ("" if rc.returncode == 0 else "exit %d" % rc.returncode)
    cmd = ["systemd-run", "--unit=adiona-apply", "--service-type=oneshot", "--collect",
           "--no-block",
           "--property=TimeoutStartSec=infinity",
           "--property=SyslogIdentifier=adiona-apply",
           "--property=WorkingDirectory=/",
           "bash", APPLY_SCRIPT] + args
    try:
        rc = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.SubprocessError) as e:
        return False, "systemd-run failed: %s" % e
    if rc.returncode != 0:
        return False, (rc.stderr or rc.stdout or "systemd-run exited %d" % rc.returncode).strip()
    return True, ""


def apply_release(version, release_dir):
    # Logged before the call, not after: if we die inside the D-Bus round trip,
    # the journal must still show that an apply was intended.
    log("requesting apply of v%s from %s" % (version, release_dir))
    set_state(state="applying", available=version, message="Installing v%s" % version)
    write_status()
    ok, err = run_apply(["--protocol", str(APPLY_PROTOCOL),
                         "--from", current_version(), "--to", version,
                         "--release-dir", release_dir])
    if not ok:
        log("could not launch the applier: %s" % err)
        set_state(state="failed", message="Could not start the update: %s" % err)
    return ok


# ── The update cycle ─────────────────────────────────────────────────────────

class Updater(object):
    """One check per power-up.

    The box is not a server; it is switched on at a venue, used, and switched off.
    So it asks the licence server exactly once per boot, offers whatever it finds,
    and then stays out of the way for the rest of the session — no background
    polling, and no possibility of a prompt appearing hours into an event.

    "Once per boot" has to mean "once it manages to ask", though, not "once, 60
    seconds after systemd started me". At power-up NetworkManager is still
    associating, DHCP has not finished and DNS may not resolve for a while; a
    single early attempt would simply miss. So the check is retried until it
    actually reaches the server and gets a verified answer, and only then does the
    updater go quiet for the rest of the boot.
    """

    def __init__(self):
        self.checked = False            # a verified answer has been obtained
        self.retry_at = 0.0             # monotonic; 0 = try as soon as there is a link
        self.retry_delay = RETRY_MIN
        self.candidate = None           # release dict awaiting an answer
        self.staged_dir = None          # release dir ready to apply
        self.prompt_deadline = None     # time.monotonic() when the offer lapses
        self.pending_accept = False     # accepted, waiting for the box to go idle
        self.declined = set()           # versions refused THIS BOOT; cleared by a reboot
        self.waiting_logged = False     # so "no uplink yet" is said once, not every tick
        self.last_cmd_seq = 0
        # Which apply we are watching, so a result belonging to a PREVIOUS one is
        # never mistaken for it. See poll_apply().
        self.applying_version = ""
        self.saw_marker = False
        self.apply_started = 0.0

    def done_checking(self):
        """A verified answer. Nothing further happens until the box is restarted."""
        self.checked = True
        self.retry_at = 0.0
        set_state(checked=True, last_check_at=int(time.time()))

    def retry_later(self, reason):
        """Could not reach or trust the server. Back off and try for the one check
        again — the box may still be bringing its uplink up."""
        log(reason)
        self.retry_at = time.monotonic() + self.retry_delay
        self.retry_delay = min(RETRY_MAX, self.retry_delay * 2)

    # ── the check ──
    def check(self, force=False, cstate=None):
        # Refresh the box's own identity first. run() does this every tick, but
        # check() is also reachable from `--check` in a one-shot process where
        # nothing else ever has — and a diagnostic that reports the STATE defaults
        # (current "", layout "flat") as if they were findings is worse than one
        # that reports nothing, because "flat" is a plausible answer.
        set_state(current=current_version(), layout=layout(), releases=list_releases())
        if not ENABLED:
            set_state(state="blocked", blocked_reason="updates disabled in box.conf")
            self.done_checking()
            return

        if cstate is None:
            cstate = controller_state()
        online = bool((cstate.get("uplink") or {}).get("internet"))
        set_state(internet=online)
        if not online and not force:
            # Not a failure, and not the boot's check either. The uplink may take
            # minutes or an hour to come up — a venue's Wi-Fi, a phone hotspot
            # someone gets round to enabling, a cable plugged in later — and the
            # box waits for it indefinitely rather than giving up.
            #
            # retry_at is cleared rather than pushed forward, so the decision is
            # re-made on the NEXT TICK. run() already has the controller's state in
            # hand, so this costs a dictionary lookup once a second, and the check
            # fires within about a second of the uplink coming up rather than up to
            # a polling interval later.
            if not self.waiting_logged:
                log("no uplink yet — will check as soon as one appears")
                self.waiting_logged = True
            set_state(state="idle", message="")
            self.retry_at = 0.0
            # A link that has just come up deserves a fresh retry budget; failures
            # against the previous one say nothing about this one.
            self.retry_delay = RETRY_MIN
            return

        if self.waiting_logged:
            log("uplink is up — checking for updates")
            self.waiting_logged = False

        set_state(state="checking", message="")
        write_status()
        try:
            body = fetch_manifest()
        except (urllib.error.URLError, socket.timeout, OSError, ValueError) as e:
            # Reached for it and failed — DNS not up, captive portal, server down.
            # Still no answer, so this does not count as the boot's check.
            set_state(state="idle")
            self.retry_later("update check failed: %s" % e)
            return

        good, why = verify_manifest(body)
        if not good:
            # A security refusal, not a network problem. Retrying cannot make an
            # untrusted answer trustworthy, so this counts as the boot's check and
            # the box simply runs on with what it has.
            set_state(state="blocked", blocked_reason=why, available="")
            log("MANIFEST REJECTED: %s" % why)
            self.done_checking()
            return

        manifest = body.get("manifest") or {}
        set_state(blocked_reason="")
        self.done_checking()

        rel, blocked = choose_release(manifest)
        if rel is None:
            set_state(state="idle", available="", notes="", size=0,
                      blocked_reason=blocked)
            if blocked:
                log("update available but not installable: %s" % blocked)
            else:
                log("up to date (v%s)" % current_version())
            return

        version = rel.get("version", "")
        if version in self.declined:
            # Only reachable via a forced re-check (--check or the kiosk's check
            # command) after someone already said no in this session.
            log("v%s was declined this session — not re-offering until a restart" % version)
            set_state(state="idle", available="")
            return

        self.candidate = rel
        set_state(state="available", available=version,
                  notes=str(rel.get("notes", ""))[:400],
                  size=int(rel.get("size", 0) or 0), progress=0.0)
        log("v%s is available (v%s installed)" % (version, current_version()))

    # ── the offer ──
    def maybe_prompt(self, cstate):
        if self.candidate is None or self.prompt_deadline is not None:
            return
        with LOCK:
            if STATE["state"] != "available":
                return
        if not box_is_idle(cstate):
            return                       # a headset is here; the page is covered
        self.prompt_deadline = time.monotonic() + PROMPT_SECONDS
        set_state(state="prompting", prompt_expires_in=PROMPT_SECONDS)
        log("offering v%s on screen for %d s" % (self.candidate.get("version"), PROMPT_SECONDS))

    def tick_prompt(self, cstate):
        if self.prompt_deadline is None:
            return
        if not box_is_idle(cstate):
            # A headset turned up mid-offer. Withdraw the prompt WITHOUT counting
            # it as a refusal: this boot gets exactly one offer, and burning it
            # while nobody could even see the screen would mean the box silently
            # never updates. It is re-shown as soon as the box is idle again.
            log("headset present — withdrawing the offer, will re-ask when idle")
            self.prompt_deadline = None
            set_state(state="available", prompt_expires_in=0)
            return
        left = self.prompt_deadline - time.monotonic()
        if left > 0:
            set_state(prompt_expires_in=int(left + 0.5))
            return
        version = self.candidate.get("version", "") if self.candidate else ""
        log("no answer in %d s — staying on v%s until the next power-up"
            % (PROMPT_SECONDS, current_version()))
        self.prompt_deadline = None
        if version:
            self.declined.add(version)
        self.candidate = None
        set_state(state="idle", available="", prompt_expires_in=0,
                  message="Update postponed")

    # ── accept ──
    def accept(self, version):
        if self.candidate is None or self.candidate.get("version") != version:
            return {"ok": False, "message": "no such offer"}
        self.prompt_deadline = None
        self.pending_accept = True
        set_state(state="downloading", progress=0.0, prompt_expires_in=0,
                  message="Downloading v%s" % version)
        threading.Thread(target=self._download_and_stage, daemon=True).start()
        return {"ok": True, "message": "downloading"}

    def _download_and_stage(self):
        rel = self.candidate
        if rel is None:
            return
        version = rel.get("version", "")
        size = int(rel.get("size", 0) or 0)
        url = rel.get("url", "")
        sha = rel.get("sha256", "")

        room, free = enough_space(size)
        if not room:
            need = (size * SPACE_MULTIPLIER + SPACE_HEADROOM) // (1024 * 1024)
            msg = "Not enough space for the update (needs %d MB, %d MB free)" % (
                need, free // (1024 * 1024))
            log(msg)
            set_state(state="failed", message=msg)
            self.candidate = None
            self.pending_accept = False
            return

        os.makedirs(DOWNLOAD_DIR, exist_ok=True)
        dest = os.path.join(DOWNLOAD_DIR, "adiona-tv-%s.tar.gz" % version)
        try:
            download(url, size, sha, dest, lambda p: set_state(progress=round(p, 3)))
            set_state(state="verifying", progress=1.0, message="Verifying v%s" % version)
            packages = rel.get("packages") if ALLOW_PACKAGES else []
            release_dir = stage(dest, version, packages)
        except (urllib.error.URLError, socket.timeout, OSError, ValueError,
                tarfile.TarError) as e:
            reason = str(e)
            log("update to v%s failed: %s" % (version, reason))
            shutil.rmtree(os.path.join(RELEASES_DIR, version + ".partial"),
                          ignore_errors=True)
            # Not retried until the next power-up. Nothing is downloaded without
            # someone pressing Y, so a bad artifact costs a hotspot nothing while
            # it sits there.
            self.declined.add(version)
            self.candidate = None
            self.pending_accept = False
            set_state(state="failed", available="", progress=0.0,
                      message="Update failed — still on v%s" % current_version())
            return
        finally:
            # The tarball has been unpacked (or has failed); either way keeping it
            # costs card space for nothing.
            try:
                os.remove(dest)
            except OSError:
                pass

        self.staged_dir = release_dir
        set_state(state="staged", message="Ready to install v%s" % version)
        log("v%s staged at %s" % (version, release_dir))

    def maybe_apply(self, cstate):
        if not self.pending_accept or not self.staged_dir:
            return
        if not box_is_idle(cstate):
            set_state(message="Update will install when the session ends")
            return
        version = self.candidate.get("version", "") if self.candidate else ""
        release_dir = self.staged_dir
        self.pending_accept = False
        self.staged_dir = None
        self.candidate = None
        self.applying_version = version
        self.saw_marker = False
        self.apply_started = time.monotonic()
        set_state(**{"from": current_version()})
        apply_release(version, release_dir)

    # ── commands from the kiosk page, relayed by the controller ──
    def command(self, cmd):
        action = cmd.get("action", "")
        version = str(cmd.get("version", ""))
        if action == "check":
            # An explicit ask-again, without a reboot.
            self.checked = False
            self.retry_at = 0.0
            self.retry_delay = RETRY_MIN
            return
        if action == "decline":
            if self.candidate is not None:
                v = self.candidate.get("version", "")
                if not version or version == v:
                    log("v%s declined at the keyboard — not re-offering until a restart" % v)
                    self.declined.add(v)
                    self.candidate = None
                    self.prompt_deadline = None
                    set_state(state="idle", available="", prompt_expires_in=0,
                              message="Update postponed")
            return
        if action == "accept":
            res = self.accept(version)
            if not res["ok"]:
                log("accept ignored: %s" % res["message"])
            return
        if action == "cancel":
            self.pending_accept = False
            self.staged_dir = None
            set_state(state="idle", message="")
            return
        log("unknown command %r" % action)

    # ── watch an apply through to its end ──
    def poll_apply(self):
        """Follow the applier to its outcome.

        Two traps, both of which produced a wrong screen on real hardware:

        1. resume() latches "an update is in flight" from the marker at startup -
           correct, because the applier restarts this process mid-apply - but the
           applier then FINISHES and clears the marker, and nothing looked again.
           A successful update sat on "Installing" indefinitely.

        2. The absence of the marker does NOT mean "finished". systemd-run is
           fire-and-forget, so this can run within a second of launching the
           applier, while it is still in `preparing`/`config` and has not written
           the marker yet - roughly a three-second window. Treating that as
           completion made it publish the PREVIOUS apply's result: a 1.6.10 ->
           1.6.11 update briefly announced "Updated to v1.6.9".

        So a result is only ours if it names the version we launched, and until we
        have seen the marker at least once we assume the applier is still starting.
        """
        if os.path.exists(MARKER_PATH):
            self.saw_marker = True
            if not self.applying_version:
                # Restarted mid-apply: the marker itself says what is being installed.
                self.applying_version = marker_field("to")
            phase = read_first_line(PHASE_PATH, "applying")
            set_state(state=("health" if phase == "health" else "applying"),
                      available=self.applying_version,
                      message="Installing — do not remove power",
                      **{"from": STATE.get("from") or marker_field("from")})
            return

        result = None
        try:
            with open(RESULT_PATH) as fh:
                result = json.load(fh)
        except (OSError, ValueError):
            result = None

        ours = bool(result) and (not self.applying_version
                                 or result.get("to") == self.applying_version)
        if not ours:
            # Either the applier has not started writing yet, or this result belongs
            # to an earlier update. Keep showing "Installing" while it could still
            # be the former; the marker appearing will confirm it.
            if not self.saw_marker and (time.monotonic() - self.apply_started) < APPLY_START_GRACE:
                return
            log("apply finished with no result of its own; returning to idle")
            self.applying_version = ""
            set_state(state="idle", message="", available="", **{"from": ""})
            return

        set_state(current=current_version(), layout=layout(), releases=list_releases(),
                  last_result=result)
        if result.get("ok"):
            set_state(state="ok", available="",
                      message="Updated to v%s" % result.get("to", current_version()))
            log("update to v%s completed" % result.get("to"))
        else:
            set_state(state="failed", available="",
                      message="Update failed — still on v%s" % current_version())
            log("update to v%s failed: %s" % (result.get("to"), result.get("reason")))
        set_state(**{"from": ""})
        self.applying_version = ""
        self.saw_marker = False

    # ── reconstruct after the restart every apply causes ──
    def resume(self):
        """An apply restarts this process half way through its own job. Work out
        from the on-disk marker and result file what is going on, so the screen
        does not flicker back to 'waiting' in the middle of an install."""
        set_state(current=current_version(), layout=layout(),
                  releases=list_releases())
        if os.path.exists(MARKER_PATH):
            phase = read_first_line(PHASE_PATH, "applying")
            # Seed what poll_apply() needs, so the result it eventually reads is
            # matched against the version actually being installed.
            self.saw_marker = True
            self.applying_version = marker_field("to")
            self.apply_started = time.monotonic()
            set_state(state="applying" if phase not in ("health",) else "health",
                      available=self.applying_version,
                      message="Installing — do not remove power",
                      **{"from": marker_field("from")})
            log("an update is in flight (phase %s, installing v%s)"
                % (phase, self.applying_version or "?"))
            return
        try:
            with open(RESULT_PATH) as fh:
                result = json.load(fh)
        except (OSError, ValueError):
            return
        set_state(last_result=result)
        age = time.time() - float(result.get("at", 0) or 0)
        if age > 900:
            return                      # old news; do not put it back on screen
        if result.get("ok"):
            set_state(state="ok", available="",
                      message="Updated to v%s" % result.get("to", current_version()))
            log("previous update to v%s succeeded" % result.get("to"))
        else:
            set_state(state="failed", available="",
                      message="Update failed — still on v%s" % current_version())
            log("previous update to v%s failed: %s"
                % (result.get("to"), result.get("reason")))

    # ── main loop ──
    def run(self):
        self.resume()
        # An update is mid-flight (this process was restarted BY the applier). Do
        # not check: the box is in the middle of changing version, and the answer
        # would be about a version it is no longer running.
        if os.path.exists(MARKER_PATH):
            self.checked = True
        clear_at = 0.0
        while RUNNING:
            cstate = controller_state()
            set_state(current=current_version(), layout=layout(),
                      releases=list_releases(),
                      internet=bool((cstate.get("uplink") or {}).get("internet")))

            self.last_cmd_seq, cmd = read_command(self.last_cmd_seq)
            if cmd:
                try:
                    self.command(cmd)
                except Exception as e:       # a bad command must never kill us
                    log("command failed: %s" % e)

            with LOCK:
                state = STATE["state"]

            # An apply is owned by a separate process that outlives restarts of
            # this one, so its progress has to be READ, not remembered. Without
            # this the screen keeps whatever resume() latched at startup.
            if state in ("applying", "health"):
                self.poll_apply()
                with LOCK:
                    state = STATE["state"]

            # Transient result banners clear themselves so the waiting screen does
            # not carry "Updated to v1.6.0" for the rest of the event.
            if state in ("ok", "failed"):
                if clear_at == 0.0:
                    clear_at = time.monotonic() + (
                        RESULT_HOLD_OK if state == "ok" else RESULT_HOLD_FAIL)
                elif time.monotonic() > clear_at:
                    clear_at = 0.0
                    set_state(state="idle", message="")
            else:
                clear_at = 0.0

            if state not in ("downloading", "verifying", "applying", "health"):
                # One check per power-up. `checked` is set once a verified answer
                # arrives, and nothing clears it except an explicit ask-again or a
                # restart of this service — which is what a power cycle is.
                if not self.checked and time.monotonic() >= self.retry_at:
                    try:
                        # Hand over the state run() already fetched, so the
                        # waiting-for-an-uplink path is a dict lookup per tick.
                        self.check(cstate=cstate)
                    except Exception as e:
                        set_state(state="idle")
                        self.retry_later("check raised: %s" % e)
                self.maybe_prompt(cstate)
                self.tick_prompt(cstate)
                self.maybe_apply(cstate)

            write_status()
            time.sleep(TICK)


# ── CLI ──────────────────────────────────────────────────────────────────────

def cli_status(show_published=True):
    if show_published:
        try:
            with open(STATUS_PATH) as fh:
                print(fh.read())
        except OSError:
            print("no status file at %s (is adiona-updater running?)" % STATUS_PATH)
    print("installed : v%s" % current_version())
    print("image     : v%s" % (image_version() or "unknown"))
    print("layout    : %s" % layout())
    print("releases  : %s" % ", ".join(list_releases()))
    print("box id    : %s" % (box_id() or "unknown"))
    if os.path.exists(MARKER_PATH):
        print("!! an update is in flight or was interrupted (%s)" % MARKER_PATH)


def cli_apply(version):
    """Download and install a named version with no prompt. The field escape
    hatch: an operator over SSH has already answered the question the prompt asks."""
    up = Updater()
    body = fetch_manifest()
    good, why = verify_manifest(body)
    if not good:
        print("manifest rejected: %s" % why, file=sys.stderr)
        return 1
    for rel in (body.get("manifest") or {}).get("releases") or []:
        if rel.get("version") == version:
            up.candidate = rel
            break
    else:
        print("v%s is not offered by the server" % version, file=sys.stderr)
        return 1
    up._download_and_stage()
    if not up.staged_dir:
        print("staging failed — see the journal", file=sys.stderr)
        return 1
    ok, err = run_apply(["--protocol", str(APPLY_PROTOCOL), "--from", current_version(),
                         "--to", version, "--release-dir", up.staged_dir], detached=False)
    print("apply %s%s" % ("succeeded" if ok else "failed", "" if ok else ": " + err))
    return 0 if ok else 1


def main():
    global RUNNING

    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--status", action="store_true", help="print what the updater thinks")
    ap.add_argument("--check", action="store_true", help="check now and exit")
    ap.add_argument("--apply", metavar="VERSION", help="install a version now, no prompt")
    ap.add_argument("--rollback", action="store_true", help="revert to the previous release")
    ap.add_argument("--migrate", action="store_true", help="flat layout -> release layout")
    ap.add_argument("--merge-conf", metavar="FILE", help="merge a shipped box.conf into the live one")
    args = ap.parse_args()

    if args.status:
        cli_status()
        return 0
    if args.merge_conf:
        return 0 if merge_box_conf(args.merge_conf) else 1
    if args.migrate:
        ok, err = run_apply(["--migrate"], detached=False)
        if not ok:
            print(err, file=sys.stderr)
        return 0 if ok else 1
    if args.rollback:
        ok, err = run_apply(["--rollback"], detached=False)
        if not ok:
            print(err, file=sys.stderr)
        return 0 if ok else 1
    if args.apply:
        return cli_apply(args.apply)
    if args.check:
        up = Updater()
        up.check(force=True)
        # Deliberately NOT write_status(). /run/adiona/update.json belongs to the
        # running service: the wheel service reads it for the AB02 flags and the
        # controller relays it into /state, so publishing this one-shot process's
        # view over it would briefly blank a prompt the operator is reading. Print
        # what this run found instead.
        with LOCK:
            print(json.dumps(STATE, indent=2))
        cli_status(show_published=False)
        return 0

    os.makedirs(RUN_DIR, exist_ok=True)
    os.makedirs(STATE_DIR, exist_ok=True)
    # One updater. Without this a stray manual run would fight the service over
    # the status file and, worse, over the download directory.
    lock = open(LOCK_PATH, "w")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        log("another updater is already running")
        return 1

    # Past every CLI subcommand, so this process is the service: it may publish.
    PUBLISH[0] = True

    log("started — %s, one check per power-up, prompt %d s, channel %s"
        % ("enabled" if ENABLED else "DISABLED", PROMPT_SECONDS, CHANNEL))
    try:
        Updater().run()
    except KeyboardInterrupt:
        RUNNING = False
    return 0


if __name__ == "__main__":
    sys.exit(main())
