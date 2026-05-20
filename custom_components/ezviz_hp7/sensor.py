"""EZVIZ HP7 sensor entities."""

from __future__ import annotations

import logging
from datetime import datetime
from typing import TYPE_CHECKING, Any

from homeassistant.components.sensor import SensorDeviceClass, SensorEntity
from homeassistant.helpers.entity import DeviceInfo, EntityCategory
from homeassistant.helpers.update_coordinator import CoordinatorEntity
from homeassistant.util import dt as dt_util

from .const import DOMAIN
from .helpers import get_device_info

if TYPE_CHECKING:
    from .coordinator import Hp7Coordinator

_LOGGER = logging.getLogger(__name__)

# Keys that should be treated as diagnostic (hidden by default)
DIAGNOSTIC_KEYS = {
    "signal",
    "ssid",
    "local_ip",
    "wan_ip",
    "upgrade_available",
    "seconds_last_trigger",
}

# Sensor configuration: (key, translation_key, device_class, unit, icon, transform_fn)
SENSORS: list[
    tuple[str, str, SensorDeviceClass | None, str | None, str | None, Any]
] = [
    # Identity and basic status
    ("name", "name", None, None, "mdi:label", None),
    ("version", "version", None, None, "mdi:update", None),
    (
        "status",
        "status",
        None,
        None,
        "mdi:power",
        lambda v: "online" if v in (1, "1", True, "online") else "offline",
    ),
    # Network
    (
        "signal",
        "signal",
        None,
        "%",
        "mdi:wifi",
        lambda v: v if isinstance(v, (int, float)) else None,
    ),
    ("ssid", "ssid", None, None, "mdi:wifi", None),
    ("local_ip", "local_ip", None, None, "mdi:ip", None),
    ("wan_ip", "wan_ip", None, None, "mdi:wan", None),
    # NOTE: the upstream "motion" sensor used to be here but was always
    # ``"none"`` on HP7 / CP7 — the firmware never populates the
    # ``Motion_Trigger`` field that backs it (verified empirically over
    # 240+ polls).  Use ``binary_sensor.smart_detection_alarm`` /
    # ``binary_sensor.intelligent_detection_alarm`` /
    # ``binary_sensor.doorbell_ringing`` instead, which pulse for
    # ``PULSE_SECONDS`` whenever a fresh cloud alarm arrives.
    # ── Last events / diagnostics ────────────────────────────────────
    (
        "last_alarm_time",
        "last_alarm_time",
        SensorDeviceClass.TIMESTAMP,
        None,
        "mdi:clock-alert",
        None,
    ),
    ("alarm_name", "alarm_name", None, None, "mdi:alert", None),
    # ``alarm_type_code`` is the language-stable ``ext.alarmType``
    # numeric id (e.g. ``10079`` for Smart Detection, ``10243`` for
    # gate open).  Exposed so users with EZVIZ accounts in languages
    # we don't have name-matches for can build automations against a
    # field that does not change with locale.  See issue #8.
    ("alarm_type_code", "alarm_type_code", None, None, "mdi:numeric", None),
    (
        "seconds_last_trigger",
        "seconds_last_trigger",
        SensorDeviceClass.DURATION,
        "s",
        "mdi:timer",
        None,
    ),
    # Firmware updates
    (
        "upgrade_available",
        "upgrade_available",
        None,
        None,
        "mdi:update",
        lambda v: "yes" if v in (1, "1", True, "true") else "no",
    ),
]


def _dig(data: dict[str, Any], path: str, default: Any = None) -> Any:
    """Recursively get value from nested dictionary.

    Args:
        data: Dictionary to search.
        path: Dot-separated path (e.g., "wifi.signal").
        default: Default value if path not found.

    Returns:
        Value at path or default.
    """
    cur = data
    for p in path.split("."):
        if not isinstance(cur, dict) or p not in cur:
            return default
        cur = cur[p]
    return cur


async def async_setup_entry(hass, entry, async_add_entities) -> None:
    """Set up EZVIZ HP7 sensor entities.

    Args:
        hass: Home Assistant instance.
        entry: Config entry.
        async_add_entities: Callback to add entities.
    """
    data: dict[str, Any] = hass.data[DOMAIN][entry.entry_id]
    coordinator: Hp7Coordinator = data["coordinator"]
    serial: str = data["serial"]

    entities: list[Hp7Sensor] = []
    for cfg in SENSORS:
        entity = Hp7Sensor(coordinator, serial, *cfg)
        if cfg[0] in DIAGNOSTIC_KEYS:
            entity._attr_entity_category = EntityCategory.DIAGNOSTIC
            entity._attr_entity_registry_enabled_default = False
        entities.append(entity)

    async_add_entities(entities)


class Hp7Sensor(CoordinatorEntity, SensorEntity):
    """Sensor entity for EZVIZ HP7 device status."""

    _attr_has_entity_name = True

    def __init__(
        self,
        coordinator: Hp7Coordinator,
        serial: str,
        path: str,
        translation_key: str,
        device_class: SensorDeviceClass | None,
        unit: str | None,
        icon: str | None,
        transform: Any = None,
    ) -> None:
        """Initialize sensor entity.

        Args:
            coordinator: Data coordinator.
            serial: Device serial number.
            path: Dot-separated path to value in coordinator data.
            translation_key: i18n translation key.
            device_class: Device class for sensor.
            unit: Unit of measurement.
            icon: Icon to display.
            transform: Optional transform function for values.
        """
        super().__init__(coordinator)
        self._serial = serial
        self._path = path
        self._attr_translation_key = translation_key
        self._attr_unique_id = f"{DOMAIN}_{serial}_sensor_{path.replace('.', '_')}"
        self._attr_device_class = device_class
        self._unit = unit
        self._icon = icon
        self._transform = transform

    @property
    def native_unit_of_measurement(self) -> str | None:
        """Return the unit of measurement."""
        return self._unit

    @property
    def icon(self) -> str | None:
        """Return the icon."""
        return self._icon

    @property
    def device_info(self) -> DeviceInfo:
        """Return device information (shared across all platforms)."""
        return get_device_info(self._serial, getattr(self.coordinator, "api", None))

    @property
    def native_value(self) -> Any:
        """Return the sensor value."""
        data = self.coordinator.data or {}
        val = _dig(data, self._path)

        # Handle timestamp values
        if self._attr_device_class == SensorDeviceClass.TIMESTAMP:
            if not val:
                return None
            try:
                dt = datetime.strptime(val, "%Y-%m-%d %H:%M:%S")
                return dt.replace(tzinfo=dt_util.DEFAULT_TIME_ZONE)
            except (ValueError, TypeError, AttributeError):
                _LOGGER.debug("Failed to parse timestamp: %s", val)
                return None

        # Apply optional transform
        if self._transform:
            try:
                val = self._transform(val)
            except (ValueError, TypeError, AttributeError):
                _LOGGER.debug("Transform failed for %s: %s", self._path, val)

        return val
