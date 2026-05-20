"""Phase 2.6 — coverage for ``button.py``.

Two entities (one per ``supports_gate`` / ``supports_door`` capability)
and an ``async_press`` that routes to ``api.unlock_gate`` /
``api.unlock_door`` via ``async_add_executor_job``.  Tested with a
mocked ``api`` and a ``hass`` stub whose ``async_add_executor_job``
just awaits the function directly.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from custom_components.ezviz_hp7.button import EzvizHp7Button, async_setup_entry
from custom_components.ezviz_hp7.const import DOMAIN, SIGNAL_LOCAL_UNLOCK


def _hass_with_executor() -> MagicMock:
    """Return a ``hass`` stub whose ``async_add_executor_job`` runs the
    callable inline and returns its result as an awaitable."""
    hass = MagicMock()

    async def _run(fn, *args, **kwargs):
        return fn(*args, **kwargs)

    hass.async_add_executor_job = _run
    return hass


# ── async_setup_entry ──────────────────────────────────────────────


async def test_async_setup_entry_registers_both_when_both_capabilities() -> None:
    api = MagicMock(supports_gate=True, supports_door=True)
    hass = _hass_with_executor()
    hass.data = {DOMAIN: {"e": {"api": api, "serial": "S"}}}
    entry = MagicMock(entry_id="e")
    add: MagicMock = MagicMock()

    await async_setup_entry(hass, entry, add)

    add.assert_called_once()
    (entities,), _ = add.call_args
    actions = sorted(e._action for e in entities)
    assert actions == ["unlock_door", "unlock_gate"]


async def test_async_setup_entry_registers_none_when_neither_supported() -> None:
    api = MagicMock(supports_gate=False, supports_door=False)
    hass = _hass_with_executor()
    hass.data = {DOMAIN: {"e": {"api": api, "serial": "S"}}}
    entry = MagicMock(entry_id="e")
    add: MagicMock = MagicMock()

    await async_setup_entry(hass, entry, add)
    (entities,), _ = add.call_args
    assert entities == []


async def test_async_setup_entry_treats_missing_attrs_as_unsupported() -> None:
    """``getattr(api, "supports_*", False)`` defaults to False — the
    capability detection in ``api.detect_capabilities`` may not have
    populated these on every code path."""
    api = MagicMock(spec=[])  # no attributes at all
    hass = _hass_with_executor()
    hass.data = {DOMAIN: {"e": {"api": api, "serial": "S"}}}
    entry = MagicMock(entry_id="e")
    add: MagicMock = MagicMock()

    await async_setup_entry(hass, entry, add)
    (entities,), _ = add.call_args
    assert entities == []


# ── EzvizHp7Button ────────────────────────────────────────────────


def test_button_unique_id_combines_domain_serial_action() -> None:
    api = MagicMock(model="HP7")
    btn = EzvizHp7Button(_hass_with_executor(), api, "S-1", "unlock_door")
    assert btn.unique_id == f"{DOMAIN}_S-1_unlock_door"
    assert btn._attr_translation_key == "unlock_door"


def test_button_device_info_uses_api_model() -> None:
    api = MagicMock(model="CP7")
    btn = EzvizHp7Button(_hass_with_executor(), api, "S-2", "unlock_gate")
    info = btn.device_info
    assert info["model"] == "CP7"
    assert (DOMAIN, "S-2") in info["identifiers"]


async def test_async_press_unlock_door_calls_api(caplog) -> None:
    api = MagicMock()
    api.unlock_door.return_value = True
    btn = EzvizHp7Button(_hass_with_executor(), api, "S-1", "unlock_door")

    await btn.async_press()

    api.unlock_door.assert_called_once_with("S-1")
    assert any("Unlock Door successful" in r.message for r in caplog.records)


async def test_async_press_unlock_gate_calls_api() -> None:
    api = MagicMock()
    api.unlock_gate.return_value = True
    btn = EzvizHp7Button(_hass_with_executor(), api, "S-1", "unlock_gate")

    await btn.async_press()
    api.unlock_gate.assert_called_once_with("S-1")


async def test_async_press_logs_error_on_failure(caplog) -> None:
    """The API call returning False must not raise — just log an error."""
    api = MagicMock()
    api.unlock_door.return_value = False
    btn = EzvizHp7Button(_hass_with_executor(), api, "S-1", "unlock_door")

    await btn.async_press()  # must not raise
    assert any(
        "Unlock Door failed" in r.message and r.levelname == "ERROR"
        for r in caplog.records
    )


async def test_async_press_with_unknown_action_is_noop() -> None:
    """Defensive: an unexpected ``_action`` value must short-circuit
    instead of crashing the button platform."""
    api = MagicMock()
    btn = EzvizHp7Button(_hass_with_executor(), api, "S-1", "do_something_weird")

    await btn.async_press()
    api.unlock_door.assert_not_called()
    api.unlock_gate.assert_not_called()


@pytest.mark.parametrize("action", ["unlock_door", "unlock_gate"])
async def test_async_press_propagates_capability_serial(action: str) -> None:
    """The serial passed to the API method is always the one held by
    the entity, regardless of which capability is exercised."""
    api = MagicMock()
    getattr(api, action).return_value = True
    btn = EzvizHp7Button(_hass_with_executor(), api, "SERIAL-XYZ", action)

    await btn.async_press()
    getattr(api, action).assert_called_once_with("SERIAL-XYZ")


# ── Dispatcher signal on successful unlock (issue #8) ─────────────


@pytest.mark.parametrize("action", ["unlock_door", "unlock_gate"])
async def test_successful_unlock_fires_local_signal(action: str) -> None:
    """On success, the button must emit ``SIGNAL_LOCAL_UNLOCK`` with
    ``(serial, action)``.  The matching binary sensor uses this to
    pulse without consulting the localised cloud feed (issue #8)."""
    api = MagicMock()
    getattr(api, action).return_value = True
    hass = _hass_with_executor()
    btn = EzvizHp7Button(hass, api, "SERIAL-LOCAL", action)

    with patch("custom_components.ezviz_hp7.button.async_dispatcher_send") as dispatch:
        await btn.async_press()

    dispatch.assert_called_once_with(hass, SIGNAL_LOCAL_UNLOCK, "SERIAL-LOCAL", action)


async def test_failed_unlock_does_not_fire_local_signal() -> None:
    """If the API returns False, no fake pulse — the dispatcher must
    only fire on confirmed success."""
    api = MagicMock()
    api.unlock_door.return_value = False
    hass = _hass_with_executor()
    btn = EzvizHp7Button(hass, api, "S-1", "unlock_door")

    with patch("custom_components.ezviz_hp7.button.async_dispatcher_send") as dispatch:
        await btn.async_press()
    dispatch.assert_not_called()
