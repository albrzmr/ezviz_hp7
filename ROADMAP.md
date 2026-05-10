# Roadmap

A live list of features and improvements planned for this fork.  Help
welcome — open an issue or PR if you want to pick something up.

---

## v0.7 — Two-way audio (intercom)

The HP7 / CP7 is a doorbell, not just a camera, so a fully-featured HA
integration should let you both **hear** the visitor and **talk back**
to them from the dashboard / Companion app.

### Where we are today (v0.6.1)

- **Downlink (visitor → user):** the upstream MPEG-PS already carries
  an MP2 audio track alongside the HEVC video.  In **HLS** mode HA's
  Stream component mux's both tracks and the browser plays them.
  ✅ Already works.
- **MJPEG mode:** the per-viewer ffmpeg subprocess is invoked with
  ``-an`` (no audio) because MJPEG is a video-only format.  ❌ No
  audio in MJPEG live view.
- **Uplink (user → visitor):** ❌ not implemented.  The doorbell's
  upload command(s) need to be reverse-engineered first.

### What v0.7 should deliver

- [ ] **Audio in MJPEG mode.**  Expose a parallel HTTP endpoint
      (``/api/ezviz_hp7/{entry_id}/audio.mp3`` or similar) that
      streams the doorbell's audio track decoded from MPEG-PS.  The
      Lovelace card config can layer the audio on top of the MJPEG
      ``<img>``, or we ship a tiny custom card that does it.
- [ ] **Uplink protocol reverse-engineering.**  Capture pcap of the
      official EZVIZ Android app while talking back to the doorbell
      with our existing MPEG-PS decryption tooling, identify the
      relevant LAN command(s) (likely a sibling of cmd ``0x2011`` /
      ``0x3105`` on port 9020 or a new port), and the audio codec
      expected (G.711 µ-law / A-law is the common Hikvision default;
      the HP7 may also accept G.722 / ADPCM / AAC).
- [ ] **Capture path inside HA.**  Browser-side: WebRTC
      ``getUserMedia`` for the microphone, encoded to whatever the
      doorbell expects (likely G.711 — that's transcoding territory
      we'll need ffmpeg or aiortc for).  Companion app: same APIs via
      its WebView.
- [ ] **Push-to-talk control.**  A button (``button.ezviz_hp7_*_talk``)
      or a service ``ezviz_hp7.talk_start`` / ``ezviz_hp7.talk_stop``
      so automations can also speak through the doorbell ("notify the
      delivery courier the package is at the back gate" etc.).
- [ ] **Frontend integration.**  At minimum: a custom Lovelace card
      with a hold-to-talk button and a live audio meter.  Stretch:
      WebRTC two-way audio that just works in HA's standard camera
      card.

### Why this is a v0.7-class undertaking

The downlink crypto we already have covers most of the wire stack.
The unknown is the **upload framing** — none of the currently
captured / decompiled flows on the recipe side cover it.  Realistic
work breakdown:

1. ~3-5 days of pcap capture + Frida hooks on the official app while
   it does talk-back, decrypted with the existing ChaCha20 / AES-128
   tooling.
2. ~2-3 days of clean-room implementation of the upload command(s)
   in ``cpd7/lan_client.py``.
3. ~3-4 days of integration into HA: aiohttp endpoint(s), WebRTC
   handler, button entity, automation service, custom card or
   integration with HA's built-in WebRTC stack.

### Open questions / decisions

- Codec for the uplink: stick to whatever the doorbell wants
  natively, or transcode in the integration?
- Latency target: WebRTC <500 ms vs the easier HTTP-streamed approach
  (~1-2 s)?
- Should we rely on go2rtc's built-in two-way support (HA Core
  bundled), or implement WebRTC handling directly in the
  integration?  The former is simpler if go2rtc can ingest the
  doorbell stream; the latter avoids the dependency.

---

## Smaller items (post-v0.7)

- [ ] **Multi-viewer fan-out** in ``tcp_relay.py`` so HLS and MJPEG
      can be consumed simultaneously from different cards / dashboards.
      The doorbell only allows one ``PLAY`` socket so the relay would
      multiplex internally.
- [ ] **HEVC → H.264 transcoding option for HLS** so users on
      Chrome / Firefox desktop can use HLS mode without browser
      compatibility headaches.
- [ ] **Cloud-relay fallback** for when HA is reachable but the
      doorbell isn't on the same LAN (currently the integration
      requires LAN reachability between HA and the doorbell).
- [ ] **Snapshot via live stream**, not via the cloud's
      ``last_alarm_pic``.  Quick on-demand JPEG by tapping a frame
      out of the running upstream session.
