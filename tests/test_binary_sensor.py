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
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from homeassistant.components.binary_sensor import BinarySensorDeviceClass
from homeassistant.util import dt as dt_util

from custom_components.ezviz_hp7.binary_sensor import (
    ALARM_CODE_FIELD,
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
from custom_components.ezviz_hp7.const import DOMAIN, SIGNAL_LOCAL_UNLOCK

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


def test_simple_map_contains_image_encryption_and_motion_trigger() -> None:
    """Both simple bools we surface today must remain in the map —
    ``image_encryption`` flags Video Encryption (an integration-blocker)
    and ``motion_trigger`` mirrors the official integration's motion
    window so dashboards work consistently between forks."""
    keys = {cfg[0] for cfg in SIMPLE_MAP}
    assert "image_encryption" in keys
    assert "motion_trigger" in keys
    for cfg in SIMPLE_MAP:
        data_key, translation_key, _device_class, icon = cfg
        assert data_key
        assert translation_key
        assert icon.startswith("mdi:")


@pytest.mark.parametrize("cfg", ALARM_MAP, ids=lambda c: c[1])
def test_alarm_map_shape(cfg: tuple) -> None:
    match_values, translation_key, device_class, icon, local_actions, match_codes = cfg
    assert isinstance(match_values, list) and match_values
    assert all(isinstance(v, str) and v for v in match_values)
    assert isinstance(translation_key, str) and translation_key
    assert device_class is None or isinstance(device_class, BinarySensorDeviceClass)
    assert icon.startswith("mdi:")
    assert isinstance(local_actions, list)
    assert all(isinstance(v, str) and v for v in local_actions)
    assert isinstance(match_codes, list)
    assert all(isinstance(v, str) and v for v in match_codes)


def test_unlock_sensors_have_local_actions() -> None:
    """gate_open / unlock_lock must have a local-dispatcher fallback —
    that's the whole point of issue #8.  If the next refactor strips
    the field by accident, this test catches it loudly."""
    by_key = {cfg[1]: cfg for cfg in ALARM_MAP}
    assert "unlock_gate" in by_key["gate_open"][4]
    assert "unlock_door" in by_key["unlock_lock"][4]


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
    match_values: list[str],
    data: dict[str, Any] | None = None,
    local_actions: list[str] | None = None,
    match_codes: list[str] | None = None,
    seen_first_update: bool = True,
) -> Hp7BinaryAlarm:
    coord = MagicMock()
    coord.data = data if data is not None else {}
    entity = Hp7BinaryAlarm(
        coord,
        "S-1",
        match_values,
        "smart",
        None,
        "mdi:run",
        local_actions=local_actions,
        match_codes=match_codes,
    )
    # Skip the cold-start seeding hop by default — most tests exercise
    # the steady-state path; ones that care about first-update behaviour
    # opt out by passing ``seen_first_update=False``.
    entity._seen_first_update = seen_first_update
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


def test_first_coordinator_update_does_not_pulse_stale_alarm() -> None:
    """The very first update after a HA restart seeds ``_prev_alarm_time``
    silently — a cold-start snapshot of a 2h-old doorbell ring is NOT a
    fresh event and must not flip the binary sensor ON for 3 seconds."""
    a = _alarm(["Your doorbell is ringing"], seen_first_update=False)
    a.coordinator.data = {
        ALARM_FIELD: "Your doorbell is ringing",
        ALARM_TIME_FIELD: "2026-05-20 12:00:00",  # stale
    }
    with patch(
        "custom_components.ezviz_hp7.binary_sensor.async_call_later"
    ) as call_later:
        a._handle_coordinator_update()

    assert a._seen_first_update is True
    assert a._prev_alarm_time == "2026-05-20 12:00:00"
    assert a._last_trigger is None
    call_later.assert_not_called()
    a.async_write_ha_state.assert_called_once()


def test_second_update_with_same_alarm_time_after_seed_does_not_pulse() -> None:
    """If the second update still shows the same (stale) alarm_time we
    saw on the cold-start snapshot, still no pulse — only a *new*
    alarm_time counts as a fresh event."""
    a = _alarm(["Ring"], seen_first_update=False)
    a.coordinator.data = {
        ALARM_FIELD: "Ring",
        ALARM_TIME_FIELD: "2026-05-20 12:00:00",
    }
    with patch(
        "custom_components.ezviz_hp7.binary_sensor.async_call_later"
    ) as call_later:
        a._handle_coordinator_update()  # seed
        a._handle_coordinator_update()  # same time → no pulse
    assert a._last_trigger is None
    call_later.assert_not_called()


def test_second_update_with_new_alarm_time_after_seed_pulses() -> None:
    """After the cold-start seed, a genuinely newer ``last_alarm_time``
    must fire the pulse normally."""
    a = _alarm(["Ring"], seen_first_update=False)
    a.coordinator.data = {
        ALARM_FIELD: "Ring",
        ALARM_TIME_FIELD: "2026-05-20 12:00:00",
    }
    with patch(
        "custom_components.ezviz_hp7.binary_sensor.async_call_later"
    ) as call_later:
        a._handle_coordinator_update()  # seed
        a.coordinator.data = {
            ALARM_FIELD: "Ring",
            ALARM_TIME_FIELD: "2026-05-20 12:00:15",  # newer
        }
        a._handle_coordinator_update()
    assert a._last_trigger is not None
    assert a._prev_alarm_time == "2026-05-20 12:00:15"
    call_later.assert_called_once()


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


def test_handle_coordinator_update_triggers_on_code_match() -> None:
    """``alarmType`` code match takes precedence over the localised name.

    This is the path that lets non-English EZVIZ accounts trigger the
    sensors at all — the cloud translates ``sampleName`` but not the
    numeric code.  See issue #8.
    """
    a = _alarm(["English Name We Don't Speak"], match_codes=["3001"])
    a.coordinator.data = {
        ALARM_FIELD: "L'app EZVIZ apre il cancello",  # localised, doesn't match name list
        ALARM_CODE_FIELD: "3001",
        ALARM_TIME_FIELD: "2026-05-10T18:47:22+00:00",
    }
    with patch(
        "custom_components.ezviz_hp7.binary_sensor.async_call_later"
    ) as call_later:
        a._handle_coordinator_update()

    assert a._last_trigger is not None
    assert a._prev_alarm_time == "2026-05-10T18:47:22+00:00"
    call_later.assert_called_once()


def test_handle_coordinator_update_ignores_non_matching_code() -> None:
    a = _alarm(["Name"], match_codes=["3001"])
    a.coordinator.data = {
        ALARM_FIELD: "Other",
        ALARM_CODE_FIELD: "9999",
        ALARM_TIME_FIELD: "2026-05-10T18:47:22+00:00",
    }
    with patch(
        "custom_components.ezviz_hp7.binary_sensor.async_call_later"
    ) as call_later:
        a._handle_coordinator_update()
    assert a._last_trigger is None
    call_later.assert_not_called()


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


# ── Local-dispatch fallback (issue #8) ──────────────────────────────


def test_handle_local_unlock_pulses_for_matching_serial_and_action() -> None:
    """The dispatcher signal fires when a HA-originated unlock succeeds —
    that's how non-English accounts get a working ``gate_open`` / ``unlock_lock``
    sensor even though the cloud's ``sampleName`` never matches the English
    strings in ``ALARM_MAP``."""
    a = _alarm(["irrelevant"], local_actions=["unlock_gate"])
    with patch(
        "custom_components.ezviz_hp7.binary_sensor.async_call_later"
    ) as call_later:
        a._handle_local_unlock("S-1", "unlock_gate")

    assert a._last_trigger is not None
    call_later.assert_called_once()
    a.async_write_ha_state.assert_called_once()


def test_handle_local_unlock_ignores_other_serial() -> None:
    a = _alarm(["x"], local_actions=["unlock_gate"])
    with patch(
        "custom_components.ezviz_hp7.binary_sensor.async_call_later"
    ) as call_later:
        a._handle_local_unlock("OTHER-SERIAL", "unlock_gate")
    assert a._last_trigger is None
    call_later.assert_not_called()


def test_handle_local_unlock_ignores_other_action() -> None:
    a = _alarm(["x"], local_actions=["unlock_gate"])
    with patch(
        "custom_components.ezviz_hp7.binary_sensor.async_call_later"
    ) as call_later:
        a._handle_local_unlock("S-1", "unlock_door")
    assert a._last_trigger is None
    call_later.assert_not_called()


def test_handle_local_unlock_no_op_when_no_local_actions() -> None:
    """Sensors with empty ``local_actions`` (the cloud-only ones —
    doorbell_ringing, smart/intelligent detection) must never react to
    dispatcher signals."""
    a = _alarm(["x"])  # local_actions defaults to None → []
    with patch(
        "custom_components.ezviz_hp7.binary_sensor.async_call_later"
    ) as call_later:
        a._handle_local_unlock("S-1", "unlock_gate")
    assert a._last_trigger is None
    call_later.assert_not_called()


async def test_async_added_to_hass_subscribes_when_local_actions_present() -> None:
    a = _alarm(["x"], local_actions=["unlock_gate"])
    a.async_on_remove = MagicMock()
    with (
        patch(
            "custom_components.ezviz_hp7.binary_sensor.async_dispatcher_connect",
            return_value=MagicMock(),
        ) as connect,
        patch.object(
            Hp7BinaryAlarm.__bases__[0], "async_added_to_hass", new_callable=AsyncMock
        ),
    ):
        await a.async_added_to_hass()
    connect.assert_called_once()
    args, _ = connect.call_args
    assert args[0] is a.hass
    assert args[1] == SIGNAL_LOCAL_UNLOCK
    # Bound-method identity check: same function + same instance.
    assert args[2].__func__ is Hp7BinaryAlarm._handle_local_unlock
    assert args[2].__self__ is a
    a.async_on_remove.assert_called_once()


async def test_async_added_to_hass_skips_subscribe_without_local_actions() -> None:
    a = _alarm(["x"])
    a.async_on_remove = MagicMock()
    with (
        patch(
            "custom_components.ezviz_hp7.binary_sensor.async_dispatcher_connect",
        ) as connect,
        patch.object(
            Hp7BinaryAlarm.__bases__[0], "async_added_to_hass", new_callable=AsyncMock
        ),
    ):
        await a.async_added_to_hass()
    connect.assert_not_called()
    a.async_on_remove.assert_not_called()


# ── async_setup_entry ──────────────────────────────────────────────


async def test_async_setup_entry_registers_simple_and_alarm_entities() -> None:
    coord = MagicMock(data={})
    hass = MagicMock()
    hass.data = {DOMAIN: {"entry-id": {"coordinator": coord, "serial": "S"}}}
    entry = MagicMock(entry_id="entry-id")
    add: MagicMock = MagicMock()

    await async_setup_entry(hass, entry, add)

    add.assert_called_once()
    (entities,), _ = add.call_args
    assert len(entities) == len(SIMPLE_MAP) + len(ALARM_MAP)
    n_simple = sum(1 for e in entities if isinstance(e, Hp7BinarySimple))
    n_alarm = sum(1 for e in entities if isinstance(e, Hp7BinaryAlarm))
    assert n_simple == len(SIMPLE_MAP)
    assert n_alarm == len(ALARM_MAP)
