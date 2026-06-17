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
install -m 0644 files/payload/VERSION "${ROOTFS_DIR}/opt/adiona/VERSION"

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

# udev: ignore pointer devices so no mouse cursor is ever drawn.
install -m 0644 files/payload/system/udev/99-adiona-no-pointer.rules \
        "${ROOTFS_DIR}/etc/udev/rules.d/99-adiona-no-pointer.rules"

# Chromium managed policy: suppress the (undismissable in kiosk) save-password bubble.
install -d "${ROOTFS_DIR}/etc/chromium/policies/managed"
install -m 0644 files/payload/system/chromium/adiona-policy.json \
        "${ROOTFS_DIR}/etc/chromium/policies/managed/adiona-policy.json"

# Plymouth boot-splash theme (marketing image + "Starting Adiona-TV..."). The
# splash image is shared with the browser waiting screen.
PLY="${ROOTFS_DIR}/usr/share/plymouth/themes/adiona-tv"
install -d "$PLY"
install -m 0644 files/payload/system/plymouth/adiona-tv/adiona-tv.plymouth "$PLY/"
install -m 0644 files/payload/system/plymouth/adiona-tv/adiona-tv.script   "$PLY/"
install -m 0644 files/payload/web/splash.png "$PLY/splash.png"

# Kernel cmdline: quiet the boot, hide the rainbow/cursor, move console logging
# off tty1 (the HDMI) so the Plymouth splash isn't interrupted by text, and keep
# the console from blanking the display before cage takes over.
for cmdline in "${ROOTFS_DIR}/boot/firmware/cmdline.txt" "${ROOTFS_DIR}/boot/cmdline.txt"; do
	[ -f "$cmdline" ] || continue
	sed -i 's/console=tty1/console=tty3/' "$cmdline"
	grep -q consoleblank "$cmdline" || sed -i 's/[[:space:]]*$/ consoleblank=0/' "$cmdline"
	grep -q ' splash' "$cmdline" || sed -i 's/[[:space:]]*$/ quiet splash plymouth.ignore-serial-consoles loglevel=3 logo.nologo vt.global_cursor_default=0/' "$cmdline"
done

# config.txt: disable the firmware rainbow splash.
for cfg in "${ROOTFS_DIR}/boot/firmware/config.txt" "${ROOTFS_DIR}/boot/config.txt"; do
	[ -f "$cfg" ] || continue
	grep -q '^disable_splash=1' "$cfg" || echo 'disable_splash=1' >> "$cfg"
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

# Plymouth: make our splash the default and rebuild the initramfs so it shows
# from early boot (Raspberry Pi OS loads Plymouth from the initramfs).
plymouth-set-default-theme -R adiona-tv || plymouth-set-default-theme adiona-tv || true
EOF
