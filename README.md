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
  (DHCP + NAT in one profile), on **5 GHz**. LAN is `192.168.50.0/24`, gateway
  `192.168.50.1`.
- `wlan1` (USB dongle, optional) → internet uplink as a Wi-Fi *client*, pinned to
  **2.4 GHz**. Configured on-screen with the `W` key.
- `eth0` → DHCP client; the preferred internet uplink. NAT from the AP is
  automatic whenever something has a default route.

### The two radios must not share a band

Both radios are dual-band and both can host an AP, so the split is a deliberate
choice — but they cannot both be on 2.4 GHz. Their antennas sit centimetres
apart, and the AP transmitting deafens the dongle's receiver. Measured on
hardware, that looked like auth frames needing 2–3 retries *even when they
succeeded*, re-association every ~10 s, and eventually `ssid-not-found` with the
upstream AP plainly in range. Moving the dongle onto a USB extension cable
measurably helped, which is the signature of exactly this problem.

The AP takes 5 GHz because it carries the video stream: it belongs on the
better-proven radio (`brcmfmac` AP mode is the standard Pi configuration, while
`rtw88`'s USB AP support is far less exercised) and it benefits from the cleaner
band. The dongle does client mode, which is what USB adapters are good at.

`WIFI_BAND` and `UPLINK_BAND` in `box.conf` control this, and can be swapped for
a venue that needs 2.4 GHz range for the headset — see **Configuration**.

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

Settings worth a deliberate decision per venue:

- **`WIFI_BAND` / `UPLINK_BAND`** — the band plan. Defaults are `a` (AP on 5 GHz,
  channel 36) and `bg` (dongle uplink on 2.4 GHz). **Keep them different**; see
  *The two radios must not share a band*.

  If a venue needs more range than 5 GHz gives — a headset well away from the box
  — swap them: `WIFI_BAND="bg"` with `WIFI_CHANNEL` 1, 6 or 11, and
  `UPLINK_BAND="a"`. Apply with `.\tools\deploy.ps1 -FirstBoot`; no reflash.

  Prefer non-DFS channels for the AP (36–48, 149–165). The 52–144 range needs
  radar detection, which delays or drops the beacon.
- **`PLAYER_LATENCY_MS`** — how long the player waits for a late packet before
  discarding it. 120 ms (~2 frame periods at 15 fps) is the default; the floor is
  the frame period, not network jitter. Raise it if a weak link shows stutter.
- **`STREAM_TIMEOUT_SEC`** — how long the last frame stays frozen after the
  stream stops before the splash returns. Default 30 s.

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
  in the foreground and read the GStreamer error with `.\tools\deploy.ps1 -Follow`
  (note that `journalctl -u adiona-kiosk` shows **nothing** from the player — the
  unit's `PAMName=login` puts those processes in a login-session scope, so they
  are journalled under `SYSLOG_IDENTIFIER=cage-session.sh`, not the unit),
  or check that the `gstreamer1.0-libav` package is present (it provides
  `avdec_h264`).

**Video plays but the splash never gets out of the way** — this box's `cage`
build isn't stacking the two clients. Set `PLAYER_OVERLAY_MODE="swap"` in
`/etc/adiona/box.conf` and restart `adiona-kiosk`.

**Stutter or tearing on a weak link** — raise `PLAYER_LATENCY_MS`, or move the AP
to 5 GHz. Do not "fix" it by raising the headset's bitrate: offering the link more
than it can carry is what causes the problem in the first place.

**Picture freezes instead of returning to the splash** — the box holds the last
frame for `STREAM_TIMEOUT_SEC` (default 30 s) after packets stop, then goes back
to the waiting screen and reconnects on its own when frames resume. Lower it for
a snappier return; raise it to ride out longer pauses.

---

## Troubleshooting the USB Wi-Fi uplink

A USB dongle that associates and then drops is almost always power management,
not signal. The box disables both mechanisms — 802.11 power save and USB
autosuspend — via `/etc/NetworkManager/conf.d/10-adiona-wifi.conf` and, because
some drivers restore the defaults on reassociation, again at runtime from the
controller every 30 s.

If it is still unstable, work through these in order:

```bash
IF=wlan1        # the dongle; wlan0 is the built-in AP radio

iw dev $IF get power_save                     # must say "off"
cat /sys/class/net/$IF/device/../power/control # must say "on"
dmesg -T | grep -iE "$IF|usb|firmware" | tail -40
journalctl -u NetworkManager -n 60 --no-pager
```

- **`dmesg` shows resets, `firmware` errors, or repeated disconnects** — driver
  or power-delivery problem, not configuration. Check the PSU first: a Pi 5 on an
  underpowered supply browns out USB under load, and a Wi-Fi dongle transmitting
  is exactly that load. Use the official 5 V/5 A supply.
- **Both radios on the same band.** This is the most common cause of "works,
  then unstable" once power management is ruled out, and it is what was actually
  wrong here. Check the two are still split:

  ```bash
  iw dev wlan0 info | grep channel     # AP      - expect a 5 GHz channel (36…)
  iw dev wlan1 link  | grep freq       # uplink  - expect a 2.4 GHz freq (24xx)
  ```

  If the uplink has drifted onto 5 GHz, its saved profile predates the band plan.
  The controller pins every saved profile at startup, so restarting
  `adiona-controller` (or redeploying) is enough; `UPLINK_BAND` in `box.conf`
  controls it.
- **Physical separation.** Even on different bands, a dongle plugged directly
  into the Pi sits centimetres from the AP antenna. A short USB extension cable
  measurably helped here and costs nothing.
- **NetworkManager keeps re-scanning.** Background scans on a client interface
  briefly leave the channel, which a marginal link may not survive. Check the
  journal for repeated `scanning` entries.
- **Chipset.** The dongle in use is an RTL8821CU on the in-kernel `rtw88_8821cu`
  driver — dual-band, and it reports AP mode as well as client. Prefer adapters
  with in-kernel drivers; an out-of-tree module (`rtl88xxau` and friends) adds a
  failure mode that no amount of configuration fixes. `dmesg` naming the module
  tells you which you have.

Ethernet remains the dependable uplink — the dongle exists for venues where
running a cable is impractical, and it is the box's least reliable link by nature.

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
