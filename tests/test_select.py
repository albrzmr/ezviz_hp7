"""Phase 2.7 — coverage for ``select.py``.

A single ``Hp7LiveViewModeSelect`` entity that mirrors the
``live_view_mode`` config-entry option (MJPEG / HLS).  Picking a new
option must:

1. Refuse anything outside ``_LIVE_VIEW_OPTIONS``.
2. Short-circuit when the chosen option already matches.
3. Otherwise call ``hass.config_entries.async_update_entry`` with the
   updated options dict (the update listener in ``__init__`` reloads
   the entry).
"""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock

import pytest

from custom_components.ezviz_hp7.const import (
    CONF_LIVE_VIEW_MODE,
    DEFAULT_LIVE_VIEW_MODE,
    DOMAIN,
    LIVE_VIEW_HLS,
    LIVE_VIEW_MJPEG,
)
from custom_components.ezviz_hp7.select import (
    Hp7LiveViewModeSelect,
    async_setup_entry,
)


def _entry(options: dict[str, Any] | None = None) -> MagicMock:
    e = MagicMock()
    e.options = options or {}
    return e


def _select(
    entry: MagicMock | None = None, *, api_model: str = "HP7"
) -> Hp7LiveViewModeSelect:
    hass = MagicMock()
    api = MagicMock(model=api_model)
    return Hp7LiveViewModeSelect(hass, entry or _entry(), api, "S-1")


# ── async_setup_entry ──────────────────────────────────────────────


async def test_async_setup_entry_registers_one_select_entity() -> None:
    hass = MagicMock()
    api = MagicMock()
    hass.data = {DOMAIN: {"e": {"api": api, "serial": "S"}}}
    entry = _entry()
    entry.entry_id = "e"
    add: MagicMock = MagicMock()

    await async_setup_entry(hass, entry, add)

    add.assert_called_once()
    (entities,), _ = add.call_args
    assert len(entities) == 1
    assert isinstance(entities[0], Hp7LiveViewModeSelect)


# ── Entity properties ─────────────────────────────────────────────


def test_select_unique_id_and_options() -> None:
    s = _select()
    assert s.unique_id == f"{DOMAIN}_S-1_live_view_mode"
    assert set(s.options) == {LIVE_VIEW_MJPEG, LIVE_VIEW_HLS}


def test_select_current_option_defaults_when_unset() -> None:
    s = _select(_entry(options={}))
    assert s.current_option == DEFAULT_LIVE_VIEW_MODE


def test_select_current_option_reads_from_entry_options() -> None:
    s = _select(_entry(options={CONF_LIVE_VIEW_MODE: LIVE_VIEW_HLS}))
    assert s.current_option == LIVE_VIEW_HLS


def test_select_device_info_uses_api_model() -> None:
    s = _select(api_model="CP7")
    info = s.device_info
    assert info["model"] == "CP7"
    assert (DOMAIN, "S-1") in info["identifiers"]


# ── async_select_option ───────────────────────────────────────────


async def test_select_option_rejects_unknown_value() -> None:
    s = _select()
    with pytest.raises(ValueError, match="unknown live-view mode"):
        await s.async_select_option("bogus")
    s.hass.config_entries.async_update_entry.assert_not_called()


async def test_select_option_no_op_when_already_selected() -> None:
    s = _select(_entry(options={CONF_LIVE_VIEW_MODE: LIVE_VIEW_MJPEG}))
    await s.async_select_option(LIVE_VIEW_MJPEG)
    s.hass.config_entries.async_update_entry.assert_not_called()


async def test_select_option_updates_entry_options_when_changed() -> None:
    entry = _entry(options={CONF_LIVE_VIEW_MODE: LIVE_VIEW_MJPEG, "keep": True})
    s = _select(entry)
    await s.async_select_option(LIVE_VIEW_HLS)

    s.hass.config_entries.async_update_entry.assert_called_once()
    args, kwargs = s.hass.config_entries.async_update_entry.call_args
    assert args[0] is entry
    assert kwargs["options"] == {
        CONF_LIVE_VIEW_MODE: LIVE_VIEW_HLS,
        "keep": True,  # other options preserved
    }


async def test_select_option_logs_change(caplog) -> None:
    s = _select(_entry(options={CONF_LIVE_VIEW_MODE: LIVE_VIEW_MJPEG}))
    await s.async_select_option(LIVE_VIEW_HLS)
    assert any("live view mode changed to hls" in r.message for r in caplog.records)
