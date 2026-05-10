"""Constants for EZVIZ HP7 integration."""

DOMAIN = "ezviz_hp7"
CONF_REGION = "region"
CONF_SERIAL = "serial"
CONF_CAMERA_PASSWORD = "camera_password"

# Platforms to set up
PLATFORMS = ["button", "sensor", "binary_sensor", "camera", "select"]

# Poll interval in seconds (2 seconds for fast event detection)
UPDATE_INTERVAL_SEC = 2

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
