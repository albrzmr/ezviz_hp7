from __future__ import annotations
from homeassistant.components.camera import Camera, CameraEntityFeature
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.update_coordinator import CoordinatorEntity
from homeassistant.helpers.entity import DeviceInfo
from .const import DOMAIN

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
        self._attr_supported_features = CameraEntityFeature.STREAM

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
            return None

        session = async_get_clientsession(self.hass)
        # Primo tentativo “semplice”
        try:
            async with session.get(url, timeout=15) as resp:
                if resp.status == 200:
                    return await resp.read()
        except Exception:
            pass

        headers = {
            "User-Agent": "EZVIZ/6.9.5 (HomeAssistant)",
            "Accept": "*/*",
            "Accept-Encoding": "gzip, deflate, br",
        }
        try:
            async with session.get(url, headers=headers, timeout=15, allow_redirects=True) as resp:
                if resp.status == 200:
                    return await resp.read()
        except Exception:
            return None

        return None

    async def stream_source(self) -> str | None:
        """Restituisce l'URL RTSP per lo streaming live."""
        data = self.coordinator.data or {}
        ip = data.get("local_ip")
        port = data.get("local_rtsp_port") or "554"
        password = data.get("rtsp_password")
        if ip and password:
            return f"rtsp://admin:{password}@{ip}:{port}/Streaming/Channels/101/"
        return None
