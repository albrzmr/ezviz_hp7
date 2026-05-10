# Roadmap

A live list of features and improvements planned for this fork.  Help
welcome — open an issue or PR if you want to pick something up.

---

## v0.9 — Two-way audio (intercom)

The HP7 / CP7 is a doorbell, not just a camera, so a fully-featured HA
integration should let you both **hear** the visitor and **talk back**
to them from the dashboard / Companion app.

### Where we are today

- **Downlink (visitor → user):** the live stream already carries an
  audio track alongside the video.  In **HLS** mode Home Assistant's
  Stream component muxes both tracks and the browser plays them. ✅
  Already works.
- **MJPEG mode:** the per-viewer ffmpeg subprocess is invoked with
  ``-an`` (no audio) because MJPEG is a video-only format. ❌ No
  audio in MJPEG live view.
- **Uplink (user → visitor):** ❌ not implemented.

### What this milestone should deliver

- [ ] **Audio in MJPEG mode.**  Expose a parallel HTTP endpoint
      (``/api/ezviz_hp7/{entry_id}/audio.mp3`` or similar) that
      streams the doorbell's audio track.  The Lovelace card config
      can layer the audio on top of the MJPEG ``<img>``, or we ship
      a tiny custom card that does it.
- [ ] **Capture path inside HA.**  Browser-side: WebRTC
      ``getUserMedia`` for the microphone, encoded to whatever the
      doorbell expects.  Companion app: same APIs via its WebView.
- [ ] **Push-to-talk control.**  A button
      (``button.ezviz_hp7_*_talk``) or services
      ``ezviz_hp7.talk_start`` / ``ezviz_hp7.talk_stop`` so
      automations can also speak through the doorbell.
- [ ] **Frontend integration.**  At minimum: a custom Lovelace card
      with a hold-to-talk button and a live audio meter.  Stretch:
      WebRTC two-way audio that just works in HA's standard camera
      card.

### Open questions / decisions

- Codec for the uplink: stick to whatever the doorbell wants
  natively, or transcode in the integration?
- Latency target: WebRTC <500 ms vs the easier HTTP-streamed
  approach (~1–2 s)?
- Should we rely on go2rtc's built-in two-way support (bundled with
  HA Core), or implement WebRTC handling directly in the
  integration?  The former is simpler if go2rtc can ingest the
  doorbell stream; the latter avoids the extra dependency.

---

## Smaller items

- [ ] **Multi-viewer fan-out** in ``tcp_relay.py`` so HLS and MJPEG
      can be consumed simultaneously from different cards /
      dashboards.  The doorbell only allows one live session at a
      time, so the relay would multiplex internally.
- [ ] **HEVC → H.264 transcoding option for HLS** so users on
      Chrome / Firefox desktop can use HLS mode without browser
      compatibility headaches.
- [ ] **Cloud-relay fallback** for when HA is reachable but the
      doorbell isn't on the same LAN (currently the integration
      requires LAN reachability between HA and the doorbell).
- [ ] **Snapshot via live stream**, not via the cloud's
      ``last_alarm_pic``.  Quick on-demand JPEG by tapping a frame
      out of the running upstream session.
