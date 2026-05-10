"""Data update coordinator for EZVIZ HP7."""
from __future__ import annotations

import logging
from datetime import timedelta
from typing import Any, TYPE_CHECKING

from homeassistant.helpers.update_coordinator import (
    DataUpdateCoordinator,
    UpdateFailed,
)

from .const import UPDATE_INTERVAL_SEC

if TYPE_CHECKING:
    from homeassistant.config_entries import ConfigEntry
    from homeassistant.core import HomeAssistant
    from .api import Hp7Api
    from .stats import ActivityStats

_LOGGER = logging.getLogger(__name__)


class Hp7Coordinator(DataUpdateCoordinator):
    """Periodic device-status poller for the EZVIZ HP7 / CP7."""

    def __init__(
        self,
        hass: "HomeAssistant",
        entry: "ConfigEntry",
        api: "Hp7Api",
        serial: str,
        stats: "ActivityStats | None" = None,
    ) -> None:
        """Initialize the coordinator.

        ``config_entry`` is required by HA 2024.12+ — without it
        ``async_config_entry_first_refresh`` raises ``ConfigEntryError``
        ("Detected code that uses async_config_entry_first_refresh,
        which is only supported for coordinators with a config entry").
        """
        super().__init__(
            hass,
            _LOGGER,
            config_entry=entry,
            name="EZVIZ HP7",
            update_interval=timedelta(seconds=UPDATE_INTERVAL_SEC),
        )
        self.api = api
        self.serial = serial
        self._stats = stats

    async def _async_update_data(self) -> dict[str, Any]:
        """Fetch latest device status from the API on every tick.

        Wraps any failure in ``UpdateFailed`` so HA can apply its usual
        backoff + ``unavailable`` semantics rather than us silently
        returning an empty dict and leaving entities with stale values.
        """
        if self._stats is not None:
            self._stats.cloud_polls += 1
        try:
            return await self.hass.async_add_executor_job(
                self.api.get_status, self.serial,
            )
        except Exception as exc:  # noqa: BLE001 — pyezvizapi raises various types
            raise UpdateFailed(f"EZVIZ HP7 poll failed: {exc}") from exc
