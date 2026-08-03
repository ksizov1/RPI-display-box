# Deploying the Adiona-TV box

Two paths. Both are run from a session whose working directory is this repo:

```
c:\D2_Adiona_Godot\RPI-display-box
```

| | Use when | Turnaround |
|---|---|---|
| **A — Live deploy** (`tools/deploy.ps1`) | Iterating on the player, supervisor, controller, kiosk page or `box.conf` on a box that is already provisioned | ~15 s |
| **B — Build & flash** | New/replacement hardware, or shipping a fleet | ~45 min + flash |

> **The box and the headset ship together.** The RTP receiver here and the RTP
> sender in Adiona-G changed in the same work. An old APK sends to a dead address
> (`192.168.8.255`, the retired GL.iNet subnet); an old box has no RTP receiver at
> all. Either mismatch means **no video** — the splash just stays up. Deploy the
> box and install the new Adiona-G APK together.

---

## A — Live deploy

`tools/deploy.ps1` replays the same repo → box file mapping that
`image/pi-gen/stage-adiona/00-install/01-run.sh` performs at image-build time,
onto a running box. Read its comment header before first use; the essentials:

```powershell
# First deploy of the RTP change set — MUST include -Packages
.\tools\deploy.ps1 -Packages -Logs 60

# Subsequent iteration
.\tools\deploy.ps1

# What is this box actually running?
.\tools\deploy.ps1 -Status

# Is RTP arriving at all? (stops the kiosk for the duration)
.\tools\deploy.ps1 -Probe
```

**`-Packages` is not optional for this change set.** The receiver needs
`gstreamer1.0-{tools,plugins-base,plugins-good,plugins-bad,libav}`, which a
provisioned box does not have. A file-only sync leaves the box unable to run the
new code, and the failure — a box stuck on the splash — looks nothing like its
cause. `-Packages` installs whatever from `00-packages` is missing, so it is
harmless to pass every time.

Requirements the script itself enforces or documents:

- `ssh.exe` and `tar.exe` (both ship with Windows 10/11).
- **Passwordless sudo on the box.** The payload is fed to `ssh` on stdin, so
  `sudo` has no console to prompt from. Add a `/etc/sudoers.d` NOPASSWD entry for
  `adionauser` once, by hand.
- Key-based SSH auth is strongly recommended — Debian trixie's sshd applies
  `PerSourcePenalties`, so a burst of connections gets you temporarily blocked.
  A deploy is deliberately **one** connection; only `-Status` / `-Probe` /
  `-Logs` / `-Follow` open a second.

### Targeting a box

Default is `adiona-tv-6ced.local`. Override per-run or per-session:

```powershell
.\tools\deploy.ps1 -Box 192.168.1.155
$env:ADIONA_BOX = 'adiona-tv-1f3a.local'    # for the rest of the session
```

The box is a DHCP client on `eth0`, so with an Ethernet uplink it is reachable at
`<hostname>.local` (avahi is installed). Failing that, join its Wi-Fi and use
`192.168.50.1`, or use a keyboard on the box — `Ctrl+Alt+F2` reaches a login shell
(the kiosk runs `cage -s`, which permits VT switching).

Credentials come from `image/pi-gen/config`: `adionauser` / `stoPdrunKdrivinG`.

### Wi-Fi settings need `-FirstBoot`

`RTP_PORT`, `PLAYER_LATENCY_MS`, `PLAYER_OVERLAY_MODE`, `SCAN_INTERVAL_SECONDS`
and `RECONNECT_GRACE_SECONDS` take effect on the service restart a normal deploy
already does.

`WIFI_BAND`, `WIFI_CHANNEL`, `SSID_PREFIX` and `WIFI_PASSPHRASE` are consumed
**once**, when first-boot creates the NetworkManager AP profile. To apply a
change (e.g. moving this box to 5 GHz):

```powershell
.\tools\deploy.ps1 -FirstBoot
```

SSID and hostname are MAC-derived, so they don't change — an auto-connecting
headset rejoins on its own after the AP bounces.

---

## B — Build and flash a card

### B1 — Build

**GitHub Actions (recommended).** Push, then run **Build Adiona-TV image** from
the Actions tab, or push a `v*` tag. Download the `adiona-tv-image` artifact.

A `preflight-packages` job resolves the apt package list first, in about a
minute. pi-gen doesn't reach our stage until ~40 minutes in, so a bad package
name used to cost an entire build to discover — that is exactly how
`gstreamer1.0-wayland` (which doesn't exist on Trixie; `waylandsink` lives in
`gstreamer1.0-plugins-bad`) got caught. If preflight fails, fix
`image/pi-gen/stage-adiona/00-install/00-packages` before rerunning.

**Locally** (Linux/macOS, or Windows via WSL2 + Docker Desktop):

```bash
bash image/build-image.sh
# → image/.build/pi-gen/deploy/*.img.xz
```

`image/assemble-stage.sh` copies `web/`, `controller/`, `system/`, `config/` and
`VERSION` wholesale into the pi-gen payload, so new files under those are picked
up with no build-script change. A file *outside* them needs `assemble-stage.sh`
edited — and if it also has to reach a live box, `tools/deploy.ps1`'s
`$PayloadItems` and its `install.sh` need the same addition. Keep the three in
step.

### B2 — Flash

Raspberry Pi Imager (reads `.img.xz` directly) or balenaEtcher → **Use custom
image** → write.

Do **not** use Imager's customisation options (hostname, user, Wi-Fi, SSH) — they
collide with what first-boot sets up.

### B3 — First boot

1. HDMI to the TV, power on. Ethernet optional.
2. First boot derives SSID/hostname from the Wi-Fi MAC and builds the AP, then
   lands on the waiting screen. Later boots reach it in ~20–30 s.
3. The waiting screen shows the SSID and passphrase the headset should join.

---

## Verifying the stream

1. On the Quest: **Settings → Wi-Fi**, join `Adiona-TV-XXXX`, set auto-connect.
2. In Adiona-G: **Settings → Screen Casting** on.
3. The TV should switch from splash to live video within a couple of seconds.

If it doesn't, work outward from the box:

```powershell
.\tools\deploy.ps1 -Status     # services up? controller /state? stations associated?
.\tools\deploy.ps1 -Probe      # is RTP actually arriving?
```

- **`-Probe` says "NO PACKETS"** → the headset isn't sending here. Confirm it
  joined *this* box's SSID and that the new APK is installed. On the headset,
  `adb logcat -s CastingPlugin` logs the target it resolved, e.g.
  `RTP target: default-route gateway 192.168.50.1`.
- **Packets counted but no picture** → the player is failing. `.\tools\deploy.ps1
  -Follow` and read the GStreamer error. Most likely the GStreamer packages are
  missing — redeploy with `-Packages`.
- **Video plays but the splash stays on top** → this `cage` build won't stack the
  two Wayland clients. Set `PLAYER_OVERLAY_MODE="swap"` in `config/box.conf` and
  redeploy.
- **Stutter on a weak link** → raise `PLAYER_LATENCY_MS`, or move the AP to 5 GHz
  (`WIFI_BAND="a"`, `WIFI_CHANNEL="36"`, then `-FirstBoot`). Do **not** raise the
  headset's bitrate — offering the link more than it can carry is what caused the
  original latency problem.

### Latency

Expect **~250–400 ms, stable**. The remaining delay is capture and encoder
look-ahead on the headset, not transport. The property that matters is that it
must **not grow** over a session: the old TCP/MSE path ratcheted upward and never
recovered. If you see it climbing, something is buffering that shouldn't be.

---

## Rolling back

A live deploy is just files — `git checkout` the previous commit and re-run
`tools/deploy.ps1`. `-Status` reports the stamp of what a box is currently
running (`/etc/adiona/.deployed`), including whether the working tree was dirty.
For a flashed card, keep the previous `.img.xz`; reflashing is the rollback.
