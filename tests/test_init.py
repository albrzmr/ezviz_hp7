"""Phase 2.2 — coverage for ``__init__.py``.

``async_setup_entry`` is the integration boot path.  Tests here patch
every external dependency (``Hp7Api``, ``Hp7Coordinator``,
``CpdMpegPsRelay``, ``ActivityStats``, ``async_track_time_interval``)
at module level so HA itself is not booted.  ``hass`` is a
``MagicMock`` whose ``async_add_executor_job`` runs the callable inline.

The manifest / HACS smoke checks moved to ``tests/test_manifest.py``.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from custom_components.ezviz_hp7 import (
    _install_event_prewarm,
    async_reload_entry,
    async_setup_entry,
    async_unload_entry,
)
from custom_components.ezviz_hp7.const import (
    CONF_FEATURE_CODE,
    CONF_LIVE_VIEW_MODE,
    DEFAULT_LIVE_VIEW_MODE,
    DOMAIN,
    LIVE_VIEW_HLS,
    PLATFORMS,
)

INIT_MOD = "custom_components.ezviz_hp7"


def _hass() -> MagicMock:
    """``hass`` stub with an inline ``async_add_executor_job``."""
    hass = MagicMock()

    async def _run(fn, *args, **kwargs):
        return fn(*args, **kwargs)

    hass.async_add_executor_job = _run
    hass.config_entries.async_forward_entry_setups = AsyncMock(return_value=True)
    hass.config_entries.async_unload_platforms = AsyncMock(return_value=True)
    hass.config_entries.async_reload = AsyncMock()
    hass.config_entries.async_update_entry = MagicMock()

    # ``async_setup_entry`` schedules background tasks with real coroutines
    # (``_refresh_aes``).  Close them so we don't trip the "coroutine was
    # never awaited" warning while still recording the call.
    def _consume(coro, **_kwargs):
        if hasattr(coro, "close"):
            coro.close()
        return MagicMock()

    hass.async_create_background_task = MagicMock(side_effect=_consume)
    hass.data = {}
    return hass


def _entry(
    *,
    feature_code: str | None = "deadbeef" * 4,
    options: dict[str, Any] | None = None,
) -> MagicMock:
    e = MagicMock()
    e.entry_id = "e-1"
    data: dict[str, Any] = {
        "username": "u@example.com",
        "password": "p",
        "region": "eu",
        "serial": "S-1",
        "token": {"session_id": "fake"},
    }
    if feature_code is not None:
        data[CONF_FEATURE_CODE] = feature_code
    e.data = data
    e.options = options or {}
    return e


@pytest.fixture
def patched(mocker):
    """Patch every external boot dependency in the integration module."""
    api = MagicMock()
    api.login = MagicMock(return_value=None)
    api.detect_capabilities = MagicMock(return_value=None)
    api.get_related_device = MagicMock(return_value="S-1-CAM")
    api.fetch_lan_aes_key = MagicMock(return_value=b"0" * 16)
    api.close = MagicMock()

    coord = MagicMock()
    coord.data = {
        "last_alarm_time": None,
        "alarm_name": None,
        "local_ip": "192.0.2.10",
    }
    coord.async_config_entry_first_refresh = AsyncMock()
    coord.async_add_listener = MagicMock(return_value=MagicMock())

    relay = MagicMock()
    relay.async_start = AsyncMock()
    relay.async_stop = AsyncMock()
    relay.async_prewarm = MagicMock(return_value=MagicMock())  # returns coro stand-in
    relay.url = "tcp://127.0.0.1:8554"

    stats = MagicMock()

    mocker.patch(f"{INIT_MOD}.Hp7Api", return_value=api)
    mocker.patch(f"{INIT_MOD}.Hp7Coordinator", return_value=coord)
    mocker.patch(f"{INIT_MOD}.CpdMpegPsRelay", return_value=relay)
    mocker.patch(f"{INIT_MOD}.ActivityStats", return_value=stats)
    mocker.patch(f"{INIT_MOD}.async_track_time_interval", return_value=MagicMock())

    return {"api": api, "coord": coord, "relay": relay, "stats": stats}


# ── async_setup_entry ──────────────────────────────────────────────


async def test_async_setup_entry_happy_path(patched: dict) -> None:
    hass = _hass()
    entry = _entry()

    assert await async_setup_entry(hass, entry) is True

    hass.config_entries.async_forward_entry_setups.assert_awaited_once_with(
        entry, PLATFORMS
    )
    stored = hass.data[DOMAIN][entry.entry_id]
    assert stored["api"] is patched["api"]
    assert stored["coordinator"] is patched["coord"]
    assert stored["relay"] is patched["relay"]
    assert stored["serial"] == "S-1"
    assert stored["live_view_mode"] == DEFAULT_LIVE_VIEW_MODE
    patched["api"].login.assert_called_once()
    patched["api"].detect_capabilities.assert_called_once_with("S-1")
    patched["coord"].async_config_entry_first_refresh.assert_awaited_once()
    patched["relay"].async_start.assert_awaited_once()


async def test_async_setup_entry_picks_up_hls_mode_from_options(
    patched: dict,
) -> None:
    hass = _hass()
    entry = _entry(options={CONF_LIVE_VIEW_MODE: LIVE_VIEW_HLS})
    await async_setup_entry(hass, entry)
    assert hass.data[DOMAIN][entry.entry_id]["live_view_mode"] == LIVE_VIEW_HLS


async def test_async_setup_entry_generates_feature_code_when_missing(
    patched: dict, mocker
) -> None:
    """Migration path: entries created before 0.8.3 lack a featureCode.
    Setup must mint a 32-char hex value, clear the cached token, and
    persist the change via ``async_update_entry``."""
    mocker.patch(f"{INIT_MOD}.secrets.token_hex", return_value="cafe" * 8)
    hass = _hass()
    entry = _entry(feature_code=None)

    await async_setup_entry(hass, entry)

    hass.config_entries.async_update_entry.assert_called_once()
    _args, kwargs = hass.config_entries.async_update_entry.call_args
    assert kwargs["data"][CONF_FEATURE_CODE] == "cafe" * 8
    assert "token" not in kwargs["data"]


async def test_async_setup_entry_keeps_existing_feature_code(patched: dict) -> None:
    hass = _hass()
    entry = _entry(feature_code="abcdef01" * 4)
    await async_setup_entry(hass, entry)
    hass.config_entries.async_update_entry.assert_not_called()


async def test_async_setup_entry_raises_not_ready_on_login_failure(
    patched: dict,
) -> None:
    from homeassistant.exceptions import ConfigEntryNotReady

    patched["api"].login.side_effect = RuntimeError("auth broken")
    with pytest.raises(ConfigEntryNotReady, match="Cannot connect"):
        await async_setup_entry(_hass(), _entry())


async def test_async_setup_entry_raises_not_ready_on_first_refresh_failure(
    patched: dict,
) -> None:
    from homeassistant.exceptions import ConfigEntryNotReady

    patched["coord"].async_config_entry_first_refresh.side_effect = RuntimeError(
        "cloud down"
    )
    with pytest.raises(ConfigEntryNotReady, match="Failed to fetch"):
        await async_setup_entry(_hass(), _entry())


async def test_async_setup_entry_falls_back_when_get_related_fails(
    patched: dict, caplog
) -> None:
    patched["api"].get_related_device.side_effect = RuntimeError("dns down")
    assert await async_setup_entry(_hass(), _entry()) is True
    assert any("get_related_device failed" in r.message for r in caplog.records)


async def test_async_setup_entry_continues_when_relay_fails(
    patched: dict, caplog
) -> None:
    patched["relay"].async_start.side_effect = RuntimeError("port in use")
    hass = _hass()
    entry = _entry()

    assert await async_setup_entry(hass, entry) is True
    stored = hass.data[DOMAIN][entry.entry_id]
    assert stored["relay"] is None
    assert any("relay failed to start" in r.message for r in caplog.records)


# ── async_unload_entry ─────────────────────────────────────────────


async def test_async_unload_entry_stops_relay_and_closes_api(patched: dict) -> None:
    hass = _hass()
    entry = _entry()
    await async_setup_entry(hass, entry)

    assert await async_unload_entry(hass, entry) is True

    patched["relay"].async_stop.assert_awaited_once()
    patched["api"].close.assert_called_once()
    patched["stats"].log_summary.assert_called()
    assert entry.entry_id not in hass.data.get(DOMAIN, {})


async def test_async_unload_entry_handles_missing_relay(patched: dict) -> None:
    hass = _hass()
    entry = _entry()
    hass.data[DOMAIN] = {entry.entry_id: {"api": patched["api"], "stats": None}}
    assert await async_unload_entry(hass, entry) is True
    patched["api"].close.assert_called_once()


async def test_async_unload_entry_returns_false_when_platforms_fail() -> None:
    hass = _hass()
    hass.config_entries.async_unload_platforms = AsyncMock(return_value=False)
    hass.data[DOMAIN] = {"e-1": {"api": MagicMock(), "stats": MagicMock()}}
    assert await async_unload_entry(hass, _entry()) is False
    assert "e-1" in hass.data[DOMAIN]


# ── async_reload_entry ────────────────────────────────────────────


async def test_async_reload_entry_delegates_to_hass_config_entries() -> None:
    hass = _hass()
    entry = _entry()
    await async_reload_entry(hass, entry)
    hass.config_entries.async_reload.assert_awaited_once_with(entry.entry_id)


# ── _install_event_prewarm ────────────────────────────────────────


def _prewarm_setup(
    patched: dict, initial_alarm_time: str | None = None
) -> tuple[MagicMock, Any, MagicMock]:
    """Wire ``_install_event_prewarm`` and return (hass, listener, relay).

    Captures the ``_on_update`` callback registered via
    ``coordinator.async_add_listener`` so tests can fire it directly.
    """
    hass = _hass()
    entry = _entry()
    coord = patched["coord"]
    relay = patched["relay"]
    coord.data = {"last_alarm_time": initial_alarm_time, "alarm_name": None}

    _install_event_prewarm(hass, entry, coord, relay)
    coord.async_add_listener.assert_called_once()
    on_update = coord.async_add_listener.call_args[0][0]
    return hass, on_update, relay


def test_install_event_prewarm_triggers_on_new_alarm_time(patched: dict) -> None:
    hass, on_update, relay = _prewarm_setup(patched, initial_alarm_time=None)
    patched["coord"].data = {
        "last_alarm_time": "2026-05-11T10:00:00+00:00",
        "alarm_name": "Smart Detection Alarm",
    }
    on_update()
    hass.async_create_background_task.assert_called_once()
    relay.async_prewarm.assert_called_once()


def test_install_event_prewarm_dedupes_same_alarm_time(patched: dict) -> None:
    """A coordinator tick with the same ``last_alarm_time`` we already
    pre-warmed for must NOT schedule a second pre-warm."""
    hass, on_update, _relay = _prewarm_setup(
        patched, initial_alarm_time="2026-05-11T10:00:00+00:00"
    )
    patched["coord"].data = {
        "last_alarm_time": "2026-05-11T10:00:00+00:00",  # unchanged
        "alarm_name": "Smart Detection Alarm",
    }
    on_update()
    hass.async_create_background_task.assert_not_called()


def test_install_event_prewarm_ignores_none_alarm_time(patched: dict) -> None:
    hass, on_update, _relay = _prewarm_setup(
        patched, initial_alarm_time="2026-05-11T10:00:00+00:00"
    )
    patched["coord"].data = {"last_alarm_time": None, "alarm_name": None}
    on_update()
    hass.async_create_background_task.assert_not_called()
