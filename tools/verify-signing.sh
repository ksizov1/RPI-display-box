#!/usr/bin/env bash
#
# Ask the licence server what it would tell a box, and check three things a box
# checks: is the manifest signed by the pair of our public key, is it the right
# SHAPE, and what would a box actually decide to do with it.
#
# The signature is only the first of those, and on its own it is misleading. A
# perfectly signed manifest with `releases: []` - or with a release entry pasted in
# as the whole file, so `releases` is missing entirely - is accepted by every box
# and installs nothing, silently. That combination has already cost one field test:
# the server said VERIFIED, the boxes said "up to date", and nothing anywhere said
# the manifest was malformed.
#
# Needs: curl, openssl, and a Python 3 (found as python3, python or py). Runs from
# a box, from WSL, or from Git Bash on Windows.
#
#     bash tools/verify-signing.sh
#     bash tools/verify-signing.sh https://staging.example/api/box/updates
#     ADIONA_BOX_VERSION=1.6.2 bash tools/verify-signing.sh    # decide as that box
#
# Exit codes: 0 all good, 1 rejected, unreachable, or malformed.

set -uo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
URL="${1:-}"
KEY="${ADIONA_UPDATE_KEY:-}"

# The version to reason as. Defaults to this checkout's VERSION, which is normally
# what the fleet is on or about to be on.
BOXVER="${ADIONA_BOX_VERSION:-$(head -1 "$ROOT/VERSION" 2>/dev/null | tr -d '[:space:]')}"

# Find a Python 3 that actually runs.
#
# On Windows, `python3` is usually a Microsoft Store stub that prints an advert to
# stderr and exits 49 WITHOUT running anything - so "is it on PATH" and "did it
# exit non-zero" are both useless as tests. Ask it something only a working
# Python 3 answers.
PY=""
for cand in python3 python py; do
	command -v "$cand" >/dev/null 2>&1 || continue
	if [ "$("$cand" -c 'import sys; print(sys.version_info[0])' 2>/dev/null | tr -d '\r\n')" = "3" ]; then
		PY="$cand"
		break
	fi
done
if [ -z "$PY" ]; then
	echo "no working Python 3 found (tried python3, python, py)" >&2
	echo "on Windows, install Python or run this under WSL:" >&2
	echo "    wsl bash tools/verify-signing.sh" >&2
	exit 1
fi

# Default the URL to whatever box.conf says, so this checks the endpoint the fleet
# actually talks to rather than one hardcoded here and forgotten.
if [ -z "$URL" ]; then
	for conf in /etc/adiona/box.conf "$ROOT/config/box.conf"; do
		[ -f "$conf" ] || continue
		URL="$(sed -n 's/^UPDATE_MANIFEST_URL="\(.*\)"$/\1/p' "$conf" | head -1)"
		[ -n "$URL" ] && break
	done
fi
[ -n "$URL" ] || { echo "no manifest URL given and none found in box.conf" >&2; exit 1; }

if [ -z "$KEY" ]; then
	for k in "$ROOT/system/updater/update-key.pub" /etc/adiona/update-key.pub; do
		[ -f "$k" ] && { KEY="$k"; break; }
	done
fi
[ -n "$KEY" ] || { echo "no public key found" >&2; exit 1; }

echo "endpoint : $URL"
echo "key      : $KEY"
echo "deciding : as a box running v${BOXVER:-?}"

if grep -qi 'PLACEHOLDER' "$KEY"; then
	echo
	echo "REFUSED: $KEY is still the placeholder shipped with the repo."
	echo "         No box will install anything until it is replaced by the public"
	echo "         half of the key the licence server signs with."
	exit 1
fi
if ! openssl pkey -pubin -in "$KEY" -noout 2>/dev/null; then
	echo "REFUSED: $KEY is not a readable public key." >&2
	exit 1
fi
echo "keysize  : $(openssl pkey -pubin -in "$KEY" -noout -text 2>/dev/null | head -1 | tr -d '\n')"

TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT

if ! curl -sS --max-time 15 -X POST "$URL" \
     -H 'Content-Type: application/json' \
     -d "{\"box_id\":\"0000\",\"current_version\":\"${BOXVER:-0.0.1}\",\"image_version\":\"0.0.0\",\"os_id\":\"debian\",\"os_version\":\"13\",\"channel\":\"stable\",\"protocol\":1}" \
     -o "$TMP/resp.json"; then
	echo
	echo "FAILED: could not reach $URL"
	exit 1
fi

"$PY" - "$TMP" "$KEY" "${BOXVER:-0.0.1}" <<'PY'
import base64, json, os, subprocess, sys

tmp, key, boxver = sys.argv[1], sys.argv[2], sys.argv[3]
try:
    body = json.load(open(os.path.join(tmp, "resp.json")))
except ValueError:
    print("\nFAILED: the endpoint did not return JSON. First 200 bytes:")
    print(open(os.path.join(tmp, "resp.json")).read()[:200])
    sys.exit(1)

manifest = body.get("manifest")
sig = body.get("signature")
if manifest is None:
    print("\nFAILED: no 'manifest' in the response - is the route deployed?")
    print(json.dumps(body)[:300])
    sys.exit(1)
if not sig:
    print("\nFAILED: the manifest is NOT SIGNED.")
    print("        ADIONA_UPDATE_SIGNING_KEY is not reaching the server process.")
    print("        Boxes will report 'Updates unavailable' and install nothing.")
    sys.exit(1)

# ── 1. signature ─────────────────────────────────────────────────────────────
# Byte-for-byte what routes/boxUpdates.js canonicalise() produces and what
# adiona-updater.py verify_manifest() reconstructs.
canon = json.dumps(manifest, sort_keys=True, separators=(",", ":")).encode("utf-8")
open(os.path.join(tmp, "msg"), "wb").write(canon)
open(os.path.join(tmp, "sig"), "wb").write(base64.b64decode(sig))
rc = subprocess.run(["openssl", "dgst", "-sha256", "-verify", key,
                     "-signature", os.path.join(tmp, "sig"),
                     os.path.join(tmp, "msg")], capture_output=True)
if rc.returncode != 0:
    print("\nREJECTED: the signature does not verify against this public key.")
    print("          The server is signing with a DIFFERENT key, or the manifest was")
    print("          altered in transit. Every box carrying this key will refuse every")
    print("          release until they match.")
    print("          Check that the vault secret ADIONA-UPDATE-SIGNING-KEY is the")
    print("          private half of %s." % key)
    sys.exit(1)
print("\nsignature: verified against this public key")

# ── 2. shape ─────────────────────────────────────────────────────────────────
# A signed-but-malformed manifest is accepted by every box and installs nothing.
# The mistake that actually happens: pasting the release FRAGMENT in as the whole
# file, so the release keys end up at the top level and `releases` disappears.
RELEASE_KEYS = {"version", "url", "sha256", "size", "min_from", "min_image",
                "packages", "rollout", "notes"}
problems = []

if "releases" not in manifest:
    stray = RELEASE_KEYS & set(manifest.keys())
    if stray:
        problems.append(
            "'releases' is MISSING and the top level instead holds release keys\n"
            "          (%s).\n"
            "          box_versions.json has been replaced BY the fragment. The\n"
            "          fragment is the object that goes INSIDE the releases array:\n"
            "            { \"schema\": 1, \"channel\": \"stable\",\n"
            "              \"releases\": [ { ...the fragment... } ], \"pins\": {} }"
            % ", ".join(sorted(stray)))
    else:
        problems.append("'releases' is missing from the manifest")
elif not isinstance(manifest["releases"], list):
    problems.append("'releases' is %s, not a list" % type(manifest["releases"]).__name__)

releases = manifest.get("releases") if isinstance(manifest.get("releases"), list) else []
for i, r in enumerate(releases):
    if not isinstance(r, dict):
        problems.append("releases[%d] is not an object" % i)
        continue
    for f in ("version", "url", "sha256", "size"):
        if not r.get(f):
            problems.append("releases[%d] is missing '%s'" % (i, f))

if problems:
    print("\nMALFORMED: the manifest is signed, but boxes will install nothing.\n")
    for p in problems:
        print("  * %s" % p)
    print("\n          Every box would report 'up to date' - silently, because an")
    print("          empty release list is a legitimate answer.")
    sys.exit(1)

print("shape    : ok (%d release%s)" % (len(releases), "" if len(releases) == 1 else "s"))

# ── 3. what a box would decide ───────────────────────────────────────────────
def semver(text):
    out = []
    for part in str(text).strip().split(".")[:3]:
        digits = ""
        for ch in part:
            if not ch.isdigit():
                break
            digits += ch
        out.append(int(digits) if digits else 0)
    while len(out) < 3:
        out.append(0)
    return tuple(out)

cur = semver(boxver)
print("\na box on v%s would see:" % boxver)
offered = None
for r in releases:
    v = r.get("version", "")
    why = None
    if semver(v) <= cur:
        why = "not newer than v%s" % boxver
    elif r.get("channel") and r["channel"] != manifest.get("channel", "stable"):
        why = "channel %r, box follows %r" % (r["channel"], manifest.get("channel", "stable"))
    elif r.get("min_from") and cur < semver(r["min_from"]):
        why = "needs to be installed over v%s or newer" % r["min_from"]
    elif r.get("min_image"):
        why = ("min_image=%s BLOCKS every box upgraded by deploy.ps1 (they report "
               "image 0.0.0). Leave it empty unless the release needs a reflash."
               % r["min_image"])
    elif int(r.get("rollout", 100) or 100) < 100:
        why = "rollout %s%%, so only some boxes" % r.get("rollout")
    if why:
        print("   v%-8s skipped  - %s" % (v, why))
    else:
        print("   v%-8s OFFERED" % v)
        if offered is None or semver(v) > semver(offered.get("version", "")):
            offered = r

warn = []
if offered and "TODO" in str(offered.get("notes", "")):
    warn.append("notes is still the generated placeholder - it is what the operator reads on the TV")
if offered and not offered.get("notes"):
    warn.append("notes is empty - the prompt will show no explanation")
for w in warn:
    print("\n   warning: %s" % w)

if offered is None:
    print("\nNOTHING TO OFFER: a box on v%s would report 'up to date'." % boxver)
    print("                  If that is not what you intended, the reasons above say why.")
    sys.exit(1)

print("\nVERIFIED: signed by the pair of this public key, well-formed, and a box on")
print("          v%s would be offered v%s." % (boxver, offered["version"]))
PY
