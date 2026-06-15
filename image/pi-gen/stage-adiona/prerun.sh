#!/bin/bash -e
# Standard pi-gen stage prerun: start this stage from a copy of the previous
# stage's root filesystem.
if [ ! -d "${ROOTFS_DIR}" ]; then
	copy_previous
fi
