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

# Poll interval in seconds.  Each tick triggers ~2 cloud calls
# (``pagelist`` + ``unifiedmsg/list``), so this directly controls the
# integration's footprint on the EZVIZ servers.  The previous value of
# 2 s meant ~3,600 calls per hour per device, which is high enough to
# attract rate-limiting / abuse-detection on the EZVIZ side.  At 15 s
# we still pick up doorbell ring / motion events within a second or
# two on average (cloud lags ~3 s anyway), but generate ~12x fewer
# requests.  Considered exposing this in the options flow but the
# default is fine for the typical home doorbell case.
UPDATE_INTERVAL_SEC = 15

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
