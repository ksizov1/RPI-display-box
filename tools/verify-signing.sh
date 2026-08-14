#!/usr/bin/env bash
#
# Prove that the licence server's signing key is the pair of the public key in
# this repo — by fetching a real manifest and verifying it exactly the way a box
# does, with the key a box would use.
#
# This is the one part of the update chain that fails SILENTLY when it is wrong.
# A mismatched key looks perfectly healthy from both ends: the server reports
# "signed": true, boxes check in without errors, and nothing goes wrong until the
# first release, at which point every box in the fleet refuses it and keeps
# refusing it. Run this after setting up or rotating the key, and before
# publishing a release anyone is waiting for.
#
# Needs: curl, openssl, and a Python 3 (found as python3, python or py). Runs from
# a box, from WSL, or from Git Bash on Windows.
#
#     bash tools/verify-signing.sh
#     bash tools/verify-signing.sh https://staging.example/api/box/updates
#     ADIONA_UPDATE_KEY=/etc/adiona/update-key.pub bash tools/verify-signing.sh
#
# Exit codes: 0 verified, 1 rejected or unreachable.

set -uo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
URL="${1:-}"
KEY="${ADIONA_UPDATE_KEY:-}"

# Find a Python 3 that actually runs.
#
# On Windows, `python3` is usually a Microsoft Store stub that prints an advert to
# stderr and exits 49 WITHOUT running anything — so "is it on PATH" and "did it
# exit non-zero" are both useless as tests (the first says yes, and callers that
# only check `command -v` end up piping their script into a shim). The only
# reliable question is whether it answers something a working Python 3 would.
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

# Default the URL to whatever box.conf says, so this checks the endpoint the
# fleet actually talks to rather than one hardcoded here and forgotten.
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

# Ask as a box would. The version is deliberately ancient so the server has no
# reason to answer with an empty release list.
if ! curl -sS --max-time 15 -X POST "$URL" \
     -H 'Content-Type: application/json' \
     -d '{"box_id":"0000","current_version":"0.0.1","image_version":"0.0.0","os_id":"debian","os_version":"13","channel":"stable","protocol":1}' \
     -o "$TMP/resp.json"; then
	echo
	echo "FAILED: could not reach $URL"
	exit 1
fi

"$PY" - "$TMP" "$KEY" <<'PY'
import base64, json, os, subprocess, sys

tmp, key = sys.argv[1], sys.argv[2]
try:
    body = json.load(open(os.path.join(tmp, "resp.json")))
except ValueError:
    print("\nFAILED: the endpoint did not return JSON. First 200 bytes:")
    print(open(os.path.join(tmp, "resp.json")).read()[:200])
    sys.exit(1)

manifest = body.get("manifest")
sig = body.get("signature")
if manifest is None:
    print("\nFAILED: no 'manifest' in the response — is the route deployed?")
    print(json.dumps(body)[:300])
    sys.exit(1)
if not sig:
    print("\nFAILED: the manifest is NOT SIGNED.")
    print("        ADIONA_UPDATE_SIGNING_KEY is not reaching the server process.")
    print("        Boxes will report 'Updates unavailable' and install nothing.")
    sys.exit(1)

releases = manifest.get("releases") or []
print("releases : %s" % (", ".join(r.get("version", "?") for r in releases) or "(none yet)"))

# Byte-for-byte what routes/boxUpdates.js canonicalise() produces and what
# adiona-updater.py verify_manifest() reconstructs.
canon = json.dumps(manifest, sort_keys=True, separators=(",", ":")).encode("utf-8")
open(os.path.join(tmp, "msg"), "wb").write(canon)
open(os.path.join(tmp, "sig"), "wb").write(base64.b64decode(sig))

rc = subprocess.run(["openssl", "dgst", "-sha256", "-verify", key,
                     "-signature", os.path.join(tmp, "sig"),
                     os.path.join(tmp, "msg")],
                    capture_output=True)
if rc.returncode == 0:
    print("\nVERIFIED: the server is signing with the pair of this public key.")
    print("          Boxes carrying it will accept releases from this endpoint.")
    if not releases:
        print("          (The release list is empty, so there is nothing to offer yet.)")
    sys.exit(0)

print("\nREJECTED: the signature does not verify against this public key.")
print("          The server is signing with a DIFFERENT key, or the manifest was")
print("          altered in transit. Every box carrying this key will refuse every")
print("          release until they match.")
print("          Check that the vault secret ADIONA-UPDATE-SIGNING-KEY is the")
print("          private half of %s." % key)
sys.exit(1)
PY
