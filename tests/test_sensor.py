"""Phase 2.4 — coverage for ``sensor.py``.

Most of ``sensor.py`` is pure data: the ``SENSORS`` table, the small
``_dig`` traversal helper and a handful of ``lambda`` transforms.
``Hp7Sensor`` itself is a thin ``CoordinatorEntity`` wrapper.  Tests
here stay HA-free by mocking the coordinator with ``MagicMock`` and
calling ``async_setup_entry`` directly with a dict for ``hass.data``.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any
from unittest.mock import MagicMock

import pytest
from homeassistant.components.sensor import SensorDeviceClass
from homeassistant.helpers.entity import EntityCategory

from custom_components.ezviz_hp7.const import DOMAIN
from custom_components.ezviz_hp7.sensor import (
    DIAGNOSTIC_KEYS,
    SENSORS,
    Hp7Sensor,
    _dig,
    async_setup_entry,
)

# ── _dig ────────────────────────────────────────────────────────────


def test_dig_returns_top_level_value() -> None:
    assert _dig({"name": "Doorbell"}, "name") == "Doorbell"


def test_dig_walks_dotted_path() -> None:
    assert _dig({"a": {"b": {"c": 42}}}, "a.b.c") == 42


def test_dig_returns_default_when_key_missing() -> None:
    assert _dig({"a": 1}, "b", default="fallback") == "fallback"


def test_dig_returns_default_when_intermediate_is_not_a_dict() -> None:
    assert _dig({"a": "scalar"}, "a.b", default=None) is None


def test_dig_default_is_none_unless_overridden() -> None:
    assert _dig({}, "missing") is None


# ── SENSORS table shape ────────────────────────────────────────────


@pytest.mark.parametrize("cfg", SENSORS, ids=lambda c: c[0])
def test_sensors_table_shape(cfg: tuple) -> None:
    key, translation_key, device_class, unit, icon, transform = cfg
    assert isinstance(key, str) and key
    assert isinstance(translation_key, str) and translation_key
    assert device_class is None or isinstance(device_class, SensorDeviceClass)
    assert unit is None or isinstance(unit, str)
    assert icon is None or icon.startswith("mdi:")
    assert transform is None or callable(transform)


def test_diagnostic_keys_are_a_subset_of_sensor_keys() -> None:
    sensor_keys = {cfg[0] for cfg in SENSORS}
    assert DIAGNOSTIC_KEYS.issubset(sensor_keys)


# ── Transforms ──────────────────────────────────────────────────────


def _transform_for(key: str):
    """Pull the transform lambda for a given SENSORS key."""
    return next(cfg[5] for cfg in SENSORS if cfg[0] == key)


@pytest.mark.parametrize(
    "raw, expected",
    [
        (1, "online"),
        ("1", "online"),
        (True, "online"),
        ("online", "online"),
        (0, "offline"),
        ("0", "offline"),
        ("offline", "offline"),
        (None, "offline"),
        (False, "offline"),  # ``False`` is NOT in the truthy tuple
    ],
)
def test_status_transform(raw: Any, expected: str) -> None:
    assert _transform_for("status")(raw) == expected


@pytest.mark.parametrize(
    "raw, expected",
    [
        (1, "yes"),
        ("1", "yes"),
        (True, "yes"),
        ("true", "yes"),
        (0, "no"),
        ("0", "no"),
        (None, "no"),
        ("anything", "no"),
    ],
)
def test_upgrade_available_transform(raw: Any, expected: str) -> None:
    assert _transform_for("upgrade_available")(raw) == expected


@pytest.mark.parametrize(
    "raw, expected",
    [
        (80, 80),
        (80.5, 80.5),
        ("80", None),  # str is not int/float — dropped
        (None, None),
        (True, True),  # bool is a subclass of int — accepted; documents reality
    ],
)
def test_signal_transform(raw: Any, expected: Any) -> None:
    assert _transform_for("signal")(raw) == expected


# ── Hp7Sensor ───────────────────────────────────────────────────────


def _make_sensor(
    path: str = "name",
    *,
    coordinator_data: dict[str, Any] | None = None,
    transform: Any = None,
    device_class: SensorDeviceClass | None = None,
    unit: str | None = None,
    icon: str | None = None,
) -> Hp7Sensor:
    coord = MagicMock()
    coord.data = coordinator_data if coordinator_data is not None else {}
    return Hp7Sensor(
        coord, "SERIAL-ABC", path, "trans_key", device_class, unit, icon, transform
    )


def test_hp7_sensor_unique_id_includes_path_with_dots_replaced() -> None:
    s = _make_sensor("wifi.signal")
    assert s.unique_id == f"{DOMAIN}_SERIAL-ABC_sensor_wifi_signal"


def test_hp7_sensor_returns_unit_and_icon_from_constructor() -> None:
    s = _make_sensor(unit="%", icon="mdi:wifi")
    assert s.native_unit_of_measurement == "%"
    assert s.icon == "mdi:wifi"


def test_hp7_sensor_native_value_extracts_via_dig() -> None:
    s = _make_sensor("name", coordinator_data={"name": "Doorbell"})
    assert s.native_value == "Doorbell"


def test_hp7_sensor_native_value_applies_transform() -> None:
    s = _make_sensor(
        "status", coordinator_data={"status": 1}, transform=_transform_for("status")
    )
    assert s.native_value == "online"


def test_hp7_sensor_native_value_swallows_transform_errors() -> None:
    def boom(_):
        raise ValueError("nope")

    s = _make_sensor("name", coordinator_data={"name": "raw"}, transform=boom)
    # Transform raised → original value is returned, no exception propagated.
    assert s.native_value == "raw"


def test_hp7_sensor_native_value_returns_none_when_data_missing() -> None:
    s = _make_sensor("name", coordinator_data=None)
    assert s.native_value is None


def test_hp7_sensor_native_value_parses_timestamp() -> None:
    s = _make_sensor(
        "last_alarm_time",
        coordinator_data={"last_alarm_time": "2026-05-10 18:47:22"},
        device_class=SensorDeviceClass.TIMESTAMP,
    )
    out = s.native_value
    assert isinstance(out, datetime)
    assert out.year == 2026 and out.month == 5 and out.day == 10
    assert out.tzinfo is not None


def test_hp7_sensor_timestamp_returns_none_on_malformed_value() -> None:
    s = _make_sensor(
        "last_alarm_time",
        coordinator_data={"last_alarm_time": "not-a-date"},
        device_class=SensorDeviceClass.TIMESTAMP,
    )
    assert s.native_value is None


def test_hp7_sensor_timestamp_returns_none_when_value_falsy() -> None:
    s = _make_sensor(
        "last_alarm_time",
        coordinator_data={"last_alarm_time": None},
        device_class=SensorDeviceClass.TIMESTAMP,
    )
    assert s.native_value is None


def test_hp7_sensor_device_info_uses_api_model() -> None:
    coord = MagicMock()
    coord.data = {}
    coord.api = MagicMock(model="CP7")
    s = Hp7Sensor(coord, "S-1", "name", "name", None, None, None, None)
    info = s.device_info
    assert info["model"] == "CP7"
    assert (DOMAIN, "S-1") in info["identifiers"]


def test_hp7_sensor_device_info_falls_back_to_hp7_when_api_missing() -> None:
    coord = MagicMock(spec=["data"])  # no ``api`` attribute
    coord.data = {}
    s = Hp7Sensor(coord, "S-2", "name", "name", None, None, None, None)
    assert s.device_info["model"] == "HP7"


# ── async_setup_entry ──────────────────────────────────────────────


async def test_async_setup_entry_registers_every_sensor() -> None:
    coord = MagicMock(data={})
    hass = MagicMock()
    hass.data = {DOMAIN: {"entry-id": {"coordinator": coord, "serial": "S"}}}
    entry = MagicMock(entry_id="entry-id")
    add: MagicMock = MagicMock()

    await async_setup_entry(hass, entry, add)

    add.assert_called_once()
    (entities,), _ = add.call_args
    assert len(entities) == len(SENSORS)
    # Diagnostic flag flipped only for keys in DIAGNOSTIC_KEYS.
    for entity, cfg in zip(entities, SENSORS, strict=True):
        if cfg[0] in DIAGNOSTIC_KEYS:
            assert entity._attr_entity_category == EntityCategory.DIAGNOSTIC
            assert entity._attr_entity_registry_enabled_default is False
        else:
            assert getattr(entity, "_attr_entity_category", None) is None
