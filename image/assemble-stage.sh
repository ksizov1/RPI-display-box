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

echo "Assembled payload at: ${PAYLOAD}"
