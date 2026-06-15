# Adiona-TV display box (Raspberry Pi 5)

A headless, single-purpose appliance image for the Raspberry Pi 5. Each box is a
self-contained **router + cast receiver + HDMI output** for an Adiona-G driving
simulator session:

1. **Hosts its own offline Wi-Fi LAN** for the Meta Quest headset to join (no
   internet required, no separate router).
2. **Routes the headset to the internet via Ethernet** when an uplink cable is
   plugged in (NAT), so the Quest can validate its license. Works fine with no
   uplink — the LAN just stays offline.
3. **Boots straight into a full-screen browser** that displays the headset's live
   stream on the HDMI screen, with no on-box controls.

There is **nothing to type at show time**: power on the box, power on the
headset, and the stream appears.

---

## How it works

Adiona-G casting is a **pull model**: a casting headset runs an HTTP + WebSocket
server on `:8080` and pushes hardware-encoded H.264 to any browser that connects;
the browser decodes it with the bundled `jmuxer.js` (Media Source Extensions).
See `docs/casting-receiver.md` in the Adiona-G repo. This box reuses that exact
client — so no Adiona-G changes are needed.

Because the Pi is the Wi-Fi access point **and** the DHCP server, it always knows
which headsets are connected. The controller:

- enumerates connected devices from the DHCP lease table,
- probes each one's `:8080` (an Adiona headset only serves there *while actively
  casting*), and
- picks which headset to display, exposing the decision to the kiosk page.

This sidesteps mDNS entirely — every headset defaults to the same `adiona.local`
name, so name-based discovery is useless when more than one is around.

### Display rules

| Situation | Behavior |
|---|---|
| No headset casting | "Waiting for a headset…" screen, with the box's Wi-Fi name/password and Ethernet/internet status |
| A headset starts casting | Its stream is shown full-screen |
| **A second headset starts while one is live** | **Sticky session** — the live stream is never interrupted |
| The live headset stops / disconnects | Switches to the most-recently-connected remaining caster; if none, shows "Reconnecting…" for a grace window, then "Waiting…" |
| Stream blips | "Reconnecting…" (same as the in-app behavior) |
| Any network event while live | **Never shown over a running stream** — status only appears on the waiting/reconnecting screens |

## Networking

- `wlan0` (built-in radio) → Wi-Fi AP via NetworkManager, `ipv4.method shared`
  (DHCP + NAT in one profile). LAN is `192.168.50.0/24`, gateway `192.168.50.1`.
- `eth0` → DHCP client; the internet uplink. NAT from the AP to `eth0` is
  automatic when a cable is present. No Wi-Fi uplink / logins (by design).

### Per-box unique name

The image is generic — flash one `.img` to any number of cards. On first boot
each box derives its identity from its Wi-Fi MAC:

- **SSID** `Adiona-TV-XXXX` (XXXX = 4 hex digits of a hash of the MAC)
- **hostname** `adiona-tv-xxxx`

The Wi-Fi password is shared across the fleet (set in `config/box.conf`). Both the
SSID and password are shown on the box's waiting screen so a customer can point
the Quest at the right network.

---

## Repository layout

```
config/box.conf        Fleet-wide settings (SSID prefix, Wi-Fi password, subnet, ports)
web/                   Kiosk page (index.html) + vendored jmuxer.js decoder
controller/            adiona_controller.py — discovery, selection, /state, page server
system/
  network/             sysctl forwarding drop-in
  first-boot/          MAC→SSID/hostname provisioning oneshot (+ unit)
  kiosk/               cage + Chromium launcher (+ unit)
  controller/          controller unit
image/
  pi-gen/              custom pi-gen stage + build config
  assemble-stage.sh    stage the repo files into the pi-gen payload
  build-image.sh       local Docker build
.github/workflows/     cloud image build (no local setup needed)
```

---

## Building the image

### Option A — GitHub Actions (recommended, no local tooling)

Push this repo to GitHub, open the **Actions** tab, run **Build Adiona-TV image**
(or push a `v*` tag). Download the `adiona-tv-image` artifact (`.img.xz`) when it
finishes.

### Option B — Local build (Docker)

On Linux/macOS, or Windows via **WSL2 + Docker Desktop**:

```bash
bash image/build-image.sh
# → image/.build/pi-gen/deploy/*.img.xz
```

### Flash & first boot

1. Flash the `.img.xz` with Raspberry Pi Imager / balenaEtcher.
2. Boot the Pi (HDMI to the TV; Ethernet optional).
3. First boot self-names the box and builds the AP, then comes up on the waiting
   screen. Subsequent boots go straight to the waiting screen (~20–30 s).

### Configure the headset (once)

On the Quest: **Settings → Wi-Fi**, join the box's `Adiona-TV-XXXX` network (shown
on the waiting screen). Set it to auto-connect. Then enable
**Settings → Screen Casting** in Adiona-G. The stream appears on the TV.

---

## Configuration

Edit `config/box.conf` **before building** to change fleet defaults — most
importantly `WIFI_PASSPHRASE`. Also tunable: SSID prefix, Wi-Fi channel, LAN
subnet, scan interval, and the reconnect grace period.

> Change `WIFI_PASSPHRASE` (and the image's default `adiona`/`adiona` login in
> `image/pi-gen/config`) before deploying to customers.

---

## Developing / testing the controller

The controller is stdlib-only Python and runs from a checkout (it falls back to
the repo's `config/` and `web/` when the `/etc` and `/opt` paths are absent):

```bash
python3 controller/adiona_controller.py
# open http://127.0.0.1:8090/  and  http://127.0.0.1:8090/state
```

Off-box there are no DHCP leases, so it sits on the "waiting" screen; point
`DHCP_LEASE_FILE` at a sample lease file (and have something serving the Adiona
page on `:8080`) to exercise selection.
