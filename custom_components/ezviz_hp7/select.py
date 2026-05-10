"""EZVIZ HP7 select entities — exposes the live-view mode on the device card.

Lets the user toggle between MJPEG (low latency) and HLS (high quality)
straight from the device page, without going through the integration's
Options flow.  Picking a different option updates the config entry's
options and triggers a reload, which propagates the change to the
camera entity.
"""
from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from homeassistant.components.select import SelectEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity import DeviceInfo, EntityCategory
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import (
    DOMAIN,
    CONF_LIVE_VIEW_MODE,
    DEFAULT_LIVE_VIEW_MODE,
    LIVE_VIEW_MJPEG,
    LIVE_VIEW_HLS,
)

if TYPE_CHECKING:
    from .api import Hp7Api

_LOGGER = logging.getLogger(__name__)


_LIVE_VIEW_OPTIONS: list[str] = [LIVE_VIEW_MJPEG, LIVE_VIEW_HLS]


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up EZVIZ HP7 select entities."""
    data: dict[str, Any] = hass.data[DOMAIN][entry.entry_id]
    api: Hp7Api = data["api"]
    serial: str = data["serial"]
    async_add_entities([Hp7LiveViewModeSelect(hass, entry, api, serial)])


class Hp7LiveViewModeSelect(SelectEntity):
    """Select entity that mirrors the integration's ``live_view_mode`` option."""

    _attr_has_entity_name = True
    _attr_translation_key = "live_view_mode"
    _attr_entity_category = EntityCategory.CONFIG
    _attr_options = _LIVE_VIEW_OPTIONS
    _attr_icon = "mdi:video-switch"

    def __init__(
        self,
        hass: HomeAssistant,
        entry: ConfigEntry,
        api: "Hp7Api",
        serial: str,
    ) -> None:
        self.hass = hass
        self._entry = entry
        self._api = api
        self._serial = serial
        self._attr_unique_id = f"{DOMAIN}_{serial}_live_view_mode"

    @property
    def device_info(self) -> DeviceInfo:
        model = getattr(self._api, "model", "HP7")
        return DeviceInfo(
            identifiers={(DOMAIN, self._serial)},
            name=f"EZVIZ {model} ({self._serial})",
            manufacturer="EZVIZ",
            model=model,
        )

    @property
    def current_option(self) -> str:
        return self._entry.options.get(CONF_LIVE_VIEW_MODE, DEFAULT_LIVE_VIEW_MODE)

    async def async_select_option(self, option: str) -> None:
        if option not in _LIVE_VIEW_OPTIONS:
            raise ValueError(f"unknown live-view mode: {option!r}")
        if option == self.current_option:
            return
        new_options = {**self._entry.options, CONF_LIVE_VIEW_MODE: option}
        self.hass.config_entries.async_update_entry(self._entry, options=new_options)
        # The update listener in __init__.py will reload the entry so the
        # change propagates to the camera entity (STREAM feature flip,
        # MJPEG vs HLS path).
        _LOGGER.info(
            "EZVIZ HP7: live view mode changed to %s — reloading entry", option,
        )
