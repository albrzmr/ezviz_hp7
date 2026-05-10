"""Tests for the EZVIZ HP7 config + options flow.

Phase 2.1 of the testing plan — covers every step of
``async_step_user`` / ``async_step_pick_serial`` /
``async_step_enter_serial`` plus the options flow, and locks the
per-install ``feature_code`` invariant: every entry created must
carry a 32-char hex value and the same value must be passed to
``Hp7Api`` (which in turn feeds the EUCAS ``<Sign>`` via the JWT).
"""

from __future__ import annotations

import re
from typing import Any
from unittest.mock import MagicMock, patch

from homeassistant.config_entries import SOURCE_USER
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType

from custom_components.ezviz_hp7.const import (
    CONF_FEATURE_CODE,
    CONF_LIVE_VIEW_MODE,
    DOMAIN,
    LIVE_VIEW_HLS,
    LIVE_VIEW_MJPEG,
)

_FAKE_TOKEN = {"session_id": "fake-jwt", "username": "u"}


def _mock_api(*, devices: dict[str, Any] | None = None) -> MagicMock:
    """Build a MagicMock that quacks like ``Hp7Api``."""
    api = MagicMock()
    api.login.return_value = True
    api.token = _FAKE_TOKEN
    api.list_devices.return_value = devices or {}
    return api


# ── async_step_user ────────────────────────────────────────────────


async def test_user_step_shows_form_first(hass: HomeAssistant) -> None:
    """``async_step_user`` with no input must show the form."""
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_USER}
    )
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "user"
    assert result.get("errors") in (None, {})


async def test_user_step_happy_path_advances_to_pick_serial(
    hass: HomeAssistant,
) -> None:
    api = _mock_api(devices={"BE0123456-CAM1": {"device_name": "Front Door"}})
    with patch(
        "custom_components.ezviz_hp7.config_flow.Hp7Api", return_value=api
    ) as cls:
        result = await hass.config_entries.flow.async_init(
            DOMAIN,
            context={"source": SOURCE_USER},
            data={"username": "u@x", "password": "p", "region": "eu"},
        )
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "pick_serial"
    # Hp7Api must have been constructed with a per-install featureCode.
    fc = cls.call_args.kwargs["feature_code"]
    assert isinstance(fc, str)
    assert re.fullmatch(r"[0-9a-f]{32}", fc)


async def test_user_step_no_devices_falls_through_to_enter_serial(
    hass: HomeAssistant,
) -> None:
    api = _mock_api(devices={})
    with patch("custom_components.ezviz_hp7.config_flow.Hp7Api", return_value=api):
        result = await hass.config_entries.flow.async_init(
            DOMAIN,
            context={"source": SOURCE_USER},
            data={"username": "u@x", "password": "p", "region": "eu"},
        )
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "enter_serial"


async def test_user_step_auth_error_reshows_form_with_auth_base(
    hass: HomeAssistant,
) -> None:
    """``ValueError`` from ``api.login`` → friendly ``auth`` error code."""
    api = MagicMock()
    api.login.side_effect = ValueError("bad creds")
    with patch("custom_components.ezviz_hp7.config_flow.Hp7Api", return_value=api):
        result = await hass.config_entries.flow.async_init(
            DOMAIN,
            context={"source": SOURCE_USER},
            data={"username": "u@x", "password": "p", "region": "eu"},
        )
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "user"
    assert result["errors"] == {"base": "auth"}


async def test_user_step_generic_error_reshows_form_with_cannot_connect(
    hass: HomeAssistant,
) -> None:
    """Any non-``ValueError`` raised → ``cannot_connect``."""
    api = MagicMock()
    api.login.side_effect = RuntimeError("dns timeout")
    with patch("custom_components.ezviz_hp7.config_flow.Hp7Api", return_value=api):
        result = await hass.config_entries.flow.async_init(
            DOMAIN,
            context={"source": SOURCE_USER},
            data={"username": "u@x", "password": "p", "region": "eu"},
        )
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "user"
    assert result["errors"] == {"base": "cannot_connect"}


# ── async_step_pick_serial ─────────────────────────────────────────


async def test_pick_serial_creates_entry_with_feature_code(
    hass: HomeAssistant,
) -> None:
    """A successful pick must persist the auto-generated featureCode."""
    api = _mock_api(devices={"BE0123456-CAM1": {"device_name": "Front Door"}})
    with patch("custom_components.ezviz_hp7.config_flow.Hp7Api", return_value=api):
        result = await hass.config_entries.flow.async_init(
            DOMAIN,
            context={"source": SOURCE_USER},
            data={"username": "u@x", "password": "p", "region": "eu"},
        )
        assert result["step_id"] == "pick_serial"
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {"serial": "BE0123456-CAM1"}
        )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    data = result["data"]
    assert data["serial"] == "BE0123456-CAM1"
    assert data["username"] == "u@x"
    fc = data[CONF_FEATURE_CODE]
    assert re.fullmatch(r"[0-9a-f]{32}", fc), f"feature_code shape: {fc!r}"
    # And the same value was the one passed to ``Hp7Api`` during login.
    assert data["token"] == _FAKE_TOKEN


async def test_pick_serial_aborts_if_already_configured(
    hass: HomeAssistant,
) -> None:
    """A second flow for the same device must abort, not duplicate."""
    api = _mock_api(devices={"BE0123456-CAM1": {"device_name": "Front Door"}})
    # First entry succeeds.
    with patch("custom_components.ezviz_hp7.config_flow.Hp7Api", return_value=api):
        flow1 = await hass.config_entries.flow.async_init(
            DOMAIN,
            context={"source": SOURCE_USER},
            data={"username": "u@x", "password": "p", "region": "eu"},
        )
        first = await hass.config_entries.flow.async_configure(
            flow1["flow_id"], {"serial": "BE0123456-CAM1"}
        )
        assert first["type"] is FlowResultType.CREATE_ENTRY
        # Second flow targeting the same serial must abort.
        flow2 = await hass.config_entries.flow.async_init(
            DOMAIN,
            context={"source": SOURCE_USER},
            data={"username": "u@x", "password": "p", "region": "eu"},
        )
        result = await hass.config_entries.flow.async_configure(
            flow2["flow_id"], {"serial": "BE0123456-CAM1"}
        )
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "already_configured"


# ── async_step_enter_serial ────────────────────────────────────────


async def test_enter_serial_manual_creates_entry(hass: HomeAssistant) -> None:
    """No devices listed → manual entry creates a valid config entry."""
    api = _mock_api(devices={})
    with patch("custom_components.ezviz_hp7.config_flow.Hp7Api", return_value=api):
        first = await hass.config_entries.flow.async_init(
            DOMAIN,
            context={"source": SOURCE_USER},
            data={"username": "u@x", "password": "p", "region": "eu"},
        )
        assert first["step_id"] == "enter_serial"
        result = await hass.config_entries.flow.async_configure(
            first["flow_id"], {"serial": "  BE9999999-CAM7  "}
        )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    # Serial was stripped of surrounding whitespace.
    assert result["data"]["serial"] == "BE9999999-CAM7"
    # featureCode invariant still holds.
    assert re.fullmatch(r"[0-9a-f]{32}", result["data"][CONF_FEATURE_CODE])


# ── OptionsFlowHandler ────────────────────────────────────────────


async def test_options_flow_defaults_to_mjpeg(hass: HomeAssistant) -> None:
    """First visit (no prior selection) must default to MJPEG."""
    from pytest_homeassistant_custom_component.common import MockConfigEntry

    entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id="EXTRA",
        data={"username": "u", "password": "p", "region": "eu", "serial": "EXTRA"},
        options={},  # no live_view_mode set
    )
    entry.add_to_hass(hass)
    result = await hass.config_entries.options.async_init(entry.entry_id)
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "init"
    schema = result["data_schema"].schema
    field = next(k for k in schema if getattr(k, "schema", k) == CONF_LIVE_VIEW_MODE)
    assert field.default() == LIVE_VIEW_MJPEG


async def test_options_flow_switches_to_hls(
    hass: HomeAssistant, mock_config_entry
) -> None:
    mock_config_entry.add_to_hass(hass)
    result = await hass.config_entries.options.async_init(mock_config_entry.entry_id)
    # Submit a new selection.
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {CONF_LIVE_VIEW_MODE: LIVE_VIEW_HLS}
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["data"] == {CONF_LIVE_VIEW_MODE: LIVE_VIEW_HLS}


async def test_options_flow_both_modes_in_schema(
    hass: HomeAssistant, mock_config_entry
) -> None:
    """Both LIVE_VIEW_MJPEG and LIVE_VIEW_HLS must be selectable."""
    mock_config_entry.add_to_hass(hass)
    result = await hass.config_entries.options.async_init(mock_config_entry.entry_id)
    schema = result["data_schema"].schema
    field = next(k for k in schema if getattr(k, "schema", k) == CONF_LIVE_VIEW_MODE)
    validator = schema[field]
    # ``vol.In`` exposes its container as ``.container``.
    selectable = set(validator.container)
    assert {LIVE_VIEW_MJPEG, LIVE_VIEW_HLS} == selectable


# ── feature_code → Hp7Api wiring ──────────────────────────────────


async def test_feature_code_round_trips_into_hp7api(hass: HomeAssistant) -> None:
    """The exact value persisted in entry.data must equal the one
    passed to ``Hp7Api`` — that's what guarantees the JWT's ``s``
    claim and the EUCAS ``<Sign>`` end up matching at runtime.
    """
    api = _mock_api(devices={"BE0123456-CAM1": {"device_name": "Front"}})
    with patch(
        "custom_components.ezviz_hp7.config_flow.Hp7Api", return_value=api
    ) as cls:
        first = await hass.config_entries.flow.async_init(
            DOMAIN,
            context={"source": SOURCE_USER},
            data={"username": "u@x", "password": "p", "region": "eu"},
        )
        result = await hass.config_entries.flow.async_configure(
            first["flow_id"], {"serial": "BE0123456-CAM1"}
        )
    persisted_fc = result["data"][CONF_FEATURE_CODE]
    constructed_fc = cls.call_args.kwargs["feature_code"]
    assert persisted_fc == constructed_fc
