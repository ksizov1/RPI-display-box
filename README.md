# Adiona-TV display box (Raspberry Pi 5)

A headless, single-purpose appliance image for the Raspberry Pi 5. Each box is a
self-contained **router + cast receiver + HDMI output** for an Adiona-G driving
simulator session:

1. **Hosts its own offline Wi-Fi LAN** for the Meta Quest headset to join (no
   internet required, no separate router).
2. **Routes the headset to the internet via Ethernet** when an uplink cable is
   plugged in (NAT), so the Quest can validate its license. Works fine with no
   uplink — the LAN just stays offline.
3. **Boots straight into a full-screen video player** showing the headset's live
   stream on the HDMI screen, with no on-box controls.

There is **nothing to type at show time**: power on the box, power on the
headset, and the stream appears.

---

## How it works

Adiona-G casting is a **push model**: a casting headset hardware-encodes H.264
and sends it as RTP over **UDP**, unicast to this box on `:5004`. The box plays it
natively with GStreamer.

UDP is the whole design, not an optimisation. For live video a frame that misses
its moment is worthless, so the transport must be free to **drop** it. The earlier
receiver used a browser reading the headset's WebSocket, and TCP's guarantees —
acknowledge, retransmit, deliver in order — meant a momentary radio hiccup was
buffered and delivered late instead of discarded. Nothing in that chain could shed
the backlog, so every transient became permanent latency; it accumulated to tens
of seconds over a session, punctuated by freezes. A bounded jitter buffer with
`drop-on-latency` cannot do that: late packets are thrown away and the picture
stays at the live edge by construction.

The headset resolves this box's address itself (default route → DHCP gateway →
`<subnet>.1`), so no pairing or configuration is involved. The stream is always
**unicast** — never a `.255` broadcast, which an AP would re-send into the BSS at
the 1–6 Mbps basic rate and bridge onto the Ethernet uplink.

Because the Pi is the Wi-Fi access point **and** the DHCP server, it also knows
which headsets are present. The controller:

- enumerates connected devices from the DHCP lease table,
- probes each one's `:8080` (an Adiona headset only serves there *while actively
  casting*) to tell casting headsets from idle ones, and
- picks which one is the session, exposing the decision at `/state`.

The kiosk session polls `/state` and runs the video player only while a headset is
live. This sidesteps mDNS entirely — every headset defaults to the same
`adiona.local` name, so name-based discovery is useless when more than one is
around.

### What is on screen

`cage` hosts two clients. Chromium draws the **non-live** screen — splash, join
credentials, uplink status, and the `W`-key Wi-Fi setup overlay — and stays
resident so switching is instant. `adiona-player.sh` maps its video surface on top
whenever a headset is casting. (If a `cage` build won't stack them, set
`PLAYER_OVERLAY_MODE="swap"` in `box.conf`.)

### Display rules

| Situation | Behavior |
|---|---|
| No headset casting | "Waiting for a headset…" screen, with the box's Wi-Fi name/password and Ethernet/internet status |
| A headset starts casting | Its stream is shown full-screen |
| **A second headset starts while one is live** | **Sticky session** — the live stream is never interrupted |
| The live headset stops / disconnects | Switches to the most-recently-connected remaining caster; if none, shows "Reconnecting…" for a grace window, then "Waiting…" |
| Stream pauses (headset off-head, app backgrounded) | Last frame holds on screen — no flash back to the splash |
| Packet loss | Recovers on the next keyframe (≤ 1 s); corrupt frames are suppressed rather than shown |
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
web/                   Non-live kiosk page (splash, status, Wi-Fi setup overlay)
controller/            adiona_controller.py — discovery, selection, /state, page server
system/
  network/             sysctl forwarding drop-in
  first-boot/          MAC→SSID/hostname provisioning oneshot (+ unit)
  kiosk/               cage-session.sh   → starts cage
                       kiosk-session.sh  → Chromium + player supervision
                       adiona-player.sh  → GStreamer RTP receiver (+ --probe)
  controller/          controller unit
image/
  pi-gen/              custom pi-gen stage + build config
  assemble-stage.sh    stage the repo files into the pi-gen payload
  build-image.sh       local Docker build
.github/workflows/     cloud image build (no local setup needed)
```

---

## Building the image

> For the full deployment runbook — hot-deploying to a running box over SSH,
> flashing cards, and verifying the stream end to end — see **[DEPLOY.md](DEPLOY.md)**.

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
importantly `WIFI_PASSPHRASE`. Also tunable: SSID prefix, Wi-Fi band/channel, LAN
subnet, RTP port, player latency, scan interval, and the reconnect grace period.

> Change `WIFI_PASSPHRASE` before deploying to customers — and change
> `FIRST_USER_PASS` in `image/pi-gen/config` with it. The SSH login
> (`adionauser`) currently shares the Wi-Fi passphrase, which the waiting screen
> displays to anyone standing in front of the box.

Two settings are worth a deliberate decision per venue:

- **`WIFI_BAND`** — defaults to `bg` (2.4 GHz) for range. 2.4 GHz is the most
  contended spectrum at a public event and rate-adapts down hard at distance. If
  the headset sits within a few metres of the box, `a` (5 GHz, channel 36 or 149)
  is usually the better link.
- **`PLAYER_LATENCY_MS`** — how long the player waits for a late packet before
  discarding it. 50 ms is the live-edge default; raise to 80–120 only if a weak
  link shows stutter.

---

## Troubleshooting the stream

**Nothing but the splash, though the headset says it's casting.** Check whether
RTP is arriving at all:

```bash
sudo systemctl stop adiona-kiosk        # release the port
/opt/adiona/kiosk/adiona-player.sh --probe
sudo systemctl start adiona-kiosk
```

- *"NO PACKETS"* → the headset isn't sending here. Confirm it joined **this**
  box's SSID, and that nothing is filtering `udp/5004` on `wlan0` (a stock
  Raspberry Pi OS has no INPUT firewall, so this is only a factor if one was
  added).
- *Packets counted, but still no picture* → the player itself is failing. Run it
  in the foreground and read the GStreamer error: `journalctl -u adiona-kiosk -f`,
  or check that the `gstreamer1.0-libav` package is present (it provides
  `avdec_h264`).

**Video plays but the splash never gets out of the way** — this box's `cage`
build isn't stacking the two clients. Set `PLAYER_OVERLAY_MODE="swap"` in
`/etc/adiona/box.conf` and restart `adiona-kiosk`.

**Stutter or tearing on a weak link** — raise `PLAYER_LATENCY_MS`, or move the AP
to 5 GHz. Do not "fix" it by raising the headset's bitrate: offering the link more
than it can carry is what causes the problem in the first place.

---

## Live deploy to a running box

Flashing a card per change is not an iteration loop. `tools/deploy.ps1` pushes the
working tree to a box that is already provisioned and reachable on its Ethernet
uplink, replaying the same repo→box file mapping the image build performs, then
restarts the services. Run it from Windows PowerShell:

```powershell
.\tools\deploy.ps1                          # push everything, restart both services
.\tools\deploy.ps1 -Box 192.168.1.155       # or set $env:ADIONA_BOX / $env:ADIONA_USER
.\tools\deploy.ps1 -Packages -Logs 60       # after a change that adds a dependency
.\tools\deploy.ps1 -Restart controller -NoConf   # keep the box's own box.conf tuning
.\tools\deploy.ps1 -Status                  # what is this box running right now?
.\tools\deploy.ps1 -Probe                   # is RTP actually arriving? (kiosk stopped)
.\tools\deploy.ps1 -DryRun                  # show the plan, send nothing
```

`Get-Help .\tools\deploy.ps1 -Full` documents every switch. It needs only
`ssh.exe` and `tar.exe` (both ship with Windows 10/11) plus passwordless `sudo` on
the box, and it stamps `/etc/adiona/.deployed` with the version, commit and dirty
flag it pushed — so a box never has to be guessed at later.

A deploy is a single SSH connection: the payload tarball carries the install
script inside it and is fed to `ssh` on stdin. Use key-based auth if you iterate
often — trixie's `PerSourcePenalties` blocks a source that reconnects rapidly, and
password auth means one prompt per connection.

Three classes of change it cannot carry, by design:

- **New packages.** A file sync leaves the box unable to run the new code (the
  RTP switch added five GStreamer packages). Pass `-Packages`; it installs
  whatever is missing from the image's `00-packages`.
- **Wi-Fi AP settings.** `SSID_PREFIX`, `WIFI_BAND`, `WIFI_CHANNEL` and
  `WIFI_PASSPHRASE` are consumed once, when first boot builds the NetworkManager
  AP profile. Pass `-FirstBoot` to rebuild it (SSID and hostname are MAC-derived,
  so they do not change).
- **Boot-level changes** — `cmdline.txt`, `config.txt`, Plymouth theme
  registration, service enablement. Reflash for those.

Deploy is for iteration; the image is the release. Anything validated this way
still has to land in the repo and be flashed before it counts as shipped.

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
