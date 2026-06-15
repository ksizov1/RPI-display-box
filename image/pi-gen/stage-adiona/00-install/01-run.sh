#!/bin/bash -e
#
# Install the Adiona-TV payload into the image root filesystem and enable the
# services. The payload under files/payload/ is assembled from the repo by
# image/assemble-stage.sh (run before the pi-gen build).

install -d "${ROOTFS_DIR}/opt/adiona"
install -d "${ROOTFS_DIR}/etc/adiona"

cp -r files/payload/web                "${ROOTFS_DIR}/opt/adiona/web"
cp -r files/payload/controller         "${ROOTFS_DIR}/opt/adiona/controller"
cp -r files/payload/system/first-boot  "${ROOTFS_DIR}/opt/adiona/first-boot"
cp -r files/payload/system/kiosk       "${ROOTFS_DIR}/opt/adiona/kiosk"

install -m 0644 files/payload/config/box.conf "${ROOTFS_DIR}/etc/adiona/box.conf"

chmod +x "${ROOTFS_DIR}/opt/adiona/first-boot/adiona-firstboot.sh" \
         "${ROOTFS_DIR}/opt/adiona/kiosk/cage-chromium.sh"

# systemd units
install -m 0644 files/payload/system/controller/adiona-controller.service \
        "${ROOTFS_DIR}/etc/systemd/system/adiona-controller.service"
install -m 0644 files/payload/system/kiosk/adiona-kiosk.service \
        "${ROOTFS_DIR}/etc/systemd/system/adiona-kiosk.service"
install -m 0644 files/payload/system/first-boot/adiona-firstboot.service \
        "${ROOTFS_DIR}/etc/systemd/system/adiona-firstboot.service"

# sysctl: IPv4 forwarding for the router role
install -m 0644 files/payload/system/network/99-adiona-forward.conf \
        "${ROOTFS_DIR}/etc/sysctl.d/99-adiona-forward.conf"

# Stop the Linux console from blanking the HDMI output before cage takes over.
for cmdline in "${ROOTFS_DIR}/boot/firmware/cmdline.txt" "${ROOTFS_DIR}/boot/cmdline.txt"; do
	if [ -f "$cmdline" ] && ! grep -q consoleblank "$cmdline"; then
		sed -i 's/[[:space:]]*$/ consoleblank=0/' "$cmdline"
	fi
done

on_chroot << 'EOF'
set -e
# Kiosk user (pi-gen's first user = adionauser, uid 1000) needs GPU + input + tty.
usermod -aG video,render,input,tty adionauser || true

# Our kiosk service owns tty1, so keep a getty off it.
systemctl disable getty@tty1.service || true
systemctl mask getty@tty1.service || true

systemctl enable adiona-firstboot.service
systemctl enable adiona-controller.service
systemctl enable adiona-kiosk.service

# Headless appliance: boot to the console (multi-user), no display manager.
systemctl set-default multi-user.target
EOF
