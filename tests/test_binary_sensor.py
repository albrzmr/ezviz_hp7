"""Phase 2.5 — coverage for ``binary_sensor.py``.

The interesting bits are:

* ``_to_bool`` — accepts everything HA might throw at it.
* ``Hp7BinaryAlarm`` — pulses for ``PULSE_SECONDS`` after a matching
  alarm name + a *new* ``last_alarm_time`` arrive, then auto-resets.
* ``Hp7BinarySimple`` — currently unused (``SIMPLE_MAP`` is empty) but
  still construct/exercise it so the class doesn't bit-rot.
* ``async_setup_entry`` — registers one entity per ``ALARM_MAP`` entry.
"""

from __future__ import annotations

from datetime import timedelta
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
from homeassistant.components.binary_sensor import BinarySensorDeviceClass
from homeassistant.util import dt as dt_util

from custom_components.ezviz_hp7.binary_sensor import (
    ALARM_FIELD,
    ALARM_MAP,
    ALARM_TIME_FIELD,
    PULSE_SECONDS,
    SIMPLE_MAP,
    Hp7BinaryAlarm,
    Hp7BinarySimple,
    _to_bool,
    async_setup_entry,
)
from custom_components.ezviz_hp7.const import DOMAIN

# ── _to_bool ────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "raw, expected",
    [
        (True, True),
        (False, False),
        (None, False),
        (1, True),
        (0, False),
        (-3, True),
        (0.0, False),
        (0.5, True),
        ("1", True),
        ("0", False),
        ("true", True),
        ("TRUE", True),
        ("  on  ", True),
        ("yes", True),
        ("y", True),
        ("false", False),
        ("anything-else", False),
        ("", False),
        (object(), False),
    ],
)
def test_to_bool(raw: Any, expected: bool) -> None:
    assert _to_bool(raw) is expected


# ── ALARM_MAP / SIMPLE_MAP shape ───────────────────────────────────


def test_simple_map_is_empty_for_hp7() -> None:
    """The HP7 firmware doesn't populate any simple-bool fields.
    Keeping the list empty avoids permanently-OFF zombie sensors."""
    assert SIMPLE_MAP == []


@pytest.mark.parametrize("cfg", ALARM_MAP, ids=lambda c: c[1])
def test_alarm_map_shape(cfg: tuple) -> None:
    match_values, translation_key, device_class, icon = cfg
    assert isinstance(match_values, list) and match_values
    assert all(isinstance(v, str) and v for v in match_values)
    assert isinstance(translation_key, str) and translation_key
    assert device_class is None or isinstance(device_class, BinarySensorDeviceClass)
    assert icon.startswith("mdi:")


# ── Hp7BinarySimple ────────────────────────────────────────────────


def _simple(key: str, data: dict[str, Any]) -> Hp7BinarySimple:
    coord = MagicMock()
    coord.data = data
    return Hp7BinarySimple(coord, "S-1", key, "trans", BinarySensorDeviceClass.RUNNING)


def test_hp7_binary_simple_unique_id_includes_domain_and_key() -> None:
    s = _simple("foo", {"foo": 1})
    assert s.unique_id == f"{DOMAIN}_S-1_binary_foo"


@pytest.mark.parametrize("raw, expected", [(1, True), (0, False), (None, False)])
def test_hp7_binary_simple_is_on_round_trips_to_bool(raw: Any, expected: bool) -> None:
    assert _simple("foo", {"foo": raw}).is_on is expected


def test_hp7_binary_simple_handles_missing_coordinator_data() -> None:
    coord = MagicMock()
    coord.data = None
    s = Hp7BinarySimple(coord, "S-1", "foo", "trans", BinarySensorDeviceClass.RUNNING)
    assert s.is_on is False


def test_hp7_binary_simple_device_info_uses_api_model() -> None:
    coord = MagicMock()
    coord.data = {}
    coord.api = MagicMock(model="CP7")
    s = Hp7BinarySimple(coord, "S-9", "foo", "trans", BinarySensorDeviceClass.RUNNING)
    info = s.device_info
    assert info["model"] == "CP7"
    assert (DOMAIN, "S-9") in info["identifiers"]


# ── Hp7BinaryAlarm ─────────────────────────────────────────────────


def _alarm(
    match_values: list[str], data: dict[str, Any] | None = None
) -> Hp7BinaryAlarm:
    coord = MagicMock()
    coord.data = data if data is not None else {}
    entity = Hp7BinaryAlarm(coord, "S-1", match_values, "smart", None, "mdi:run")
    # The entity is never added to HA — short-circuit anything that
    # would touch the (missing) entity platform.
    entity.async_write_ha_state = MagicMock()
    entity.hass = MagicMock()
    return entity


def test_hp7_binary_alarm_unique_id_uses_translation_key() -> None:
    a = _alarm(["X"])
    assert a.unique_id == f"{DOMAIN}_S-1_alarm_smart"


def test_hp7_binary_alarm_is_off_when_never_triggered() -> None:
    assert _alarm(["X"]).is_on is False


def test_hp7_binary_alarm_is_on_within_pulse_window(freezer) -> None:
    a = _alarm(["X"])
    a._last_trigger = dt_util.utcnow()
    assert a.is_on is True
    freezer.tick(timedelta(seconds=PULSE_SECONDS - 1))
    assert a.is_on is True


def test_hp7_binary_alarm_is_off_after_pulse_window(freezer) -> None:
    a = _alarm(["X"])
    a._last_trigger = dt_util.utcnow()
    freezer.tick(timedelta(seconds=PULSE_SECONDS + 1))
    assert a.is_on is False


def test_handle_coordinator_update_triggers_on_match() -> None:
    a = _alarm(["Smart Detection Alarm"])
    a.coordinator.data = {
        ALARM_FIELD: "Smart Detection Alarm",
        ALARM_TIME_FIELD: "2026-05-10T18:47:22+00:00",
    }
    with patch(
        "custom_components.ezviz_hp7.binary_sensor.async_call_later"
    ) as call_later:
        a._handle_coordinator_update()

    assert a._last_trigger is not None
    assert a._prev_alarm_time == "2026-05-10T18:47:22+00:00"
    call_later.assert_called_once()
    args, _ = call_later.call_args
    assert args[0] is a.hass and args[1] == PULSE_SECONDS and callable(args[2])
    a.async_write_ha_state.assert_called_once()


def test_handle_coordinator_update_ignores_non_matching_alarm() -> None:
    a = _alarm(["Smart Detection Alarm"])
    a.coordinator.data = {
        ALARM_FIELD: "Some Other Alarm",
        ALARM_TIME_FIELD: "2026-05-10T18:47:22+00:00",
    }
    with patch(
        "custom_components.ezviz_hp7.binary_sensor.async_call_later"
    ) as call_later:
        a._handle_coordinator_update()
    assert a._last_trigger is None
    call_later.assert_not_called()
    a.async_write_ha_state.assert_called_once()


def test_handle_coordinator_update_dedupes_repeat_alarm_time() -> None:
    a = _alarm(["Ring"])
    a._prev_alarm_time = "2026-05-10T18:47:22+00:00"
    a.coordinator.data = {
        ALARM_FIELD: "Ring",
        ALARM_TIME_FIELD: "2026-05-10T18:47:22+00:00",
    }
    with patch(
        "custom_components.ezviz_hp7.binary_sensor.async_call_later"
    ) as call_later:
        a._handle_coordinator_update()
    assert a._last_trigger is None
    call_later.assert_not_called()


def test_handle_coordinator_update_skips_without_alarm_time() -> None:
    a = _alarm(["Ring"])
    a.coordinator.data = {ALARM_FIELD: "Ring", ALARM_TIME_FIELD: None}
    with patch(
        "custom_components.ezviz_hp7.binary_sensor.async_call_later"
    ) as call_later:
        a._handle_coordinator_update()
    assert a._last_trigger is None
    call_later.assert_not_called()


def test_schedule_state_update_cancels_pending_off_handle() -> None:
    a = _alarm(["X"])
    old_unsub = MagicMock()
    a._off_unsub = old_unsub
    with patch(
        "custom_components.ezviz_hp7.binary_sensor.async_call_later",
        return_value=MagicMock(),
    ):
        a._schedule_state_update()
    old_unsub.assert_called_once()
    assert a._off_unsub is not None


def test_schedule_state_update_callback_fires_write_state() -> None:
    """The scheduled callback must clear ``_off_unsub`` and push a state write."""
    a = _alarm(["X"])
    with patch(
        "custom_components.ezviz_hp7.binary_sensor.async_call_later"
    ) as call_later:
        a._schedule_state_update()

    # Invoke the captured callback as ``async_call_later`` would.
    cb = call_later.call_args[0][2]
    cb(dt_util.utcnow())

    assert a._off_unsub is None
    a.hass.add_job.assert_called_once_with(a.async_write_ha_state)


def test_hp7_binary_alarm_device_info_falls_back_to_hp7() -> None:
    coord = MagicMock(spec=["data"])
    coord.data = {}
    a = Hp7BinaryAlarm(coord, "S-7", ["X"], "smart", None, "mdi:run")
    assert a.device_info["model"] == "HP7"


# ── async_setup_entry ──────────────────────────────────────────────


async def test_async_setup_entry_registers_one_entity_per_alarm() -> None:
    coord = MagicMock(data={})
    hass = MagicMock()
    hass.data = {DOMAIN: {"entry-id": {"coordinator": coord, "serial": "S"}}}
    entry = MagicMock(entry_id="entry-id")
    add: MagicMock = MagicMock()

    await async_setup_entry(hass, entry, add)

    add.assert_called_once()
    (entities,), _ = add.call_args
    assert len(entities) == len(SIMPLE_MAP) + len(ALARM_MAP)
    assert all(isinstance(e, Hp7BinaryAlarm) for e in entities)
