from __future__ import annotations
from typing import Any, Optional
from datetime import datetime, timedelta
from homeassistant.util import dt as dt_util
from homeassistant.components.sensor import SensorEntity, SensorDeviceClass
from homeassistant.helpers.entity import DeviceInfo
from homeassistant.helpers.update_coordinator import CoordinatorEntity
from .const import DOMAIN

def _dig(data: dict, path: str, default=None):
    cur = data
    for p in path.split("."):
        if not isinstance(cur, dict) or p not in cur:
            return default
        cur = cur[p]
    return cur

SENSORS = [
    ("name", "Nome Dispositivo", None, None, "mdi:label", None),
    ("version", "Firmware", None, None, "mdi:update", None),
    ("status", "Stato", None, None, "mdi:power", lambda v: "online" if v == 1 else "offline"),
    ("wifiInfos.signal", "WiFi Segnale", None, "%", "mdi:wifi", None),
    ("local_ip", "IP Locale", None, None, "mdi:ip", None),
    ("wan_ip", "IP WAN", None, None, "mdi:wan", None),
    ("last_alarm_time", "Ultimo Allarme", SensorDeviceClass.TIMESTAMP, None, "mdi:alarm-light",
     lambda v: None if not v else __import__("datetime").datetime.strptime(v, "%Y-%m-%d %H:%M:%S")),
    ("last_alarm_type_name", "Tipo Ultimo Allarme", None, None, "mdi:alarm-light-outline", None),
    ("Seconds_Last_Trigger", "Secondi da Ultimo Trigger", SensorDeviceClass.DURATION, "s", "mdi:timer-outline", None),
]


async def async_setup_entry(hass, entry, async_add_entities):
    data = hass.data[DOMAIN][entry.entry_id]
    coordinator = data["coordinator"]
    serial = data["serial"]
    ents = [Hp7Sensor(coordinator, serial, *cfg) for cfg in SENSORS]
    async_add_entities(ents)

class Hp7Sensor(CoordinatorEntity, SensorEntity):
    _attr_has_entity_name = True

    def __init__(self, coordinator, serial, path, name, device_class, unit, icon, transform):
        super().__init__(coordinator)
        self._serial = serial
        self._path = path
        self._attr_name = name
        self._attr_unique_id = f"{DOMAIN}_{serial}_sensor_{path.replace('.', '_')}"
        self._attr_device_class = device_class
        self._unit = unit
        self._icon = icon
        self._transform = transform

    @property
    def native_unit_of_measurement(self) -> Optional[str]:
        return self._unit

    @property
    def icon(self) -> Optional[str]:
        return self._icon

    @property
    def device_info(self) -> DeviceInfo:
        model = getattr(self.coordinator.api, "model", "HP7")
        return DeviceInfo(
            identifiers={(DOMAIN, self._serial)},
            name=f"EZVIZ {model} ({self._serial})",
            manufacturer="EZVIZ",
            model=model,
        )

    @property
    def native_value(self):
        data = self.coordinator.data or {}
        val = _dig(data, self._path)

        # TIMESTAMP
        if self._attr_device_class == SensorDeviceClass.TIMESTAMP:
            if not val:
                return None
            try:
                from datetime import datetime
                from homeassistant.util import dt as dt_util
                dt = datetime.strptime(val, "%Y-%m-%d %H:%M:%S")
                return dt.replace(tzinfo=dt_util.DEFAULT_TIME_ZONE)
            except Exception:
                return None

        # DURATION
        if self._attr_device_class == SensorDeviceClass.DURATION:
            try:
                return float(val)
            except (TypeError, ValueError):
                return None

        # default
        if self._transform:
            try:
                val = self._transform(val)
            except Exception:
                pass
        return val
    

    @property
    def extra_state_attributes(self) -> dict:
        if self._path != "status":
            return {}
        data = self.coordinator.data or {}
        return {
            "device_category": data.get("device_category"),
            "device_sub_category": data.get("device_sub_category"),
            "alarm_notify": data.get("alarm_notify"),
            "alarm_schedules_enabled": data.get("alarm_schedules_enabled"),
            "PIR_Status": data.get("PIR_Status"),
            "Motion_Trigger": data.get("Motion_Trigger"),
            "cam_timezone": data.get("cam_timezone"),
            "supported_channels": data.get("supported_channels"),
            "ssid": _dig(data, "wifiInfos.ssid"),
            "signal": _dig(data, "wifiInfos.signal"),
        }
