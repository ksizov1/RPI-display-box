#!/usr/bin/env bash
#
# Assemble the repo's runtime files into the pi-gen stage's files/payload/ dir so
# the build can copy them into the image. Run before every build (local or CI).
#
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PAYLOAD="${ROOT}/image/pi-gen/stage-adiona/00-install/files/payload"

rm -rf "$PAYLOAD"
mkdir -p "$PAYLOAD"

cp -r "${ROOT}/web"        "${PAYLOAD}/web"
cp -r "${ROOT}/controller" "${PAYLOAD}/controller"
cp -r "${ROOT}/system"     "${PAYLOAD}/system"
cp -r "${ROOT}/config"     "${PAYLOAD}/config"
cp    "${ROOT}/VERSION"    "${PAYLOAD}/VERSION"

# Bytecode a developer's local run left behind is not part of the release. It is
# gitignored, so it never reaches CI — which is precisely why it would otherwise
# only ever appear in images built on a workstation, and only sometimes.
find "${PAYLOAD}" -name '__pycache__' -type d -prune -exec rm -rf {} + 2>/dev/null || true
find "${PAYLOAD}" -name '*.pyc' -delete 2>/dev/null || true

echo "Assembled payload at: ${PAYLOAD}"
