#!/usr/bin/env bash
#
# Build the flashable Adiona-TV .img locally with Docker (pi-gen).
#
# Requirements: Docker + git on a Linux/macOS host, or Windows via WSL2 with
# Docker Desktop. The simplest no-local-setup path is the GitHub Actions
# workflow (.github/workflows/build-image.yml), which publishes the .img as a
# build artifact — use that if you don't want to install Docker.
#
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
# 64-bit images are built from pi-gen's arm64 branch.
PIGEN_REF="${PIGEN_REF:-arm64}"
WORK="${ROOT}/image/.build"
PIGEN="${WORK}/pi-gen"

echo "==> Assembling payload"
"${ROOT}/image/assemble-stage.sh"

echo "==> Fetching pi-gen (${PIGEN_REF})"
mkdir -p "$WORK"
if [ ! -d "$PIGEN/.git" ]; then
	git clone --depth 1 --branch "$PIGEN_REF" https://github.com/RPi-Distro/pi-gen.git "$PIGEN"
fi

echo "==> Installing custom stage + config"
rm -rf "${PIGEN}/stage-adiona"
cp -r "${ROOT}/image/pi-gen/stage-adiona" "${PIGEN}/stage-adiona"
cp "${ROOT}/image/pi-gen/config" "${PIGEN}/config"
# Only our stage should export an image.
touch "${PIGEN}/stage2/SKIP_IMAGES"
# pi-gen skips stages that contain SKIP (we want full lite), ensure our scripts
# are executable.
chmod +x "${PIGEN}/stage-adiona/prerun.sh" "${PIGEN}/stage-adiona/00-install/01-run.sh"

echo "==> Building image with Docker (this takes a while)"
cd "$PIGEN"
CONTINUE=1 PRESERVE_CONTAINER=1 ./build-docker.sh

echo "==> Done. Image(s):"
ls -lh "${PIGEN}/deploy/" 2>/dev/null || true
