# Adiona-TV display box (Raspberry Pi 5)

A headless, single-purpose appliance image for the Raspberry Pi 5. Each box is a
self-contained **router + cast receiver + wheel bridge + HDMI output** for an
Adiona-G driving simulator session:

1. **Hosts its own offline Wi-Fi LAN** for the Meta Quest headset to join (no
   internet required, no separate router).
2. **Routes the headset to the internet** — via Ethernet, or via a USB Wi-Fi
   dongle acting as a repeater — so the Quest can validate its license. Works
   fine with no uplink; the LAN just stays offline.
3. **Boots straight into a full-screen video player** showing the headset's live
   stream on the HDMI screen.
4. **Bridges a USB racing wheel to the headset** over that same Wi-Fi, so wheels
   the Quest cannot physically accept still drive the game.
5. **Updates itself** from the licence server when an uplink is present — only
   ever after somebody on site says yes, and reversibly.

Everything an operator can change is driven from **a keyboard plugged into the
box**, on screen. There is **nothing to type at show time**: power on the box,
power on the headset, and the stream appears.

**Contents** — [what it does](#what-the-box-does) · [networking](#networking) ·
[LAN wheel](#lan-wheel-usb-racing-wheel--headset) ·
[updates](#software-updates) · [configuration](#configuration) ·
[working on it](#working-on-the-box-software) ·
[troubleshooting](#troubleshooting)

---

# What the box does

## How casting works

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

## What is on screen

`cage` hosts two clients. Chromium draws the **non-live** screen — splash, join
credentials, uplink status, the wheel line, and every overlay — and stays resident
so switching is instant. `adiona-player.sh` maps its video surface on top whenever
a headset is casting. (If a `cage` build won't stack them, set
`PLAYER_OVERLAY_MODE="swap"` in `box.conf`.)

### Keys on the box

A USB keyboard plugged into the box is **the headset's keyboard**. Adiona-G
already accepts a keyboard paired to the Quest over Bluetooth, so the operator
used to need two; now the box relays its keystrokes over the LAN and one does
both jobs. Everything the game's key table lists works from here — see
[LAN keyboard](#lan-keyboard) for how it travels.

**F12 is the one key the box keeps for itself**, and it never reaches the
headset. It opens the settings panel, which holds every setup screen as a tab.

**While the panel is open the keyboard and mouse belong entirely to it.** The
bridge releases its exclusive grab for as long as it is up, so nothing on these
screens can collide with the game's key table — which is why the tabs can have
plain function keys of their own.

| Key | Does |
|---|---|
| **F12** | Open/close the settings panel. Works while a headset is casting: the video is suspended for as long as the panel is up, and returns on its own. It always opens on **F2** |
| **F1** | *Help* — what each tab is for, and every key on this list |
| **F2** | *Internet Wi-Fi* — scan, join, band plan |
| **F3** | *Bluetooth* — pair a keyboard or mouse to the box |
| **F4** | *Steering wheel* — axis assignment, range, centring |
| **F5** | *Vehicle sensors* — calibration. Present only while a rig is plugged in |
| **← →** | Switch tab, skipping any that is not currently present |
| **Tab**, arrows, **Enter** | Move around inside a tab |
| **S** | Save the wheel mapping, or confirm a sensor calibration |
| **Ctrl+S** | Re-centre the steering. The one key the box acts on **and still forwards**: it zeroes the USB sensor rig's turn count here, and the headset re-centres as it always has. See [USB vehicle sensors](#usb-vehicle-sensors-drive-square-steering--gas--brake) |
| **Esc** | Close the panel, or cancel the step that is running |
| **Y** / **N** | Answer the software-update prompt, when one is up |
| *everything else* | Goes to the headset |

The update prompt is drawn **above** the settings panel and takes the keyboard
while it is up, so its Y/N can never be swallowed by a dialog underneath.

> **The keyboard is held exclusively while it is forwarding.** That is what stops
> **Ctrl+W** and **Ctrl+R** — the game's siren and reverse — from closing and
> reloading the kiosk browser. Two consequences worth knowing:
>
> * `Ctrl+Alt+F2` does not reach the compositor while the grab is held. The grab
>   is only taken while a headset is **subscribed**, so an idle box behaves
>   exactly as it always did, and stopping `adiona-wheel` releases the keyboard
>   immediately (the grab belongs to the open file descriptor, so even a kill
>   frees it).
> * Set `KEYS_ENABLED="0"` in `box.conf` to switch the bridge off entirely.

### Display rules

| Situation | Behavior |
|---|---|
| No headset casting | "Waiting for a headset…" screen, with the box's Wi-Fi name/password, Ethernet/internet status and the wheel line |
| A headset starts casting | Its stream is shown full-screen |
| **A second headset starts while one is live** | **Sticky session** — the live stream is never interrupted |
| The live headset stops / disconnects | Switches to the most-recently-connected remaining caster; if none, shows "Reconnecting…" for a grace window, then "Waiting…" |
| Stream pauses (headset off-head, app backgrounded) | Last frame holds on screen — no flash back to the splash |
| Packet loss | Recovers on the next keyframe (≤ 1 s); corrupt frames are suppressed rather than shown |
| Any network event while live | **Never shown over a running stream** — status only appears on the waiting/reconnecting screens |
| An update is available | Full-screen prompt, but **only while no headset is connected**; withdrawn if one appears |
| An update is installing | Full-screen "do not remove power" screen; the result shows for a few seconds and clears itself |
| Waiting screen unchanged for 3 min | **Screensaver** — the two logos and the wordmark drift around a half-brightness screen. Any change on screen, or any keypress, dismisses it. Never runs over a live stream, nor over the update prompt |

> **A cursor in the settings panel, and nowhere else.** A mouse — USB, Bluetooth
> or the trackpad half of a combo keyboard — works normally in the panel and is
> properly visible there. It is not drawn on the waiting screen, over a live
> stream or under the screensaver, where a pointer parked in a corner for hours
> is the same burn-in the screensaver exists to prevent.
>
> Three layers, each covering what the one before it cannot:
> `web/index.html` sets `cursor: none` on `html, body`, which handles the waiting
> screen and the screensaver; it draws the panel's pointer from a **data: URI
> image**; and `blank-cursor.py` + `XCURSOR_THEME` give the compositor a fully
> transparent cursor for the one surface CSS cannot reach at all — the video
> player's.
>
> The image matters. Every cursor NAME resolves through that transparent theme,
> so asking for `default` or `pointer` returns nothing drawable. `<button>` and
> `<input>` carry user-agent cursor styles that override the inherited
> `cursor: none`, so the pointer used to wink in and out as it crossed the
> panel's controls, depending on which names the theme happened to define. An
> image is not a name and never consults the theme.
>
> `99-adiona-no-pointer.rules` used to make libinput ignore pointer devices for
> the same goal. It no longer does: it could never cover a combo keyboard+
> trackpad (keyboard and pointer share one event node, so dropping it took the
> keyboard too), and it broke the plain mouse this panel is meant to be usable
> with.

---

# Networking

- `wlan0` (built-in radio) → Wi-Fi AP via NetworkManager, `ipv4.method shared`
  (DHCP + NAT in one profile), on **5 GHz**. LAN is `192.168.50.0/24`, gateway
  `192.168.50.1`.
- `wlan1` (USB dongle, optional) → internet uplink as a Wi-Fi *client*, pinned to
  **2.4 GHz**. Configured on-screen with the `W` key.
- `eth0` → DHCP client; the preferred internet uplink. NAT from the AP is
  automatic whenever something has a default route.

## The two radios must not share a band

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

`WIFI_BAND` and `UPLINK_BAND` in `box.conf` control this. For a venue that needs
2.4 GHz range for the headset, the pairing can be swapped **on the box itself**:
press `W`, and use the **Radio band plan** switch at the bottom of the overlay.

The switch offers only the two valid pairings, never four independent band
choices, because the one arrangement that must not happen is both radios sharing
a band. It takes two presses (it drops the headset mid-session), and it is greyed
out when the USB adapter cannot do 5 GHz — read from the adapter's actual channel
list, since the same USB product id has shipped both 2.4-only and dual-band.
Changes are written to `box.conf`, so they survive a reboot but not a reflash.

## Per-box unique name

The image is generic — flash one `.img` to any number of cards. On first boot
each box derives its identity from its Wi-Fi MAC:

- **SSID** `Adiona-TV-XXXX` (XXXX = 4 hex digits of a hash of the MAC)
- **hostname** `adiona-tv-xxxx`

The Wi-Fi password is shared across the fleet (set in `config/box.conf`). Both the
SSID and password are shown on the box's waiting screen so a customer can point
the Quest at the right network.

Those same four hex digits are the box's **fleet id**, reported to the headset in
the `AB02` packet and to the licence server on every update check — so a box can
be identified from either end without SSH.

---

# LAN wheel (USB racing wheel → headset)

Good steering wheels — Logitech G920, Fanatec, Thrustmaster — are **USB only**, and
the Quest cannot take USB peripherals in the field. So the wheel plugs into *this
box*, which is already the access point at every event, and its readings are
streamed back to the headset over the same Wi-Fi that carries the video, at
roughly an order of magnitude less latency than Bluetooth.

That includes the **Doyo L820** that ships with many headsets. It pairs to the
Quest over Bluetooth perfectly well, but plugged into the box over USB instead it
is the same wheel on a much shorter leash.

**Plug the wheel into any USB port.** `adiona-wheel.service` finds it, applies the
rotation range where the device supports one, and streams.

| Wheel | Setup needed |
|---|---|
| Logitech **G920** | none — built-in profile, 900°, force-feedback centring |
| **Doyo L820** | none — built-in profile, 270°. Enumerates as `shanwan Android GamePad` (its board is ShanWan's, USB `2563:0526`), so do not look for "Doyo" anywhere on the box |
| Logitech **G29** | axes must be mapped once, on screen — same driver family as the G920, but its pedal axis order has never been measured here and a wrong profile is worse than none |
| anything else | axes mapped once, on screen (below), then saved and recognised automatically |

## How it works

`adiona-wheel.py` reads the wheel from `/dev/input/event*` (raw evdev via `ioctl`,
stdlib only), maps its axes to steering/gas/brake, and sends the headset a fixed
**48-byte packet at 90 Hz on UDP 5010**:

- **The headset subscribes, the box streams.** The headset sends an 8-byte `ASUB`
  keepalive to `<gateway>:5010` every 500 ms; the box streams back to whatever
  source address that came from, and stops after 3 s of silence. No discovery, no
  handshake, no session state a dropped packet could corrupt — the same premise as
  the video path, where the box is always the AP's gateway.
- **Every packet is a complete snapshot, sent at a fixed rate** whether or not the
  wheel moved. A lost packet therefore costs 11 ms of staleness instead of sticking
  a stale value until the next physical movement. There is no retransmission and
  no benefit to adding one.
- **It carries the keyboard too** — the last four keystrokes from a USB keyboard
  plugged into the box ride in the same packet. See [LAN keyboard](#lan-keyboard).
- **The box owns all device knowledge.** The headset carries no per-device table:
  it is told what is attached, what it is called and what it can do in a 2 Hz
  `DEVICE` packet, and derives its own full-lock angle as half the reported range
  (900° → ±450°, about 2.5 turns lock to lock). Supporting a new wheel is a
  30-second `deploy.ps1` to one box, not an APK rebuild and a sideload to every
  headset.

Latency budget, wheel to game physics: ~10–25 ms typical (USB HID poll 1–10, send
interval 0–8, Wi-Fi 1–3, headset frame 0–17).

## The protocol

Four messages share UDP 5010, all little-endian. **Finalised in v1.7.0, before the
first box shipped** — after that, changing a field means updating a fleet.

| Magic | Direction | Min size | Rate | Carries |
|---|---|---|---|---|
| `ASUB` | headset → box | 8 | 2 Hz | subscribe keepalive + the headset's licence mask |
| `AW02` | box → headset | 48 | 90 Hz | wheel state + the last four keystrokes |
| `AI02` | box → headset | 68 | 2 Hz *per device* | one attached device: type, name, capabilities, range |
| `AB02` | box → headset | 72 | 1 Hz | box identity, version and capabilities |

### Extending it safely

**Dispatch on the magic, require the minimum length, and ignore trailing bytes.**
Both ends do this, and it is what makes the format final rather than frozen: a
newer box can append a field to any packet and headsets already in the field keep
working, because they stop reading where their knowledge ends. Two rules keep that
true, and neither is worth trading for a few bytes:

- **Append only.** Never reorder, resize or repurpose an existing field. A new one
  goes on the end; a retired one becomes `reserved` and is still sent.
- **Reserved means zero.** Senders write zero, readers ignore — which is what lets
  a reserved field become real later with no version negotiation.

This replaced dispatch-on-exact-size, which made packet sizes a scarce resource
("48, 56 and 64 are taken") and turned every added field into a new message type
*and* a coordinated release.

### Flags versus capabilities

Both `AI02` and `AB02` carry each. The distinction is worth keeping: **flags are
state that changes while running; capabilities are facts about the hardware or the
build.** A capability answers "could this ever work", a flag answers "is it working
now". So the headset asks *does this box have a keyboard bridge* rather than *is it
newer than 1.7.0* — version comparisons are the thing that ages worst across a
fleet on mixed releases. `AI02.capabilities` uses bits 32–47 for the vehicle
sensors; `range_deg` reports the range currently applied while `CAP_RANGE_270` /
`CAP_RANGE_900` say which are supported at all.

### Exactly one wheel descriptor, and it is whatever drives the stream

The headset reads the steering name and the full-lock range off the
`DEVICE_TYPE_WHEEL` entry and off nothing else, and the range is not cosmetic:
the box scales `steer_deg` by `range/2` and the headset divides by exactly the
same figure, so the two must agree or full lock lands somewhere the control
cannot reach. So a USB sensor rig that is driving the stream is announced **as
the wheel** — which is what it is, functionally — and separately as
`DEVICE_TYPE_SENSORS`, which is what says how many boxes are attached and which
of them are calibrated. Two descriptors, because they answer two questions.

The consequence is worth knowing: an idle racing wheel is **not** announced while
a rig is driving, because a second wheel entry would break the invariant and
there is nowhere else to put it.

### The licence mask

`ASUB` carries the headset's **entire** `feature_flags` word, verbatim — never one
bit, never a derived boolean. The box uses it only to avoid *offering* hardware the
licence does not cover; the headset enforces, as it always has, and the box never
contacts the licence server. Carrying the whole word means a future capability is
already on the wire the day the licence grants it, so it costs a consumer rather
than a protocol change. It appears on `/wheel` as `feature_flags`, and is `null`
until a headset reports one.

> **Older APKs still subscribe.** The box's `ASUB` minimum is 8 bytes, so a headset
> from before this change registers normally and simply reports no mask. What it
> cannot do is decode `AW02`/`AI02`/`AB02` — the LAN wheel needs the matching app
> build, and `LanWheelManager.MIN_BOX_VERSION` reports the reverse mismatch as
> "box too old" on the headset's status line rather than failing silently.

## Setting up an unrecognised wheel

Anything not in `WHEEL_PROFILES` needs its axes identified once. Do it from the
box's own screen — you are standing there with the wheel in your hands:

1. Press **F12** on a keyboard attached to the box and select the **Steering
   wheel** tab with **→**. This works mid-stream too — the video steps aside.
2. Pick a control with **↑ ↓**, press **Enter**, move that control through its full
   travel, press **Enter** again to assign the axis that moved most.
3. Set **Range** to 900° or 270° on the last row.
4. Press **S** to save. It goes to `/etc/adiona/wheel-map.json`, keyed by device
   name, and is recognised automatically from then on.

Every axis is shown live underneath, so a wrong assignment is obvious before you
save it. There is no pedal calibration step: the driver already reports each
axis's true limits, and the wheel self-centres when it powers up.

## Checking it

The waiting screen shows a wheel line (device, range, and whether it is mapped).
For more detail:

```bash
curl -s localhost:8090/state | python3 -m json.tool   # summary
curl -s localhost:8090/wheel | python3 -m json.tool   # + live axis values
sudo python3 /opt/adiona/wheel/adiona-wheel.py --dump # every device and axis
```

`--dump` requires the service to be stopped (`sudo systemctl stop adiona-wheel`)
and is the first thing to run when a wheel behaves oddly — it prints every input
device, its exact name, and every axis with live values. Watch that gas and brake
move **different** axes; if one axis moves for both, the `hid-logitech-hidpp`
driver has combined the pedals and they cannot be separated here.

## On the headset

Settings → **Controller Type → Wheel over LAN**. Nothing changes for any other
controller type until that is selected. **Settings → Steering Wheel (LAN) Setup**
shows the link status and the sensitivity/curve sliders.

---

# USB vehicle sensors (Drive Square Steering / Gas / Brake)

A Drive Square sensor rig clamps onto a **real car's** steering wheel and pedals
and turns the actual controls into sim inputs. Adiona-G has supported the
Bluetooth version of these boxes for a long time; this is the USB generation,
which the Quest cannot take directly — so, exactly like the racing wheel, they
plug into this box and it streams them.

**Plug them into any USB port, hub or not.** They enumerate as three USB serial
devices whose names carry their role:

```
1b4f:9d2f Drive Square D2 Steering Sensor
1b4f:9d3f Drive Square D2 Gas Sensor
1b4f:9d4f Drive Square D2 Brake Sensor
```

Nothing happens without at least **Steering and Gas** — a lone pedal is not a
driving control. Brake is optional and reads zero when it is absent.

**They need a licence.** The box streams them only when the connected headset's
`feature_flags` has bit 2 (`INVEHICLE`) set. This is the first consumer of the
licence mask the headset already sends in `ASUB`, and the box still never
contacts the licence server: it uses the bit only to avoid *offering* hardware
the customer has not paid for. A fourth **EVO** box, gated on bit 4, has its
capability bit reserved and nothing else — that hardware does not exist yet.

## Why they are calibrated and the BLE ones were not

The BLE boxes sent a bare accelerometer reading, which can only be interpreted
against an assumed mounting — so the steering box had to be **bolted in a
pre-defined position** and only the pedals were ever calibrated.

These send the full **Kalman-fused quaternion**, which carries enough
information to *discover* the mounting instead of dictating it. So all three are
calibrated: the operator turns each control, and the box works out the axis it
actually rotates about.

**Only the axis, and which way it moves first.** No limit is measured anywhere. A
quaternion gives every angle outright, so there is nothing to learn by having
someone haul a wheel against its stops — a step that is awkward to perform, easy
to undershoot, and silently wrong when undershot: the sim would reach full lock
early, for the rest of the event, with nothing on screen to suggest why. The
rig's steering travel is declared once in `box.conf` instead
(`SENSORS_STEER_RANGE_DEG`, default 900), and a pedal's is an envelope that grows
as it is pressed.

## Degrees, not a fraction

`AW02.steer_deg` carries **degrees from centre** — 640° right is 640, not
"almost full lock". That is the whole reason the same rig can drive a car, a
tractor, a forklift or the rear steer of a tiller truck: how far the road wheels
turn for a given angle at the hand wheel is a property of the *vehicle*, and only
the vehicle model knows it.

The box's job stops at reporting the angle honestly. `range_deg` in `AI02` says
what the hardware can do, and the only thing the box does with it is clamp — so a
slipped turn count cannot send a car to a lock the wheel could never reach.

## How it works

`adiona_sensors.py` reads each box's `/dev/ttyACM*` — they are USB CDC-ACM, so
`termios` is the whole driver and there is no `pyserial` on the image — and
parses one line per sample:

```
17126192, 0.9993, -0.0017, -0.0263, 0.0265        t_ms, qw, qx, qy, qz
```

Rates differ per box and nothing assumes one: measured 67 Hz for steering, 33 Hz
for each pedal.

- **The rig IS a wheel, on the wire.** Its readings go out in the same 48-byte
  `AW02` packet at 90 Hz, as steering degrees and 0–1 pedals. The headset needs
  no new code path; it is told what the rig is in `AI02`.
- **The angle is absolute, only the turn count is integrated.** Every sample's
  angle is measured from the sensor's own attitude relative to the stored
  centre, so the box adds no drift of its own. The turn count is what makes more
  than 360° representable — and the only thing that can go wrong. See `Ctrl+S`.
- **Every channel fails to neutral.** A box that goes quiet for
  `SENSORS_STALE_SEC` reads zero rather than holding its last value: a pedal
  knocked off its mount mid-drive must not leave the throttle open.
- **Calibrations are keyed by each box's own serial number**, not by its role. A
  spare gas box carries its calibration between rigs, a knocked-out cable costs
  nothing when it goes back in, and a *different* box in the same role gets its
  own calibration or none at all — never the previous one's.

If a racing wheel is plugged in at the same time, **the rig wins** once it is
calibrated and licensed. The wheel is still there and still works the moment the
rig is unplugged.

## Calibrating

Press **F12** on a keyboard attached to the box and select **Vehicle sensors**
with **→**. The tab only exists while a sensor box is actually plugged in.

**One calibration for the whole rig, and two keypresses.**

1. **Enter** on *Calibrate all sensors*.
2. *"Take your feet off both pedals and centre the steering wheel"* — do it and
   press **Enter**. That is the zero pose for all three at once.
3. Now just use the controls, in any order and with no keypresses at all:
   **turn the wheel right first**, then left as far as it goes; **press the gas
   and brake all the way down**. Each row says `measuring…` and then
   `calibrated` as it gets what it needs, and the banner turns green when
   everything has.
4. **Enter** (or **S**) to confirm. It saves itself — there is no separate save
   step, and both keys mean the same thing while a calibration is running.

Watch the bars as you go. They are live from the moment each sensor has its
axis, *before* anything is committed, so turning the wheel right and seeing the
bar go right is the check that it is not about to be calibrated backwards.

Nothing has to be done "properly" the first time. **A pedal's travel is an
envelope that only ever grows** while the calibration is open, so a first
half-hearted press is not a mistake to undo — pressing further just extends it.
The steering axis keeps being refined from every degree you turn.

**Turn right first.** It is the only thing that tells the box which way right
is, and it settles within the first 10° of the sweep. If the steering ends up
inverted, clear it with **C** and calibrate again, turning right first this time.

**About 30° of movement is enough** for any control's axis — a quaternion pins a
hinge down quickly. That matters for hardware that cannot do a full turn:
motorcycle bars, a kart wheel, a forklift tiller. Turn further if you can, since
a longer sweep gives a better axis, but nothing *requires* it.

Every box's raw quaternion, rate and derived value are shown live underneath, so
a calibration that came out wrong is visible **before** it is saved. **Esc**
cancels a calibration in progress; a second **Esc** closes the panel. **C**
clears the selected sensor's calibration.

Saved to `/etc/adiona/sensor-cal.json` and picked up automatically from then on.

### When a row will not go green

| It says | It means |
|---|---|
| `move it — 4° of 30°` | It has not been moved enough yet to find its axis |
| `moving every which way — is it fixed to the pedal?` | Plenty of movement, but not about one axis. Almost always a box being waved rather than turning on its mount |
| `press it all the way down` | It has an axis but has not yet moved far enough from its zero, or it moved less than 5° in total |

**A pedal that reads backwards** — full at rest, zero when pressed — means its
zero pose was captured while it was already down. Redo the calibration with both
feet off the pedals at step 1. The direction is taken from the first way each
control moves away from its zero, so the zero has to be genuine.

Watch the bars rather than trusting the words: they are live from the moment a
sensor has its axis, which is well before it is committed.

### `Ctrl+S` — the steering reset

Sharp erratic steering, which is exactly what a near-crash produces, can slip the
turn count. **`Ctrl+S` zeroes it**, taking wherever the wheel currently stands as
dead ahead. It is the same key the BLE boxes have always used for the same
problem, and it works from the box's own keyboard because that keyboard is the
headset's keyboard too.

It does **not** touch the calibrated axis or range — a recovery must not cost a
calibration — and it is a no-op unless the rig is the active steering source, so
a racing wheel's behaviour is unchanged.

There is also a **Recentre** row on the tab, for when no headset is connected.

## Checking it

```bash
curl -s localhost:8090/sensors | python3 -m json.tool   # live, per box
sudo python3 /opt/adiona/wheel/adiona-wheel.py --dump   # every port + rate + quaternion
python3 tools/test-sensor-math.py                       # the maths, anywhere
python3 tools/test-sensor-loopback.py                   # the whole path, Linux/WSL
```

The waiting screen carries a sensor line beside the wheel one, which separates
the three failures that look identical at an event: a box that did not
enumerate, a rig that was never calibrated, and one the licence does not cover.

`tools/test-sensor-loopback.py` stands up three PTYs and drives a full
calibration through them, so a successful 900° sweep can be tested with no rig on
the desk — which no amount of staring at a still one will do.

---

# LAN keyboard

Adiona-G takes its commands from a keyboard — F1 for help, F2 for options, Space
to reset, Ctrl+S to re-centre the wheel, Shift/Alt+arrows for the mirrors — and on
the Quest that meant pairing a second keyboard over Bluetooth. The box already has
one plugged into it for its own setup screens, so it forwards that one instead.

**Nothing to configure.** Plug a keyboard into any USB port. If a headset is
subscribed, its keys go to the headset; if none is, the box behaves as it always
did. Both is impossible by construction — see the grab, below.

## How it works

`adiona_keys.py` reads every keyboard under `/dev/input/event*` (the same raw
evdev approach as the wheel) and keeps a ring of the **last four keystrokes**,
which `adiona-wheel.py` packs into every wheel packet. There is no send
scheduling, no burst and no retransmit anywhere:

- A keystroke stays in the ring until four newer ones push it out, so it is
  **transmitted ~90 times** before it could be lost. Losing one costs a missed
  command and nothing else — there is no held-key state on the wire to go stale,
  and therefore no key that can stick down.
- It also survives the headset's receive loop keeping only the **newest** packet
  of each drain: a keystroke that lived in one packet alone would be discarded
  there.
- Each keystroke is `(key, modifiers, down/repeat/up)`. **Ctrl, Alt and Shift are
  never sent as keystrokes** — they ride as flags on the key they modify, which is
  the only form the game ever looks at. Auto-repeat is throttled to
  `KEYS_REPEAT_HZ` (the kernel's own ~33 Hz is far too fast for a key that steps a
  mirror).
- The wire carries **Godot keycodes**, not evdev ones. Same principle as the
  wheel's axes: the box owns the device knowledge and the headset carries no
  translation table.

## The grab

While it is forwarding, the bridge holds the keyboard **exclusively**
(`EVIOCGRAB`), so cage and Chromium see nothing at all. That is required rather
than tidy — the game binds `Ctrl+R`, `Ctrl+W`, `Ctrl+P` and `Ctrl+C`, which to a
kiosk browser mean reload, close-window, print and copy.

It grabs only when **all** of these hold:

| Condition | Why |
|---|---|
| A keyboard is present | — |
| A headset is subscribed | With nowhere to send, grabbing costs the `Ctrl+Alt+F2` shell and buys nothing |
| The settings panel is closed | The panel is Chromium's, and it needs real keys to type an SSID password |
| The updater is not asking | Its Y/N is answered by Chromium too |

Transitions are deferred until **no key is physically held**, so the side about to
lose the device always sees the release of whatever was down — otherwise the F12
that opens the panel would stay latched forever in the compositor.

If this process dies, the kernel releases the grab with the file descriptor, so a
crash or a `systemctl stop adiona-wheel` hands the keyboard straight back.

## Checking it

```bash
curl -s localhost:8090/ui | python3 -m json.tool   # devices, forwarding, F12 count
cat /run/adiona/keys.json                          # the same, straight from the service
journalctl -u adiona-wheel -f                      # 'keyboard ... grabbed / released'
```

---

# Software updates

## What the headset is told

Once a second, on the same UDP socket the wheel already uses, the box sends an
`AB02` packet carrying its software version, its OS version, the box's fleet id,
a few status flags and its capability word. The headset uses it to decide whether the wheel
stream is one it can interpret, and shows it on the LAN wheel settings screen — so
"which version is that box on?" is answerable from inside the headset rather than
over SSH.

It rides on the wheel socket because the headset's socket is *connected* to
`box:5010` and drops datagrams from anywhere else.

## How an update happens

1. **Check — once per power-up.** Shortly after boot, and only when the box
   actually has an uplink, `adiona-updater.py` asks the licence server what the
   current release is. The reply is signed; a manifest that does not verify
   against `/etc/adiona/update-key.pub` is discarded and nothing is installed.

   There is no interval and no background polling. The box is switched on at a
   venue, used, and switched off, so the start of that is the only moment when
   somebody is both present and not mid-session. A prompt can therefore never
   appear hours into an event.

   "Once per boot" means once it *manages* to ask: at power-up the uplink is still
   associating, so the check retries (30 s, backing off to 15 min) until it gets a
   verified answer, and only then goes quiet. To ask again without a power cycle:
   `sudo systemctl restart adiona-updater`.
2. **Ask.** A prompt appears on the TV: *v1.6.0 → v1.6.1, Y to update, N for not
   now.* **No answer within 60 seconds means no update**, and that version is not
   offered again until the box is next powered up. The prompt only appears when no
   headset is connected — during a session the video surface covers the page
   completely, and interrupting a demo to ask about an update is the wrong thing
   to do anyway. If a headset turns up mid-prompt the offer is withdrawn without
   counting as a refusal, and re-shown once the box is idle.

   Uplinks at events are routinely somebody's phone hotspot, which is the whole
   reason nothing downloads before the question is answered.
3. **Download and stage.** The tarball is verified against the sha256 in the
   signed manifest and unpacked into `/opt/adiona/releases/<version>/`. Nothing
   the box is running has changed yet.
4. **Apply.** `apply-update.sh` installs any missing apt packages *first* (so a
   failure leaves the box untouched), merges new `box.conf` keys without
   overwriting anything the operator set, then swaps `/opt/adiona/current` — one
   atomic `rename(2)` of a symlink — and restarts the services. The display is
   only restarted if the kiosk files actually changed; otherwise the page just
   reloads itself.
5. **Prove it worked.** For 90 seconds afterwards it checks that every unit is
   active, is not crash-looping, and that the kiosk page is still polling
   `/state`. That last one is the only signal that separates a working box from
   Chromium sitting on an error page — `systemctl` cannot tell the difference.
   **Any failure rolls the symlink and the unit files straight back.**

The whole install takes a minute or two, nearly all of it that health soak. The
screen says so, because silence for that long reads as a hang and the one thing
that must not happen is somebody pulling the power to "fix" it.

## If the power is pulled mid-update

A marker is written and `fsync`ed *before* the swap, and cleared only once the new
release has passed its health check. `adiona-rollback.service` runs at boot
whenever that marker survives, **before any Adiona service starts**, and puts the
box back on the previous release. So a power cut during an update costs a longer
boot, not a dead box.

## The on-box layout

An updatable box keeps every release it has been given, and `current` is the only
thing that decides which one runs:

```
/opt/adiona/
  releases/1.6.14/{web,controller,kiosk,wheel,first-boot,updater,VERSION}
  releases/1.6.15/…
  current      -> releases/1.6.15      the ONE symlink, swapped atomically
  web          -> current/web          compat links, created once at migration
  controller   -> current/controller   and never touched again, so every
  kiosk        -> current/kiosk        absolute path in every unit file and
  …                                    script still resolves
```

`/opt/adiona` itself stays a real directory. Making *it* the symlink cannot be
done atomically — `rename(2)` refuses to replace a populated directory — so the
only conversion would be `rm -rf` followed by `ln -s`, and a power cut inside that
window leaves a box with no `/opt/adiona` at all.

A box flashed before this existed comes up `flat` and is migrated on first
contact; `deploy.ps1 -Status` prints which layout it is on. `UPDATE_KEEP_RELEASES`
(default 3) bounds the collection, and a release the box is running or could roll
back to is never collected.

---

# Configuration

Edit `config/box.conf` **before building** to change fleet defaults — most
importantly `WIFI_PASSPHRASE`. It reaches a box in the image, or via
`deploy.ps1`, or merged in by an update (which adds new keys and never overwrites
a value someone set on the box).

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
- **`WHEEL_DEFAULT_RANGE_DEG`** — rotation range for a wheel with no profile and
  no saved map. 900° suits the common Logitech wheels.
- **`SENSORS_ENABLED`** — set to `0` to ignore a Drive Square USB sensor rig
  entirely. `SENSORS_STALE_SEC` is the safety floor that zeroes a channel whose
  box goes quiet; `SENSORS_PEDAL_DEADZONE_DEG` stops a settled pedal box holding
  the throttle slightly open.
- **`SENSORS_STEER_RANGE_DEG`** — the rig's full lock-to-lock travel, **declared
  rather than measured** (default 900, a car). It bounds what goes on the wire;
  it does not scale the steering. Change it for a bus, tractor or kart wheel.
- **`UPDATE_ENABLED`** — set to `0` for a box that must never offer an update,
  such as one left with a customer between visits. `UPDATE_PROMPT_SECONDS`,
  `UPDATE_KEEP_RELEASES` and `UPDATE_ALLOW_PACKAGES` tune the rest.

Nothing the licence server sends can set a `box.conf` value. The file is `source`d
as shell by three root scripts, so a server-settable value would be remote root on
the whole fleet.

---

# Working on the box software

## Repository layout

```
VERSION                The single source of truth for the version number
config/box.conf        Fleet-wide settings (SSID prefix, Wi-Fi password, subnet, ports)
web/                   Non-live kiosk page (splash, status, every overlay)
controller/            adiona_controller.py — discovery, selection, /state, page server
system/
  network/             sysctl forwarding drop-in
  networkmanager/      Wi-Fi backend/regdom drop-in
  udev/                pointer-suppression rule
  chromium/            enterprise policy (no first-run UI, no restore prompts)
  plymouth/            boot splash theme
  first-boot/          MAC→SSID/hostname provisioning oneshot (+ unit)
  kiosk/               cage-session.sh   → starts cage (+ blank cursor theme)
                       kiosk-session.sh  → Chromium + player supervision
                       adiona-player.sh  → GStreamer RTP receiver (+ --probe)
  controller/          controller unit
                       (the controller also pairs Bluetooth keyboards/mice:
                        `busctl --json` to read BlueZ, one long-lived
                        `bluetoothctl` to scan and pair — a D-Bus discovery and a
                        pairing agent both belong to a connection, not a call)
  wheel/               adiona-wheel.py   — USB racing wheel → headset (+ unit)
                       adiona_keys.py    — USB keyboard → headset, in the same packet
                       adiona_sensors.py — Drive Square USB sensors, same packet again
  updater/             adiona-updater.py — checks, offers and stages releases
                       apply-update.sh   — the only thing that switches releases
                       update-key.pub    — verifies the signed release manifest
                       adiona-rollback.service — boot-time repair after a power cut
tools/
  deploy.ps1           live deploy from a Windows checkout
  release.ps1          cut a release: bump VERSION, commit, push (CI tags it)
  make-release.sh      build the OTA tarball + the manifest that describes it
  verify-signing.sh    prove the server signs with the pair of update-key.pub
  test-apply-update.sh sandbox test for the update/rollback machinery (Linux/WSL)
  test-update-e2e.sh   end-to-end check of check → download → apply (Linux/WSL)
  wheel-sim.py         fake LAN wheel + keyboard, for testing the game without a Pi
  test-sensor-math.py  the vehicle sensors' quaternion maths (runs anywhere)
  test-sensor-loopback.py  the whole sensor path over PTYs (Linux/WSL)
image/
  pi-gen/              custom pi-gen stage + build config
  assemble-stage.sh    stage the repo files into the pi-gen payload
  build-image.sh       local Docker build
.github/workflows/     release tarball (on push) + SD-card image (on demand)
```

There are three ways code reaches a box, in increasing order of ceremony:
**deploy** for iteration, an **OTA release** for boxes in the field, and a
**flashed card** for new hardware. They share one file mapping, so what you test
by deploying is what ships.

> The full runbook — targeting a box that will not resolve, fixing addresses,
> what to do after flashing a card, verifying the stream end to end — is
> **[DEPLOY.md](DEPLOY.md)**. This section is the overview.

## Live deploy to a running box

Flashing a card per change is not an iteration loop. `tools/deploy.ps1` pushes the
working tree to a box that is already provisioned and reachable, replaying the
same repo→box file mapping the image build performs, then restarts the services.
Run it from Windows PowerShell:

```powershell
.\tools\deploy.ps1                          # push everything, restart both services
.\tools\deploy.ps1 -Discover                # find boxes on this subnet
.\tools\deploy.ps1 -Box 192.168.1.155       # or set $env:ADIONA_BOX / $env:ADIONA_USER
.\tools\deploy.ps1 -Packages                # after a change that adds a dependency
.\tools\deploy.ps1 -Restart wheel -NoConf   # keep the box's own box.conf tuning
.\tools\deploy.ps1 -Status                  # what is this box running right now?
.\tools\deploy.ps1 -Probe                   # is RTP actually arriving? (kiosk stopped)
.\tools\deploy.ps1 -DryRun                  # show the plan, send nothing
```

Two read-only switches that **do not deploy**: `-Logs 60` tails the journal and
exits, `-Follow` follows it until Ctrl-C. To deploy and then watch, open `-Follow`
in a second window *before* the thing you want to observe.

`-Discover` sweeps the local /24 for a listening `sshd` and asks each answering
host for its SSID and version, because names are unreliable here: `.local` needs
mDNS, the bare hostname needs the router to register DHCP names, and both have
been seen failing while the box was perfectly reachable.

First contact with a **newly flashed card** is one command — `-Reflashed` clears
the stale SSH host keys, installs a deploy key and grants passwordless sudo, all
three of which break at once on a fresh card. (`-SetupKey` and `-SetupSudo` do
those pieces individually.)

`Get-Help .\tools\deploy.ps1 -Full` documents every switch. It needs only
`ssh.exe` and `tar.exe` (both ship with Windows 10/11), and it stamps
`/etc/adiona/.deployed` with the version, commit and dirty flag it pushed — so a
box never has to be guessed at later.

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
still has to land in the repo and be released before it counts as shipped.

## Cutting a release

```powershell
.\tools\release.ps1              # bump the patch version, commit, push
.\tools\release.ps1 1.7.0        # an explicit version
```

**The version lives in one place — the `VERSION` file — and everything else is
derived from it.** Push a changed `VERSION` to `main` and CI tags it, builds the
OTA tarball and publishes the release. Push anything else and nothing happens;
ordinary work lands on `main` without producing releases.

You never type a tag. The tag and `VERSION` used to be the same fact typed
twice, they drifted twice, and each time it failed in a build log rather than on
the machine that made the mistake. CI now derives one from the other, so they
cannot disagree.

`release.ps1` still exists for what CI cannot do: writing the file correctly
(LF, no BOM), reading it back to prove the write landed, and refusing to release
from a dirty tree. `bash tools/make-release.sh` builds just the tarball locally,
without releasing anything.

Everything for a version ends up on **one page**, `Releases/vX.Y.Z`:

| Asset | Built | For |
|---|---|---|
| `adiona-tv-X.Y.Z.tar.gz` | automatically, on push | the OTA update boxes download |
| `box_versions.json` | automatically | the complete manifest; replaces the server's copy |
| `*.img.xz` | **by hand** (see below) | flashing a new SD card |

Publishing an OTA is then two steps on the licence server: copy the tarball to the
public binary directory, and replace `data/box_versions.json` with the one from
the release page. The manifest is the whole contract — it names the URL, the size
and the sha256, and it is signed, so a box needs nothing else to decide what to
install and to prove it got it intact.

Before publishing anything anyone is waiting for:

```bash
bash tools/verify-signing.sh
```

A key mismatch between `system/updater/update-key.pub` and the vault secret is the
one failure in this chain that is completely silent — the server reports
`"signed": true`, boxes check in without errors, and nothing goes wrong until the
first release, which every box then refuses and goes on refusing. This fetches a
real manifest, verifies it the way a box would with the key a box would use, and
checks that the URL it names actually downloads at the declared size.

## Building an SD-card image

An image is only wanted when new hardware is being prepared — boxes already in the
field take releases over the air.

### Option A — GitHub Actions (recommended, no local tooling)

Actions tab → **Adiona-TV release** → **Run workflow**. It attaches the `.img.xz`
to the release its `VERSION` already names, so the card image and the OTA tarball
for a version sit on the same page. If no release exists for that version yet it
falls back to a run artifact and says so.

This is deliberately **not** on a push: it costs 45–75 minutes, against a couple
of minutes for the tarball, and is needed a hundred times less often.

### Option B — Local build (Docker)

On Linux/macOS, or Windows via **WSL2 + Docker Desktop**:

```bash
bash image/build-image.sh
# → image/.build/pi-gen/deploy/*.img.xz
```

`image/assemble-stage.sh` copies `config/`, `web/`, `controller/` and `system/`
wholesale into the pi-gen payload, and `stage-adiona/00-install/01-run.sh` places
them, installs the units, and writes `/etc/adiona/image-version`. That last file is
what lets a future release say "this one needs a reflash" instead of half-applying
— nothing on the box can update the kernel cmdline, `config.txt` or the Plymouth
registration, so a release that needs those has to be able to refuse.

### Flash & first boot

1. Flash the `.img.xz` with Raspberry Pi Imager / balenaEtcher.
2. Boot the Pi (HDMI to the TV; Ethernet optional).
3. First boot self-names the box and builds the AP, then comes up on the waiting
   screen. Subsequent boots go straight to the waiting screen (~20–30 s).
4. `.\tools\deploy.ps1 -Reflashed` if you intend to deploy to it.

### Configure the headset (once)

On the Quest: **Settings → Wi-Fi**, join the box's `Adiona-TV-XXXX` network (shown
on the waiting screen). Set it to auto-connect. Then enable
**Settings → Screen Casting** in Adiona-G. The stream appears on the TV.

## Testing off-box

The controller is stdlib-only Python and runs from a checkout (it falls back to
the repo's `config/` and `web/` when the `/etc` and `/opt` paths are absent):

```bash
python3 controller/adiona_controller.py
# open http://127.0.0.1:8090/  and  http://127.0.0.1:8090/state
```

Off-box there are no DHCP leases, so it sits on the "waiting" screen; point
`DHCP_LEASE_FILE` at a sample lease file (and have something serving the Adiona
page on `:8080`) to exercise selection.

### The LAN wheel, without a Pi or a wheel

`tools/wheel-sim.py` speaks the same wire protocol from any machine, so the whole
headset-side path can be exercised on a workstation. It is also the quickest way
to tell a headset-side bug from a box-side one: if the game drives correctly
against the simulator, the fault is in the wheel service or the device.

```bash
python3 tools/wheel-sim.py                # sweep lock to lock, 900°
python3 tools/wheel-sim.py --range 270    # pretend it is a 270° wheel
python3 tools/wheel-sim.py --static 90    # hold 90° right
python3 tools/wheel-sim.py --no-wheel     # box alive, nothing plugged in
python3 tools/wheel-sim.py --sensors      # a Drive Square USB sensor rig
```

Then in Godot set `Global.lan_wheel.box_ip_override` to that machine's IP
(`"127.0.0.1"` when the game runs on the same one). Run it on whatever machine the
game will treat as the display box — it binds the real port, so the actual wheel
service must not be running at the same time.

### The update machinery

Both suites build a sandbox and need real symlinks, so run them on Linux or WSL,
after touching anything under `system/updater/`:

```bash
bash tools/test-apply-update.sh   # migration, swap, rollback, crash-loop, power cut
bash tools/test-update-e2e.sh     # check → prompt → download → verify → apply
```

They cover the paths that are hardest to reach on real hardware — a release that
crash-loops, a card pulled mid-swap, a partial migration — precisely because
reaching them for real means deliberately breaking a box.

---

# Troubleshooting

## The stream

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

## The USB Wi-Fi uplink

A USB dongle that associates and then drops is *conventionally* blamed on power
management. On this box it was not: 802.11 power save and USB autosuspend were
both measured as already disabled while the link went on failing, and the actual
cause was **both radios sharing 2.4 GHz** with their antennas centimetres apart.
Radio power management is therefore left at the driver default — keeping the
radios permanently awake fixed nothing and cost idle power and heat.

So check the band split first, and treat power management as a late hypothesis
rather than a first guess.

```bash
IF=wlan1        # the dongle; wlan0 is the built-in AP radio

iw dev wlan0 info | grep channel      # AP     - expect 5 GHz (36…)
iw dev $IF link  | grep freq          # uplink - expect 2.4 GHz (24xx)
dmesg -T | grep -iE "$IF|usb|firmware" | tail -40
journalctl -u NetworkManager -n 60 --no-pager
vcgencmd get_throttled                # 0x0 = supply is fine
```

- **`dmesg` shows resets, `firmware` errors, or repeated disconnects** — driver
  or power-delivery problem, not configuration. Check the PSU first: a Pi 5 on an
  underpowered supply browns out USB under load, and a Wi-Fi dongle transmitting
  is exactly that load. Use the official 5 V/5 A supply.
- **Both radios on the same band.** Check this first — it is what was actually
  wrong here, and it is invisible unless you look. If the uplink has drifted onto
  5 GHz, its saved profile predates the band plan.
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

## Updates

Start with what the box itself thinks:

```bash
sudo /opt/adiona/updater/adiona-updater.py --status
.\tools\deploy.ps1 -Status                        # version, layout, last OTA
.\tools\deploy.ps1 -Logs 200                      # includes adiona-updater and adiona-apply
```

**No prompt ever appears.** In order of likelihood: the box never had an uplink
(the check retries forever and says so in the journal); the version on offer is
not newer; a headset has been connected the whole time; the manifest failed
signature verification, which is logged and deliberately shows nothing on screen;
or `UPDATE_ENABLED="0"`.

**A signed manifest is being refused.** Run `bash tools/verify-signing.sh` from a
checkout. A box refusing every release, silently and permanently, is what a key
mismatch looks like from the outside.

**The install failed and the box came back on its old version.** That is the
machinery working. The reason is in the journal under `adiona-apply`, and the last
outcome is in `--status`. The box is safe to leave; it will offer the release
again on the next power-up.

**"Installing" seems stuck.** The health soak is 90 seconds and the whole install
is a minute or two. Let it finish — the rollback path exists to survive a power
cut, but taking it costs a reboot that waiting would not have.

**Forcing a check without a power cycle:** `sudo systemctl restart adiona-updater`.
