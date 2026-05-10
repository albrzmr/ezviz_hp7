"""EZVIZ HP7/CP7 camera entity — live stream via cpd7 LAN pipeline.

Two live-view modes are supported, selectable from the integration's
Options flow (Settings → Devices & Services → EZVIZ HP7 → Configure):

- ``mjpeg`` (default): a small ffmpeg subprocess transcodes the upstream
  HEVC into motion JPEG that the browser/Companion app can render with
  ~500 ms latency.  Compatible with every browser; uses ~30-50 % of one
  CPU core while a viewer is connected.
- ``hls``: the entity exposes ``CameraEntityFeature.STREAM`` and lets HA's
  Stream component mux the upstream HEVC into HLS.  Higher quality (2K
  HEVC, native 25 fps) but ~10-20 s of delay; needs an HEVC-capable
  browser/device (Safari / iOS / Android with hardware decoding).
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from aiohttp import web
from homeassistant.components.camera import Camera, CameraEntityFeature
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.entity import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import (
    DEFAULT_LIVE_VIEW_MODE,
    DOMAIN,
    LIVE_VIEW_HLS,
    LIVE_VIEW_MJPEG,
    MJPEG_DEFAULT_FPS,
    MJPEG_DEFAULT_HEIGHT,
    MJPEG_DEFAULT_QUALITY,
    MJPEG_DEFAULT_WIDTH,
)
from .helpers import get_device_info
from .mjpeg import serve_mjpeg

if TYPE_CHECKING:
    from aiohttp import ClientSession

    from .coordinator import Hp7Coordinator
    from .tcp_relay import CpdMpegPsRelay

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up EZVIZ HP7/CP7 camera entities."""
    data: dict[str, Any] = hass.data[DOMAIN][entry.entry_id]
    coordinator: Hp7Coordinator = data["coordinator"]
    serial: str = data["serial"]
    relay: CpdMpegPsRelay | None = data.get("relay")
    mode: str = data.get("live_view_mode", DEFAULT_LIVE_VIEW_MODE)
    stats = data.get("stats")
    async_add_entities([Hp7Camera(hass, coordinator, serial, relay, mode, stats)])


class Hp7Camera(Camera, CoordinatorEntity):
    """Camera entity for the EZVIZ HP7/CP7 doorbell."""

    _attr_has_entity_name = True
    # ``"Stream"`` is universally understood and avoids an extra
    # translation indirection — the device name (``EZVIZ CP7 (...)``)
    # is prefixed automatically by ``has_entity_name``.
    _attr_name = "Stream"

    def __init__(
        self,
        hass: HomeAssistant,
        coordinator: Hp7Coordinator,
        serial: str,
        relay: CpdMpegPsRelay | None,
        live_view_mode: str = DEFAULT_LIVE_VIEW_MODE,
        stats: Any = None,
    ) -> None:
        Camera.__init__(self)
        CoordinatorEntity.__init__(self, coordinator)
        self.hass = hass
        self._serial = serial
        self._relay = relay
        self._live_view_mode = live_view_mode
        self._stats = stats
        self._attr_unique_id = f"{DOMAIN}_{serial}_camera"
        # In HLS mode we let HA's Stream component handle live view; in
        # MJPEG mode we override ``handle_async_mjpeg_stream`` instead, so
        # the STREAM feature is intentionally NOT advertised.
        if live_view_mode == LIVE_VIEW_HLS:
            self._attr_supported_features = CameraEntityFeature.STREAM
        else:
            self._attr_supported_features = CameraEntityFeature(0)

    # ── State (idle / streaming) ──────────────────────────────────────────
    #
    # HA's ``Camera.state`` returns ``streaming`` when ``is_streaming`` is
    # truthy and ``idle`` otherwise — we report ``streaming`` whenever
    # the relay either has a viewer attached or is pre-warmed (waiting
    # for a viewer after a doorbell event).  That gives the user a
    # meaningful state on the dashboard instead of the perpetual
    # ``idle`` ("Inactivo") that comes with the default ``Camera``
    # implementation.

    @property
    def is_streaming(self) -> bool:
        """Reflect the actual upstream / viewer state of the relay."""
        if self._relay is None:
            return False
        return self._relay.has_active_viewer or self._relay.is_warm

    @property
    def available(self) -> bool:
        """Camera is available whenever the doorbell is online and reachable.

        Defers to ``Camera.available`` (which checks the HLS stream
        when in HLS mode) but additionally requires the cloud
        coordinator to have a known LAN IP and a non-offline status —
        otherwise the dashboard would show ``streaming`` for an
        unreachable doorbell.
        """
        if not super().available:
            return False
        data = self.coordinator.data or {}
        status = (data.get("status") or "").lower()
        if status and status in {"offline", "unreachable"}:
            return False
        return bool(data.get("local_ip"))

    @property
    def device_info(self) -> DeviceInfo:
        return get_device_info(self._serial, getattr(self.coordinator, "api", None))

    # ── HLS path ──────────────────────────────────────────────────────────

    async def stream_source(self) -> str | None:
        """Return the local TCP relay URL for HA's Stream component (HLS)."""
        if self._live_view_mode != LIVE_VIEW_HLS:
            return None
        if self._relay is None or self._relay.port == 0:
            return None
        host = (self.coordinator.data or {}).get("local_ip") or ""
        if not host or host == "0.0.0.0":
            return None
        return self._relay.url

    # ── MJPEG path ────────────────────────────────────────────────────────

    async def handle_async_mjpeg_stream(
        self, request: web.Request
    ) -> web.StreamResponse | None:
        """Serve a continuous low-latency MJPEG stream to the client.

        Only active in MJPEG mode.  In HLS mode we defer to the parent
        class implementation, which polls ``async_camera_image`` — useful
        for pages that pre-fetch a thumbnail.
        """
        if self._live_view_mode != LIVE_VIEW_MJPEG:
            return await super().handle_async_mjpeg_stream(request)

        if self._relay is None or self._relay.port == 0:
            _LOGGER.debug("MJPEG: relay not running")
            return None
        host = (self.coordinator.data or {}).get("local_ip") or ""
        if not host or host == "0.0.0.0":
            _LOGGER.debug("MJPEG: doorbell LAN IP not yet known")
            return None

        return await serve_mjpeg(
            request,
            upstream_url=self._relay.url,
            fps=MJPEG_DEFAULT_FPS,
            width=MJPEG_DEFAULT_WIDTH,
            height=MJPEG_DEFAULT_HEIGHT,
            quality=MJPEG_DEFAULT_QUALITY,
            stats=self._stats,
        )

    # ── Snapshot fallback ─────────────────────────────────────────────────

    async def async_camera_image(
        self,
        width: int | None = None,
        height: int | None = None,
    ) -> bytes | None:
        """Return the last alarm snapshot from the EZVIZ cloud.

        Used by the dashboard for thumbnails and as a fallback when no
        live viewer is connected.
        """
        return await self._cloud_snapshot()

    async def _cloud_snapshot(self) -> bytes | None:
        url = (self.coordinator.data or {}).get("last_alarm_pic")
        if not url:
            _LOGGER.debug("No snapshot URL available for %s", self._serial)
            return None

        try:
            token = self.coordinator.api.token
            if not token:
                _LOGGER.warning("No authentication token available")
                return None

            session: ClientSession = async_get_clientsession(self.hass)
            headers: dict[str, str] = {"User-Agent": "EZVIZ/5.0"}
            access_token = token.get("access_token")
            if access_token:
                headers["Authorization"] = f"Bearer {access_token}"

            async with session.get(url, headers=headers, timeout=15) as resp:
                if resp.status == 200:
                    return await resp.read()
                try:
                    error_text = await resp.text()
                except Exception:
                    error_text = "Unknown error"
                _LOGGER.warning(
                    "Failed to fetch snapshot for %s: HTTP %s - %s",
                    self._serial,
                    resp.status,
                    error_text,
                )
                return None

        except TimeoutError:
            _LOGGER.warning("Timeout fetching snapshot for %s", self._serial)
            return None
        except Exception as exc:
            _LOGGER.warning(
                "Error fetching snapshot for %s: %s",
                self._serial,
                exc,
            )
            return None

    async def _async_get_supported_webrtc_provider(self, *args, **kwargs) -> None:
        """Return WebRTC provider (not implemented yet)."""
        return None
