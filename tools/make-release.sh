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
# Then: scp the tarball to /var/www/license-api-binaries/5/ on the licence server
# and paste dist/manifest-fragment.json into data/box_versions.json there.
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
printf '%s  %s\n' "$SHA" "$(basename "$TARBALL")" > "$TARBALL.sha256"

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
cat > "$OUT/manifest-fragment.json" <<JSON
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
JSON

echo "built $TARBALL"
echo "  size   $SIZE bytes"
echo "  sha256 $SHA"
echo "  manifest fragment: $OUT/manifest-fragment.json"
echo
echo "Before pasting it into data/box_versions.json:"
echo "  * write a real 'notes' line — it is what the operator reads on the TV;"
echo "  * leave min_image empty unless this release needs a REFLASHED card. Setting"
echo "    it blocks every box upgraded by deploy.ps1, which report image 0.0.0;"
echo "  * set 'rollout' below 100 to try it on a few boxes first."
echo
echo "Then follow Adiona-license-server/docs/BOX_UPDATES.md."
