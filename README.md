# Home Assistant Integration for EZVIZ HP7 / CP7 Intercom

> [!WARNING]
> ## ⚠️ This fork is deprecated — please use the upstream integration
>
> The pure-Python **LAN live-stream** work that lived in this fork has been
> folded into the **original integration**, which is actively maintained and
> now goes further than this fork ever did: LAN audio, the p2p-register + CAS
> key handshake, HEVC→H.264 transcoding, an MJPEG live-view mode, and all the
> device entities — in one place.
>
> 👉 **Use [Bobsilvio/ezviz_hp7](https://github.com/Bobsilvio/ezviz_hp7) instead**,
> and set **Stream source = `local`** in the integration options for the LAN stream.
>
> Big thanks to **[@Bobsilvio](https://github.com/Bobsilvio)** for integrating the
> LAN protocol into the main project and for the fast follow-up work. This
> repository is no longer maintained and won't receive further updates.

[![CI](https://github.com/albrzmr/ezviz_hp7/actions/workflows/ci.yml/badge.svg)](https://github.com/albrzmr/ezviz_hp7/actions/workflows/ci.yml)
[![HACS Validation](https://github.com/albrzmr/ezviz_hp7/actions/workflows/ci.yml/badge.svg?event=schedule)](https://github.com/albrzmr/ezviz_hp7/actions)
[![codecov](https://codecov.io/gh/albrzmr/ezviz_hp7/branch/main/graph/badge.svg)](https://codecov.io/gh/albrzmr/ezviz_hp7)
[![HACS](https://img.shields.io/badge/HACS-Custom-orange.svg)](https://hacs.xyz/docs/faq/custom_repositories)
[![Code style: Ruff](https://img.shields.io/endpoint?url=https://raw.githubusercontent.com/astral-sh/ruff/main/assets/badge/v2.json)](https://github.com/astral-sh/ruff)
[![pre-commit](https://img.shields.io/badge/pre--commit-enabled-brightgreen?logo=pre-commit)](https://github.com/pre-commit/pre-commit)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)

## Before you enjoy the integration — say hi to the people behind it

Two folks made this possible: **[silviosmart](https://ko-fi.com/silviosmart)**,
who built the original integration this fork stands on, and
**[albrzmr](https://ko-fi.com/albrzmr)**, who maintains this fork and the
pure-Python live-stream additions.  If it ends up being useful in your home,
buying one (or both!) a coffee makes our day.

[![Support albrzmr on Ko-fi](https://img.shields.io/badge/Ko--fi-Buy_albrzmr_a_coffee-FF5E5B?logo=ko-fi&logoColor=white&style=for-the-badge)](https://ko-fi.com/albrzmr)
[![Support silviosmart on Ko-fi](https://img.shields.io/badge/Ko--fi-Buy_silviosmart_a_coffee_(original_creator)-29ABE0?logo=ko-fi&logoColor=white&style=for-the-badge)](https://ko-fi.com/silviosmart)

Now scroll on and enjoy it.

---

This is a **custom Home Assistant integration** for the **EZVIZ HP7 / CP7 video
doorbell**.  It exposes the device in HA so you can **watch the live stream**,
**unlock the door and gate**, see device status, and listen for ring events —
all on your local LAN, without any add-on containers or extra services.

This fork extends the original integration with a **pure-Python live-stream
pipeline** that connects to the doorbell directly on the LAN.  No native SDK,
no add-on container, no emulator — just `cryptography` + `pycryptodome` +
ffmpeg (which Home Assistant already ships).

---

> **Heads-up — EZVIZ device limit.**  EZVIZ only allows 10 logged-in devices
> per account.  If login fails, go to **EZVIZ app → User → Login settings →
> Manage terminals** and remove any unused entries.

---

## Features

- Auto-discovery and registration of your HP7 / CP7.
- **Live video streaming** in HA, with two selectable modes:
  - **MJPEG (default):** ~500 ms glass-to-glass latency, universal browser
    compatibility, transcoded on the fly to motion JPEG (720p, 8 fps).
  - **HLS:** native 2K HEVC at 25 fps, ~10–20 s of delay; needs an
    HEVC-capable client (Safari / iOS / hardware-decoded Android).
- **Door unlock** (lock #2 by default).
- **Gate unlock** (lock #1 by default).
- **Sensors**: firmware version, online status, WiFi signal / SSID,
  LAN / WAN IP, last-alarm timestamp, alarm name, last snapshot URL,
  firmware-update available, seconds since last trigger.
- **Binary sensors** (event-driven, pulse for a few seconds): doorbell
  ring, smart detection, intelligent detection, gate open, lock unlock.
- **Smart pre-warming**: when the cloud reports a doorbell ring or
  motion event, the integration opens the LAN session in advance so
  that tapping the notification shows the first frame near-instantly.
- Service calls usable from automations and scripts.
- Multi-region support (EU / US / CN / AS / SA / RU).

### Choosing the live-view mode

The mode is selectable from **Settings → Devices & Services → EZVIZ HP7
→ Configure**.  It defaults to MJPEG so the live view works in any
browser and any HA Companion version.  Switch to HLS only if you want
full 2K detail and have an HEVC-capable client.

| Mode  | Latency  | Resolution / fps | CPU on HA host (per viewer) | Browser support |
|-------|----------|------------------|------------------------------|-----------------|
| MJPEG | ~500 ms  | 1280×720 / 8 fps | one ffmpeg subprocess (~30–50 % of one core on Pi 4) | Universal |
| HLS   | 10–20 s  | 2048×1296 / 25 fps | minimal (HA's built-in stream worker) | HEVC-capable only |

### Live-stream notes

- The first frame typically appears within 1–2 s on a **cold** LAN
  session.  When the session is **already warm** — either pre-warmed
  by a recent doorbell event, or reused within the post-disconnect
  grace window — the first frame is sub-second.
- Re-pairing the doorbell rotates its per-device session key; the
  integration refetches it transparently the next time a viewer
  connects.
- For the standard **Camera** Lovelace card, leave the integration in
  **HLS** mode (the card uses Home Assistant's stream worker).  For
  low-latency MJPEG, use a **Picture Entity** card with
  `camera_view: live`.

---

## How it works (brief)

```
HA Stream component → ffmpeg → asyncio TCP relay → encrypted LAN session
                                                          ↑
                                              EZVIZ cloud login + EUCAS
```

The integration logs in through the EZVIZ cloud API to discover the
doorbell, fetches an AES-128 control key from EZVIZ's EUCAS server,
opens an encrypted LAN session to the doorbell (ECDH P-256 →
ChaCha20 stream cipher), pipes the decrypted MPEG-PS bytes through a
local TCP relay, and exposes the live view to Home Assistant either
as **HLS** (HA's built-in Stream component muxes HEVC + AAC) or
**MJPEG** (a small ffmpeg subprocess transcodes to motion JPEG for
sub-second latency).

The full LAN protocol — wire format, key derivation, packet handling
— is documented under
[`docs/cpd7-stream-recipe/`](docs/cpd7-stream-recipe/) for the
curious.

---

## Cloud-friendly polling

To keep the EZVIZ cloud footprint as small as possible without
sacrificing event latency, the integration polls two endpoints at
different cadences:

- **Alarm timeline** (``unifiedmsg/list``) — every 15 s.  Drives the
  doorbell-ring / motion binary sensors and the pre-warming
  listener, so it needs to be fresh.
- **Static device info** (``pagelist``) — every 5 min.  Covers
  firmware, WiFi signal, IPs and the like, all of which change
  slowly.  Between refreshes the integration serves a cached copy,
  and a transient failure of this call is tolerated silently so the
  dashboard stays live.

The result is roughly **6 000 HTTP calls per day per doorbell** —
about half of what a naive 15 s combined-poll would generate.  Per-
install random ``featureCode`` further reduces the chance of being
caught by anti-abuse heuristics on the cloud side.

---

## Installation via HACS

1. Open Home Assistant.
2. Go to **HACS → Integrations → Custom repositories**.
3. Add `https://github.com/albrzmr/ezviz_hp7` with type `Integration`.
4. Search for `EZVIZ HP7` and install.
5. Restart Home Assistant.
6. Go to **Settings → Devices & Services → Add Integration**.

[![Open in HACS](https://my.home-assistant.io/badges/hacs_repository.svg)](https://my.home-assistant.io/redirect/hacs_repository/?owner=albrzmr&repository=ezviz_hp7&category=integration)

---

## Configuration

1. In HA, go to **Settings → Devices & Services → Add Integration**.
2. Search for **EZVIZ HP7**.
3. Enter:
   - **Username** (email used for the EZVIZ app).
   - **Password**.
   - **Region** (`eu`, `us`, `cn`, `as`, `sa`, `ru`).
4. Pick the device serial.  HP7 / CP7 serials look like
   `MAINSERIAL-CAMSERIAL` (e.g. `BE0000000-BE0000000`).

The integration logs in through the EZVIZ cloud API and starts the local TCP
relay used for streaming.

---

## Usage

After setup you'll see:

- A camera entity named **Stream** (typically
  `camera.ezviz_*_stream`, exact ID depends on Home Assistant's
  slugging of the device name) exposing the live stream and last
  alarm snapshot.
- Sensors and binary sensors for firmware, online state, doorbell ring,
  smart-detection events, gate / lock status.
- Two services:
  - `ezviz_hp7.unlock_door`
  - `ezviz_hp7.unlock_gate`

Example automation:

```yaml
alias: Unlock gate on RFID card
trigger:
  - platform: state
    entity_id: sensor.rfid_reader
    to: "CARD_1234"
action:
  - service: ezviz_hp7.unlock_gate
    data:
      serial: BE0000000-BE0000000
```

---

## Limitations

- **Two-way audio / talkback is not implemented.**  The pipeline today is
  receive-only.
- **HEVC playback depends on the client.**  If your browser cannot decode
  HEVC (most Chromium on Windows/Linux, Firefox), the camera card will be
  black even though the stream is healthy.  Workarounds: view from
  Safari / iOS / Companion app, or add a HEVC→H.264 transcoding step in
  the relay.
- **Video Encryption / Image Encryption mode is not supported yet.**  When
  enabled in the EZVIZ app (Indoor Display → Privacy → Video Encryption),
  the camera wraps the stream with a verification-code-derived layer the
  integration does not yet decode.  Symptom: control plane succeeds but
  zero plaintext bytes ever arrive.  Workaround: turn the toggle OFF on
  the indoor display; check the auto-generated entity
  ``binary_sensor.<your-cp7>_video_encryption_enabled`` to confirm.
- **Legacy NetSDK firmware (port 8000) is not supported.**  A few older
  HP7 / CP7 firmware variants only expose the legacy Hikvision NetSDK
  on TCP 8000 instead of the modern CPD7 path on 9010/9020.  Symptom:
  `[Errno 111] Connection refused` on the LAN start.  No workaround
  from the integration side today.
- **One HP7 per config entry.**  Multi-device support is planned but not
  required for typical setups (just add the integration once per device).
- **Lock unlocks are issued via cloud.**  When the cloud is unreachable
  the unlock services fail.

---

## Network requirements

The integration opens a direct TCP connection from Home Assistant to
the doorbell on **port 9010** (control) and **port 9020** (stream)
on the doorbell's LAN IP.  No proxies, no port forwarding, no relay
servers.  This means:

- **HA and the doorbell must share an L3-routable path** to that LAN IP.
  Easiest setup: HA and the doorbell on the same VLAN / subnet.
- **HA running in Docker**: use `network_mode: host` if possible.  If
  you use a custom bridge, make sure that bridge actually has a route
  to the doorbell's LAN (verify with
  `docker exec -it <ha-container> ping <doorbell-ip>` ).  A bridge in
  a NAT-only Docker network will not reach the doorbell.
- **VLANs with stateful inspection** typically work for the small
  control-plane packets (INIT/INVITE/PLAY) and then *silently drop*
  the sustained stream — you will see the LAN session open and then
  no bytes arrive.  Either move HA into the same VLAN as the doorbell
  or add an explicit allow rule for the doorbell IP to HA on port 9020.

If your setup involves Docker bridges, multiple VLANs or a router
between HA and the doorbell, please verify routing with `ping` first
before opening an issue.

---

## Troubleshooting

### Enable detailed logs

Add this to your `configuration.yaml` and reload (or restart) Home
Assistant before reproducing the issue:

```yaml
# configuration.yaml
logger:
  default: info
  logs:
    custom_components.ezviz_hp7: debug
    custom_components.ezviz_hp7.api: debug
    custom_components.ezviz_hp7.tcp_relay: debug
    custom_components.ezviz_hp7.cpd7.lan_client: debug
    custom_components.ezviz_hp7.cpd7.decoder: debug
    custom_components.ezviz_hp7.pylocalapi.cas: debug
    custom_components.ezviz_hp7.mjpeg: debug
    homeassistant.components.stream: debug
```

You can also enable / disable individual loggers at runtime from
**Developer Tools → Services → `logger.set_level`** without editing
the YAML.

After reproducing the issue, grab the log from
**Settings → System → Logs → Download Full Log** (or
`/config/home-assistant.log`).  Filtering by
`ezviz_hp7|RELAY|MJPEG|CPD7|EZVIZ-AES|P2P-REG` already shows
everything relevant.

### Reading the log — what each marker means

The relay emits a small set of distinctive markers that, taken
together, tell you exactly which step failed:

- `[SETUP] entry <id> ready (mode=mjpeg|hls, relay=tcp://127.0.0.1:N)` —
  integration loaded successfully.
- `[EZVIZ-AES] cache MISS/HIT/forced-refresh for <serial>` followed by
  `EUCAS fetch OK` — the AES-128 control key for this device was
  retrieved (or served from cache).
- `[P2P-REG] POST … -> 200 (N B)` — the per-client cloud registration
  succeeded.
- `[RELAY] LAN upstream OPEN host=<ip> related=<sub-serial> (setup=N ms)` —
  INIT/INVITE/PLAY all succeeded.  At this point the TCP socket to the
  doorbell is open.
- `[RELAY] first raw byte from camera after N ms` — the camera started
  sending data over the LAN.
- `[RELAY] first plaintext byte after N ms (raw_so_far=NB)` — the
  ChaCha20 decoder produced its first decrypted chunk; from here on
  you should see playback.
- `[MJPEG] session START` … `session END` — the per-viewer ffmpeg
  process for MJPEG mode.

Periodic activity summary line (every ~5 min):
`EZVIZ HP7 stats (uptime=...): {…}`.  Grep that to inspect cloud-poll
counts, AES cache hits, LAN session totals and pre-warm efficacy
without enabling debug logging.

### Common error patterns

| Symptom in the log | Likely cause | What to do |
|---|---|---|
| `LAN start failed: doorbell <ip> is unreachable at the IP layer (EHOSTUNREACH)` | HA cannot reach the doorbell IP at all | Verify the doorbell is powered on and online in the EZVIZ app, then `ping` it from HA's shell / container.  Check Docker bridge routing if applicable. |
| `LAN start failed: doorbell <ip> reachable but TCP 9010/9020 closed (ECONNREFUSED)` | Legacy NetSDK firmware on port 8000 | Not supported yet.  Please open an issue with your **doorbell model and firmware build** (visible in the EZVIZ app under device details). |
| `LAN start failed: connection to <ip> timed out (ETIMEDOUT)` | Silent firewall / VLAN ACL between HA and the doorbell | Check VLAN ACLs, allow traffic to ports 9010 and 9020 from HA's IP. |
| `LAN upstream OPEN … first raw byte after N ms` but **no** `first plaintext byte` for several seconds | Video Encryption silently active on the indoor display, **or** a firmware variant the integration doesn't decrypt yet | Check `binary_sensor.<your-cp7>_video_encryption_enabled`.  If `on`, toggle Video Encryption OFF on the indoor display.  If `off` and it still fails, open an issue with the log. |
| `No stream bytes received from camera 8.0s after the LAN session opened.` | Doorbell completed control plane but stays silent | Indoor display module offline / cloud-disconnected, or firmware variant we don't yet support.  Verify the indoor display is powered and online. |
| `Login error: {'code': 1069, …}` (`登录终端已达上限`) | The EZVIZ account has hit the 10-session limit for logged-in terminals | EZVIZ app → Account → Settings → My Profile → Login Settings → Terminal Management → remove unused terminals, then reload the integration. |

### Reporting an issue

If after the above the stream still fails, please open an issue at
<https://github.com/albrzmr/ezviz_hp7/issues> with the **bug report
template** (it asks for the right info up front).  Required:

- Doorbell model + firmware build (EZVIZ app → device → settings → about).
- Integration version (visible in the manifest, also in the HACS card).
- Home Assistant version + install type (OS, Container, Supervised, Core).
- A debug log filtered by `ezviz_hp7|RELAY|MJPEG|CPD7|EZVIZ-AES|P2P-REG`
  spanning from before you opened the stream until ~10 seconds after.
  **Strip the JWT `sessionId=…` substrings** before pasting.
- The state of `binary_sensor.<your-cp7>_video_encryption_enabled`.

With those five pieces of info the maintainer can usually triage in a
single round.

---

## Quality

The integration ships with a CI-gated test suite — 370+ unit and
integration tests covering ~94 % of the integration's lines.  Every
commit on ``main`` is validated by GitHub Actions: ruff
(lint + format), pytest, hassfest, HACS validation, mypy, and
codecov upload.  See the badges at the top of this README for live
status.

---

## Contributing

Pull requests and issues welcome.  Bugs, feature ideas, log dumps for new
firmware versions all useful.

The next big feature on the menu is **two-way audio** for the intercom —
see [ROADMAP.md](ROADMAP.md) for the technical scope and current state.

---

## Credits

- Original integration: [@Bobsilvio](https://github.com/Bobsilvio/ezviz_hp7) — buy him a coffee at [ko-fi.com/silviosmart](https://ko-fi.com/silviosmart).
- Cloud API helpers: [pyEzvizApi](https://github.com/RenierM26/pyEzvizApi)
  by RenierM26.
- This fork is maintained by [@albrzmr](https://github.com/albrzmr) — buy me a coffee at [ko-fi.com/albrzmr](https://ko-fi.com/albrzmr).

---

## Disclaimer

This integration is provided **as-is**, with no warranty of any kind, and is
intended for **personal use** with EZVIZ doorbells you own.

EZVIZ's Terms of Service may restrict the use of non-official clients to
access their devices and cloud services.  Installing or running this
integration is entirely at your own risk — including the risk of EZVIZ
suspending or terminating your account.  This project is not affiliated
with, endorsed by, or sponsored by EZVIZ or Hikvision.

---

## License

Released under the MIT License — see [LICENSE](LICENSE) for the full text.
