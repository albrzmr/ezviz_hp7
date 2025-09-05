from __future__ import annotations
import logging
from datetime import timedelta
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator
from .const import UPDATE_INTERVAL_SEC

_LOGGER = logging.getLogger(__name__)

class Hp7Coordinator(DataUpdateCoordinator):
    def __init__(self, hass, api, serial):
        super().__init__(
            hass,
            _LOGGER,
            name=f"EZVIZ {getattr(api, 'model', 'HP7')}",
            update_interval=timedelta(seconds=UPDATE_INTERVAL_SEC),
        )
        self.api = api
        self.serial = serial

    async def _async_update_data(self):
        return await self.hass.async_add_executor_job(self.api.get_status, self.serial)
