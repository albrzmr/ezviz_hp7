from __future__ import annotations
import logging

from homeassistant.components.camera import Camera, CameraEntityFeature
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.update_coordinator import CoordinatorEntity
from homeassistant.helpers.entity import DeviceInfo
from .const import DOMAIN


_LOGGER = logging.getLogger(__name__)

async def async_setup_entry(hass, entry, async_add_entities):
    data = hass.data[DOMAIN][entry.entry_id]
    coordinator = data["coordinator"]
    serial = data["serial"]
    async_add_entities([Hp7LastSnapshotCamera(hass, coordinator, serial)])

class Hp7LastSnapshotCamera(CoordinatorEntity, Camera):
    _attr_has_entity_name = True

    def __init__(self, hass, coordinator, serial: str):
        CoordinatorEntity.__init__(self, coordinator)
        Camera.__init__(self)
        self.hass = hass
        self._serial = serial
        self._attr_name = "Ultima Istantanea"
        self._attr_unique_id = f"{DOMAIN}_{serial}_last_snapshot"
        self._attr_supported_features = 0
        self._update_supported_features()

    @property
    def device_info(self) -> DeviceInfo:
        model = getattr(self.coordinator.api, "model", "HP7")
        return DeviceInfo(
            identifiers={(DOMAIN, self._serial)},
            name=f"EZVIZ {model} ({self._serial})",
            manufacturer="EZVIZ",
            model=model,
        )

    async def async_camera_image(self, width: int | None = None, height: int | None = None):
        data = self.coordinator.data or {}
        url = data.get("last_alarm_pic")
        if not url:
            _LOGGER.debug("%s: nessuna last_alarm_pic disponibile", self._serial)
            return None

        session = async_get_clientsession(self.hass)
        # Primo tentativo “semplice”
        try:
            async with session.get(url, timeout=15) as resp:
                _LOGGER.debug(
                    "%s: fetch snapshot status=%s", self._serial, resp.status
                )
                if resp.status == 200:
                    return await resp.read()
                _LOGGER.warning(
                    "%s: snapshot HTTP status %s", self._serial, resp.status
                )
        except Exception as e:
            _LOGGER.debug("%s: primo tentativo snapshot fallito: %s", self._serial, e)

        headers = {
            "User-Agent": "EZVIZ/6.9.5 (HomeAssistant)",
            "Accept": "*/*",
            "Accept-Encoding": "gzip, deflate, br",
        }
        try:
            async with session.get(
                url, headers=headers, timeout=15, allow_redirects=True
            ) as resp:
                _LOGGER.debug(
                    "%s: fetch snapshot (headers) status=%s", self._serial, resp.status
                )
                if resp.status == 200:
                    return await resp.read()
                _LOGGER.warning(
                    "%s: snapshot (headers) HTTP status %s", self._serial, resp.status
                )
        except Exception as e:
            _LOGGER.debug(
                "%s: secondo tentativo snapshot fallito: %s", self._serial, e
            )
            return None

        return None

    async def stream_source(self) -> str | None:
        """Restituisce l'URL RTSP per lo streaming live."""
        return self._build_rtsp_url()

    def _build_rtsp_url(self) -> str | None:
        data = self.coordinator.data or {}
        ip = data.get("local_ip")
        port = data.get("local_rtsp_port") or "554"
        password = data.get("rtsp_password")
        if ip and password:
            url = f"rtsp://admin:{password}@{ip}:{port}/Streaming/Channels/101/"
            _LOGGER.debug("%s: RTSP URL costruito: %s", self._serial, url)
            return url
        _LOGGER.debug(
            "%s: info RTSP mancanti (ip=%s, port=%s, pass=%s)",
            self._serial,
            ip,
            port,
            bool(password),
        )
        return None

    def _update_supported_features(self) -> None:
        self._attr_supported_features = (
            CameraEntityFeature.STREAM if self._build_rtsp_url() else 0
        )
        _LOGGER.debug(
            "%s: supported_features=%s", self._serial, self._attr_supported_features
        )

    def _handle_coordinator_update(self) -> None:
        self._update_supported_features()
        _LOGGER.debug(
            "%s: coordinator data aggiornata", self._serial
        )
        super()._handle_coordinator_update()
