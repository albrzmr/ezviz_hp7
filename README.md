# Home Assistant Integration for EZVIZ HP7 / CP7 Intercom

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
**unlock the door and gate**, see device status, and listen for ring / motion
events — all on your local LAN, without depending on the EZVIZ NetSDK.

This fork extends the original integration with a **pure-Python live-stream
pipeline** that reverse-engineers and replaces EZVIZ's proprietary LAN protocol
on ports 9010/9020.  No native SDK, no add-on container, no Frida, no emulator
— just `cryptography` + `pycryptodome` + ffmpeg (which HA already ships).

---

> **Heads-up — EZVIZ device limit.**  EZVIZ only allows 10 logged-in devices
> per account.  If login fails, go to **EZVIZ app → User → Login settings →
> Manage terminals** and remove any unused entries.

---

## Features

- Auto-discovery and registration of your HP7 / CP7.
- **Live video streaming** in HA, with two selectable modes:
  - **MJPEG (default):** ~1 s glass-to-glass latency, universal browser
    compatibility, transcoded on the fly to motion JPEG (720p, 8 fps).
  - **HLS:** native 2K HEVC at 25 fps, ~10–20 s of delay; needs an
    HEVC-capable client (Safari / iOS / hardware-decoded Android).
- **Door unlock** (lock #2 by default).
- **Gate unlock** (lock #1 by default).
- Device sensors: firmware, online status, last alarm picture, ring/motion events.
- Service calls usable from automations and scripts.
- Multi-region support (EU / US / CN / AS / SA / RU).

### Choosing the live-view mode

The mode is selectable from **Settings → Devices & Services → EZVIZ HP7
→ Configure**.  It defaults to MJPEG so the live view works in any
browser and any HA Companion version.  Switch to HLS only if you want
full 2K detail and have an HEVC-capable client.

| Mode  | Latency  | Resolution / fps | CPU on HA host (per viewer) | Browser support |
|-------|----------|------------------|------------------------------|-----------------|
| MJPEG | ~1 s     | 1280×720 / 8 fps | one ffmpeg subprocess (~30–50 % of one core on Pi 4) | Universal |
| HLS   | 10–20 s  | 2048×1296 / 25 fps | minimal (HA's built-in stream worker) | HEVC-capable only |

### Live-stream notes

- Each viewer triggers a fresh upstream session on the doorbell.  After
  cloud token refresh + EUCAS round-trip + ECDH handshake the first
  frame appears in roughly 1–2 s.
- The AES-128 control key rotates whenever the doorbell is re-paired;
  it is refetched per session via the EUCAS `0x2001 DirectConnect`
  command.
- For the standard **Camera** Lovelace card, leave the integration in
  **HLS** mode (the card uses HA's stream worker).  For low-latency
  MJPEG, use a **Picture Entity** card with `camera_view: live`.

---

## Architecture (brief)

```
HA Stream component → ffmpeg (PyAV) -i tcp://127.0.0.1:RANDOM
                              ↑
                      asyncio TCP relay (tcp_relay.py)
                              ↑
                      StreamDecoder  — gates output on HEVC VPS keyframe
                              ↑   ECDH P-256 → AES-256-ECB → ChaCha20
                      Cpd7LanClient — INIT 0x2013 / INVITE 0x2011 / PLAY 0x3105
                              ↑   AES-128-CBC framing on ports 9010 + 9020
                      AES-128 key from EUCAS cmd 0x2001 DirectConnect
                              ↑
                      pyezviz cloud login (existing token flow)
```

Reverse-engineering notes and the full wire-format spec live in `docs/` of
the upstream research repo (Ghidra/jadx traces, Frida hooks, packet layouts).

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

The integration logs in through the EZVIZ cloud API, fetches the AES key from
EUCAS, and starts the local TCP relay used for streaming.

---

## Usage

After setup you'll see:

- A `camera.ezviz_cp7_<serial>` entity exposing the live stream and last
  alarm snapshot.
- Sensors and binary sensors for firmware, online state, motion, ringing,
  door / gate status, etc.
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
- **One HP7 per config entry.**  Multi-device support is planned but not
  required for typical setups (just add the integration once per device).
- **Lock unlocks are issued via cloud.**  When the cloud is unreachable
  the unlock services fail.

---

## Troubleshooting

Enable debug logging if the camera doesn't stream:

```yaml
# configuration.yaml
logger:
  logs:
    custom_components.ezviz_hp7: debug
    homeassistant.components.stream: debug
```

Useful log markers:

- `tcp_relay] CPD7 relay listening on 127.0.0.1:NNNN` — relay started.
- `tcp_relay] CPD7 relay client connected` — HA Stream worker connected.
- `cpd7.lan_client] CPD7 PLAY ... rx_cmd=0x3106` — PLAY accepted by doorbell.
- `cpd7.decoder] ChaCha20 key derived` — ECDH handshake OK.
- `cpd7.decoder] keyframe found at +N (pack@M) — stream synced` — first
  HEVC keyframe seen, bytes are flowing to ffmpeg.
- `[libav.hevc] PPS id out of range` (repeated) — keyframe gating failed
  (open an issue with logs).

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

EZVIZ's Terms of Service may prohibit reverse engineering and the use of
non-official clients to access their devices and cloud services.  Installing
or running this integration is entirely at your own risk — including the
risk of EZVIZ suspending or terminating your account.  This project is not
affiliated with, endorsed by, or sponsored by EZVIZ or Hikvision.

---

## License

Released under the MIT License — see [LICENSE](LICENSE) for the full text.
