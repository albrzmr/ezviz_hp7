"""Tests for ``custom_components.ezviz_hp7.helpers``."""

from __future__ import annotations

import sys
import types
from typing import Any

import pytest


@pytest.fixture
def helpers() -> Any:
    """Import the helpers module with HA's ``DeviceInfo`` stubbed.

    ``helpers.py`` does ``from homeassistant.helpers.entity import
    DeviceInfo`` which is unavailable in pure unit-test environments.
    We stub it with ``dict`` so the helper still returns a structured
    object we can assert on.
    """
    sys.modules.setdefault(
        "homeassistant.helpers.entity",
        types.SimpleNamespace(DeviceInfo=dict),
    )
    from custom_components.ezviz_hp7 import helpers as mod

    return mod


def test_default_model_is_HP7(helpers: Any) -> None:
    info = helpers.get_device_info("BE0000000")
    assert info["model"] == "HP7"
    assert info["manufacturer"] == "EZVIZ"
    assert info["name"] == "EZVIZ HP7 (BE0000000)"


def test_dynamic_model_from_api(helpers: Any) -> None:
    api = types.SimpleNamespace(model="CP7")
    info = helpers.get_device_info("ABC123-DEF456", api=api)
    assert info["model"] == "CP7"
    assert info["name"] == "EZVIZ CP7 (ABC123-DEF456)"


def test_api_with_falsey_model_falls_back_to_default(helpers: Any) -> None:
    api = types.SimpleNamespace(model=None)
    info = helpers.get_device_info("X", api=api)
    assert info["model"] == "HP7"
    api2 = types.SimpleNamespace(model="")
    info2 = helpers.get_device_info("Y", api=api2)
    assert info2["model"] == "HP7"


def test_identifiers_are_consistent_for_same_serial(helpers: Any) -> None:
    """All entities for the same doorbell must share identifiers so HA
    groups them under one device card — this is the whole point of
    centralising the helper."""
    a = helpers.get_device_info("S1")
    b = helpers.get_device_info(
        "S1",
        api=types.SimpleNamespace(model="CP7"),
    )
    assert a["identifiers"] == b["identifiers"]
