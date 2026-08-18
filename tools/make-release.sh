#!/usr/bin/env bash
#
# Build the OTA tarball a box downloads, plus the manifest fragment to paste into
# the licence server's data/box_versions.json.
#
# The tarball carries the REPO layout (web/ controller/ system/ config/ VERSION);
# adiona-updater.py translates that into the box's flattened release directory on
# arrival. Keep this payload list and image/assemble-stage.sh in step — a file
# that reaches the image but not the tarball is a file that works on a freshly
# flashed card and vanishes on the first update, which is a miserable thing to
# debug at an event.
#
# Reproducible: sorted entries, fixed mtimes, no owner/group, no __pycache__. Two
# builds of the same commit produce byte-identical archives, so a sha256 that does
# not match is always a real difference and never a timestamp.
#
# Usage:
#     bash tools/make-release.sh                 # -> dist/
#     bash tools/make-release.sh /tmp/out        # somewhere else
#
# Produces exactly two files, both ready to use as-is:
#   adiona-tv-<version>.tar.gz   -> scp to /var/www/license-api-binaries/5/
#   box_versions.json            -> REPLACES data/box_versions.json on the server
#
# box_versions.json is the COMPLETE manifest, not a fragment to splice into an
# existing one. Editing a fragment into an array by hand went wrong the first time
# it was tried: the file was replaced BY the fragment, which is still signed and
# still valid JSON but has no `releases` key, so every box reported "up to date"
# and installed nothing, silently. Emitting the whole file removes that step and
# therefore that failure.
#
# Only one release is ever listed. The box takes the newest it is offered, and a
# server holding a history of old releases serves no purpose - the artefacts stay
# on the GitHub release if an older one is ever needed.
#
# See Adiona-license-server/docs/BOX_UPDATES.md.

set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
OUT="${1:-$ROOT/dist}"
cd "$ROOT"

VERSION="$(head -1 VERSION | tr -d '[:space:]')"
[ -n "$VERSION" ] || { echo "VERSION is empty" >&2; exit 1; }
case "$VERSION" in
	[0-9]*.[0-9]*.[0-9]*) ;;
	*) echo "VERSION must be x.y.z, got '$VERSION'" >&2; exit 1 ;;
esac

PAYLOAD="web controller system config VERSION"
for item in $PAYLOAD; do
	[ -e "$item" ] || { echo "missing from the working tree: $item" >&2; exit 1; }
done

# Fixed timestamp for every entry. SOURCE_DATE_EPOCH is the reproducible-builds
# convention; CI sets it from the tagged commit's date.
: "${SOURCE_DATE_EPOCH:=$(git -C "$ROOT" log -1 --format=%ct 2>/dev/null || echo 0)}"

mkdir -p "$OUT"
TARBALL="$OUT/adiona-tv-$VERSION.tar.gz"

# --sort=name and the fixed mtime/owner are what make this reproducible; gzip -n
# keeps the archive name and timestamp out of the gzip header, which would
# otherwise change the sha256 on every build.
tar --sort=name \
    --mtime="@$SOURCE_DATE_EPOCH" \
    --owner=0 --group=0 --numeric-owner \
    --exclude='__pycache__' --exclude='*.pyc' \
    -cf - $PAYLOAD | gzip -n -9 > "$TARBALL"

SHA="$(sha256sum "$TARBALL" | cut -d' ' -f1)"
SIZE="$(wc -c < "$TARBALL" | tr -d '[:space:]')"
# No .sha256 sidecar is written. Nothing read it: the box checks the tarball
# against the sha256 inside the SIGNED manifest, which is the only copy that
# carries any authority, and a second unsigned copy alongside it added nothing.

# Sanity: the archive must contain everything adiona-updater.py's stage() insists
# on, or the failure surfaces on a box in the field rather than here.
#
# The listing is taken ONCE into a variable rather than piped per check. Under
# `set -o pipefail`, `tar -tzf … | grep -q …` reports failure even on a match:
# grep -q exits at the first hit, tar takes SIGPIPE, and pipefail propagates
# tar's non-zero status. The check would then fail on a perfectly good tarball.
LISTING="$(tar -tzf "$TARBALL")"
for want in web/index.html controller/adiona_controller.py system/kiosk/ \
            system/wheel/ system/first-boot/ system/updater/ config/box.conf VERSION; do
	case $'\n'"$LISTING" in
		*$'\n'"$want"*) ;;
		*) echo "tarball is missing $want" >&2; exit 1 ;;
	esac
done

# min_from and min_image default to EMPTY, i.e. no constraint, because both are
# exceptions and both fail closed when set.
#
# min_image especially: a box upgraded by a live deploy reports image-version
# 0.0.0 (a deploy changes /opt/adiona, never the boot configuration, so it cannot
# honestly claim to know what the card was flashed with), and an unknown image is
# treated as too old on purpose. So a min_image copied in without thinking would
# refuse the release on every box in the existing fleet — which is exactly the
# fleet a first OTA release is aimed at. Set it ONLY when a release genuinely
# depends on something a reflash provides.
BASE_URL="${ADIONA_RELEASE_BASE_URL:-https://license.drivingsimulator.com/5}"

# min_from and min_image default to EMPTY, i.e. no constraint, because both fail
# CLOSED when set and neither is normally wanted.
#
# min_image especially: a box upgraded by a live deploy reports image-version
# 0.0.0 (a deploy changes /opt/adiona, never the boot configuration, so it cannot
# honestly claim to know what the card was flashed with), and an unknown image is
# treated as too old on purpose. A min_image copied in without thinking would
# refuse the release on every box in the existing fleet - exactly the fleet a
# release is aimed at. Set it only when a release genuinely needs a reflash.
cat > "$OUT/box_versions.json" <<JSON
{
  "schema": 1,
  "channel": "stable",
  "min_client_protocol": 1,
  "releases": [
    {
      "version": "$VERSION",
      "url": "$BASE_URL/$(basename "$TARBALL")",
      "size": $SIZE,
      "sha256": "$SHA",
      "notes": "TODO: one line, shown on the TV in the update prompt",
      "min_from": "",
      "min_image": "",
      "packages": [],
      "rollout": 100
    }
  ],
  "pins": {}
}
JSON

echo "built $TARBALL"
echo "  size   $SIZE bytes"
echo "  sha256 $SHA"
echo
echo "  $OUT/box_versions.json  <- the COMPLETE manifest; replace the server's copy"
echo
echo "Before uploading, edit box_versions.json:"
echo "  * write a real 'notes' line - it is what the operator reads on the TV;"
echo "  * set 'rollout' below 100 to try it on a few boxes first."
echo
echo "Then, on the licence server:"
echo "  scp $(basename "$TARBALL") <vm>:/var/www/license-api-binaries/5/"
echo "  cp box_versions.json data/box_versions.json && git commit && git push"
echo "  bash tools/verify-signing.sh     # confirms a box would actually be offered it"
