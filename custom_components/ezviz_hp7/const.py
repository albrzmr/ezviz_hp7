"""Constants for EZVIZ HP7 integration."""

DOMAIN = "ezviz_hp7"
CONF_REGION = "region"
CONF_SERIAL = "serial"
CONF_CAMERA_PASSWORD = "camera_password"
# Per-install random featureCode (32-char hex).  Generated once on
# first ``async_setup_entry`` and persisted in ``entry.data`` so each
# install carries its own EUCAS fingerprint — there is no global
# hardcoded value that EZVIZ could blacklist.
CONF_FEATURE_CODE = "feature_code"

# Platforms to set up
PLATFORMS = ["button", "sensor", "binary_sensor", "camera", "select"]

# Coordinator tick interval, in seconds.  Each tick fetches the
# alarm timeline (``unifiedmsg/list``) — that's the latency-sensitive
# half: a slower tick means a slower doorbell-ring notification.
# 15 s gives ~3 s worst-case extra latency on top of the cloud's own
# ~3 s lag, while keeping us comfortably under the EZVIZ
# rate-limit / abuse-detection threshold.  (Earlier 2 s default
# produced ~3 600 hits/hour/device and got the test account flagged
# once.)
UPDATE_INTERVAL_SEC = 15

# How often, in seconds, the coordinator also refreshes the static
# device info from ``pagelist`` (firmware version, WiFi signal,
# IPs…).  Those fields change on the timescale of minutes to days,
# so polling them every tick was wasted bandwidth — the coordinator
# now reuses a cached copy between refreshes.  300 s ≈ 5 min keeps
# the dashboard fresh without making the static endpoint dominate
# our cloud footprint.
STATUS_POLL_INTERVAL_SEC = 300

# ── Live view mode (options flow) ────────────────────────────────────
CONF_LIVE_VIEW_MODE = "live_view_mode"
LIVE_VIEW_MJPEG = "mjpeg"
LIVE_VIEW_HLS = "hls"
DEFAULT_LIVE_VIEW_MODE = LIVE_VIEW_MJPEG

# MJPEG transcoder defaults (ffmpeg HEVC → JPEG)
MJPEG_DEFAULT_FPS = 8
MJPEG_DEFAULT_WIDTH = 1280
MJPEG_DEFAULT_HEIGHT = 720
MJPEG_DEFAULT_QUALITY = 5  # ffmpeg -q:v scale (2 best, 31 worst)

# ── Cloud HTTP identity ──────────────────────────────────────────────
# User-Agent sent on cloud snapshot fetches.  EZVIZ's CDN expects a
# branded UA; using the official app's prefix avoids 403s from anti-
# scraper rules without pretending to be a specific app version.
CLOUD_USER_AGENT = "EZVIZ/5.0"
