"""EZVIZ HP7 binary sensor entities."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
)
from homeassistant.core import callback
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.entity import DeviceInfo
from homeassistant.helpers.event import async_call_later
from homeassistant.helpers.update_coordinator import CoordinatorEntity
from homeassistant.util import dt as dt_util

from .const import DOMAIN, SIGNAL_LOCAL_UNLOCK
from .helpers import get_device_info

if TYPE_CHECKING:
    from homeassistant.helpers.event import CALLBACK_TYPE

    from .coordinator import Hp7Coordinator

_LOGGER = logging.getLogger(__name__)

ALARM_FIELD = "alarm_name"
ALARM_CODE_FIELD = "alarm_type_code"
ALARM_TIME_FIELD = "last_alarm_time"
# How long alarm binary sensors stay ON after a fresh cloud event
# before auto-resetting.  Long enough for HA automations to fire on
# the rising edge, short enough that the dashboard doesn't show
# "alarm active" lingering long after the event ended.
PULSE_SECONDS = 3

# Simple binary sensors mapped directly to coordinator data keys.
#
# ``(coord_key, translation_key, device_class, icon)``.  ``device_class``
# may be ``None`` when no HA-standard class fits.
#
# Note (HP7 firmware): an earlier comment here claimed that
# ``Motion_Trigger`` was "permanently OFF for HP7" — that was an
# artefact of the Phase 6 normalisation bug (api.py was feeding
# ``status_alarm_dict`` raw cloud messages with no recognised keys, so
# every field including the motion-window flag silently fell back to
# its default).  Now that ``_normalize_unified_message`` runs again
# this entity reflects the same window the official integration shows.
SIMPLE_MAP: list[tuple[str, str, BinarySensorDeviceClass | None, str | None]] = [
    # User-toggled "Video Encryption" in the EZVIZ app.  When ON the
    # LAN stream stays empty — surfacing it on the dashboard lets the
    # user spot it without reading the log.
    ("image_encryption", "image_encryption", None, "mdi:lock-alert"),
    # Motion window flag — True while the last cloud alarm is still
    # inside pyezvizapi's motion timeout.  Companion to the pulse-style
    # alarm sensors below (those fire for ``PULSE_SECONDS`` on the
    # rising edge of a *new* alarm; this stays ON for the full window).
    (
        "motion_trigger",
        "motion_trigger",
        BinarySensorDeviceClass.MOTION,
        "mdi:motion-sensor",
    ),
]

# Alarm sensors that pulse for PULSE_SECONDS on a matching event.
#
# Each entry can fire from any of three independent sources, in
# order of robustness:
#
# 1. ``match_codes`` — matched against the cloud's ``alarmType``
#    numeric code (language-stable identifier; this is what the
#    integration relies on for non-English EZVIZ accounts).
# 2. ``match_values`` — matched against ``last_alarm_type_name``
#    (the ``sampleName`` field, localised by the cloud to the
#    account language; only works for accounts whose language
#    matches one of the strings we list).
# 3. ``local_actions`` — dispatcher signal emitted by HA itself
#    when an unlock button / service runs successfully.  Decouples
#    the sensor from the cloud feed entirely for HA-originated
#    events.
#
# See issue #8 — Italian / Spanish accounts never matched the
# English ``sampleName`` and saw the binary sensors stuck OFF
# even when the cloud event arrived.  Codes get filled in as
# real-world captures come in; an entry with no codes yet still
# works for its language variants + local actions.
ALARM_MAP: list[
    tuple[
        list[str],
        str,
        BinarySensorDeviceClass | None,
        str,
        list[str],
        list[str],
    ]
] = [
    (
        ["Smart Detection Alarm"],
        "smart_detection_alarm",
        None,
        "mdi:run",
        [],
        ["10079"],
    ),
    (
        ["Intelligent Detection Alarm"],
        "intelligent_detection_alarm",
        None,
        "mdi:account-search",
        [],
        # Pending real-world capture — likely a person/vehicle ML variant
        # of the Smart Detection family.  Falling back to the localised
        # name only until we see one in the wild.
        [],
    ),
    (
        ["Your doorbell is ringing"],
        "doorbell_ringing",
        None,
        "mdi:doorbell",
        [],
        ["2701"],
    ),
    (
        ["EZVIZ app open the gate", "Monitor open the gate"],
        "gate_open",
        None,
        "mdi:gate-open",
        ["unlock_gate"],
        ["10243"],
    ),
    (
        ["EZVIZ app unlock the lock", "Monitor unlock the lock"],
        "unlock_lock",
        None,
        "mdi:lock-open-variant",
        ["unlock_door"],
        ["10242"],
    ),
]


def _to_bool(value: Any) -> bool:
    """Convert various types to boolean.

    Args:
        value: Value to convert.

    Returns:
        Boolean representation of the value.
    """
    if isinstance(value, bool):
        return value
    if value is None:
        return False
    if isinstance(value, (int, float)):
        return value != 0
    if isinstance(value, str):
        return value.strip().lower() in ("1", "true", "on", "yes", "y")
    return False


async def async_setup_entry(hass, entry, async_add_entities) -> None:
    """Set up EZVIZ HP7 binary sensor entities.

    Args:
        hass: Home Assistant instance.
        entry: Config entry.
        async_add_entities: Callback to add entities.
    """
    data: dict[str, Any] = hass.data[DOMAIN][entry.entry_id]
    coordinator: Hp7Coordinator = data["coordinator"]
    serial: str = data["serial"]

    entities: list[BinarySensorEntity] = []

    for key, translation_key, device_class, icon in SIMPLE_MAP:
        entities.append(
            Hp7BinarySimple(
                coordinator, serial, key, translation_key, device_class, icon
            )
        )

    for (
        match_values,
        translation_key,
        device_class,
        icon,
        local_actions,
        match_codes,
    ) in ALARM_MAP:
        entities.append(
            Hp7BinaryAlarm(
                coordinator,
                serial,
                match_values,
                translation_key,
                device_class,
                icon,
                local_actions=local_actions,
                match_codes=match_codes,
            )
        )

    async_add_entities(entities)


class Hp7BinarySimple(CoordinatorEntity, BinarySensorEntity):
    """Simple binary sensor that directly maps to coordinator data."""

    _attr_has_entity_name = True

    def __init__(
        self,
        coordinator: Hp7Coordinator,
        serial: str,
        key: str,
        translation_key: str,
        device_class: BinarySensorDeviceClass | None,
        icon: str | None = None,
    ) -> None:
        """Initialize binary sensor entity."""
        super().__init__(coordinator)
        self._serial = serial
        self._key = key
        self._attr_translation_key = translation_key
        self._attr_unique_id = f"{DOMAIN}_{serial}_binary_{key}"
        self._attr_device_class = device_class
        if icon is not None:
            self._attr_icon = icon

    @property
    def is_on(self) -> bool:
        """Return True if sensor is on."""
        data = self.coordinator.data or {}
        val = data.get(self._key)
        return _to_bool(val)

    @property
    def device_info(self) -> DeviceInfo:
        """Return device information (shared across all platforms)."""
        return get_device_info(self._serial, getattr(self.coordinator, "api", None))


class Hp7BinaryAlarm(CoordinatorEntity, BinarySensorEntity):
    """Binary sensor that pulses briefly when alarm is triggered.

    This sensor stays ON for PULSE_SECONDS after detecting a matching alarm,
    then returns to OFF. This is useful for automations that react to events.
    """

    _attr_has_entity_name = True

    def __init__(
        self,
        coordinator: Hp7Coordinator,
        serial: str,
        match_values: list[str],
        translation_key: str,
        device_class: BinarySensorDeviceClass | None,
        icon: str,
        local_actions: list[str] | None = None,
        match_codes: list[str] | None = None,
    ) -> None:
        """Initialize alarm binary sensor entity.

        Args:
            coordinator: Data coordinator.
            serial: Device serial number.
            match_values: Localised alarm names to trigger on (legacy /
                English-account fallback; cloud translates these).
            translation_key: i18n translation key.
            device_class: Device class for sensor.
            icon: Icon to display.
            local_actions: HA action names (e.g. ``"unlock_gate"``)
                that, when emitted on the local dispatcher signal,
                pulse this sensor without consulting the cloud feed.
            match_codes: Language-stable ``alarmType`` numeric codes
                to trigger on.  Preferred over ``match_values`` when
                populated.
        """
        super().__init__(coordinator)
        self._serial = serial
        self._match_values = match_values
        self._match_codes = match_codes or []
        self._local_actions = local_actions or []
        self._attr_translation_key = translation_key
        self._attr_unique_id = f"{DOMAIN}_{serial}_alarm_{translation_key}"
        self._attr_device_class = device_class
        self._attr_icon = icon
        self._last_trigger: dt_util.datetime | None = None
        self._prev_alarm_time: str | None = None
        # Tracks whether we've seen at least one coordinator update — the
        # first one after a HA restart usually carries an old alarm from
        # the cloud's timeline (sometimes hours stale), and we don't want
        # to pulse the sensor "ON" just because of the cold-start snapshot.
        # The first matching update seeds ``_prev_alarm_time`` silently;
        # only later updates with a *newer* time fire the pulse.
        self._seen_first_update = False
        self._off_unsub: CALLBACK_TYPE | None = None

    async def async_added_to_hass(self) -> None:
        """Subscribe to local unlock signal when this sensor opts in."""
        await super().async_added_to_hass()
        if self._local_actions:
            self.async_on_remove(
                async_dispatcher_connect(
                    self.hass,
                    SIGNAL_LOCAL_UNLOCK,
                    self._handle_local_unlock,
                )
            )

    @callback
    def _handle_local_unlock(self, serial: str, action: str) -> None:
        """Pulse this sensor in response to a local HA unlock action."""
        if serial != self._serial or action not in self._local_actions:
            return
        self._last_trigger = dt_util.utcnow()
        self._schedule_state_update()
        _LOGGER.debug(
            "Local pulse for %s via %s (%s)",
            self._attr_translation_key,
            action,
            self._serial,
        )
        self.async_write_ha_state()

    @property
    def is_on(self) -> bool:
        """Return True if recently triggered (within PULSE_SECONDS)."""
        if self._last_trigger is None:
            return False
        delta = (dt_util.utcnow() - self._last_trigger).total_seconds()
        return delta < PULSE_SECONDS

    def _schedule_state_update(self) -> None:
        """Schedule state update to turn off after PULSE_SECONDS."""
        if self._off_unsub:
            self._off_unsub()

        def _cb(_now: dt_util.datetime) -> None:
            self._off_unsub = None
            self.hass.add_job(self.async_write_ha_state)

        self._off_unsub = async_call_later(self.hass, PULSE_SECONDS, _cb)

    def _matches_alarm(self, name: Any, code: Any) -> bool:
        """Return True if this sensor should fire for ``(name, code)``.

        Code match is checked first because it is language-stable; the
        localised name is only consulted as a fallback for accounts
        whose language matches one of our seeded variants.
        """
        if code and self._match_codes and str(code) in self._match_codes:
            return True
        return name in self._match_values

    @callback
    def _handle_coordinator_update(self) -> None:
        """Handle coordinator data update event.

        Detects new alarms and triggers pulse on matching sensor.
        """
        data = self.coordinator.data or {}
        current_alarm = data.get(ALARM_FIELD)
        current_code = data.get(ALARM_CODE_FIELD)
        current_alarm_time = data.get(ALARM_TIME_FIELD)

        if not self._seen_first_update:
            # Seed ``_prev_alarm_time`` from the cold-start snapshot
            # without pulsing — the alarm the cloud surfaces on the
            # first poll is whatever event happens to be latest in
            # the timeline, which can be hours stale and is not a
            # "fresh" trigger from HA's point of view.
            self._seen_first_update = True
            self._prev_alarm_time = current_alarm_time
            self.async_write_ha_state()
            return

        # Check if new alarm matches this sensor and is different from last
        if (
            self._matches_alarm(current_alarm, current_code)
            and current_alarm_time is not None
            and current_alarm_time != self._prev_alarm_time
        ):
            self._prev_alarm_time = current_alarm_time
            self._last_trigger = dt_util.utcnow()
            self._schedule_state_update()
            _LOGGER.debug(
                "Alarm triggered for %s: name=%s code=%s (%s)",
                self._attr_translation_key,
                current_alarm,
                current_code,
                self._serial,
            )

        self.async_write_ha_state()

    @property
    def device_info(self) -> DeviceInfo:
        """Return device information (shared across all platforms)."""
        return get_device_info(self._serial, getattr(self.coordinator, "api", None))
