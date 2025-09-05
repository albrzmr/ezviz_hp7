
from __future__ import annotations
import logging
from homeassistant.components.button import ButtonEntity
from homeassistant.helpers.entity import DeviceInfo
from .const import DOMAIN

_LOGGER = logging.getLogger(__name__)

async def async_setup_entry(hass, entry, async_add_entities):
    data = hass.data[DOMAIN][entry.entry_id]
    api = data["api"]
    serial = data["serial"]

    entities = []
    if getattr(api, "supports_gate", False):
        entities.append(EzvizHp7Button(api, serial, "unlock_gate", "Sblocca Cancello"))
    if getattr(api, "supports_door", False):
        entities.append(EzvizHp7Button(api, serial, "unlock_door", "Sblocca Porta"))
    async_add_entities(entities)

class EzvizHp7Button(ButtonEntity):
    def __init__(self, api, serial, action, name):
        self._api = api
        self._serial = serial
        self._action = action
        self._attr_name = name
        self._attr_unique_id = f"{DOMAIN}_{serial}_{action}"

    @property
    def device_info(self) -> DeviceInfo:
        model = getattr(self._api, "model", "HP7")
        return DeviceInfo(
            identifiers={(DOMAIN, self._serial)},
            name=f"EZVIZ {model} ({self._serial})",
            manufacturer="EZVIZ",
            model=model,
        )

    async def async_press(self) -> None:
        model = getattr(self._api, "model", "HP7")
        _LOGGER.warning("EZVIZ %s: premuto bottone '%s' (%s)", model, self._action, self._serial)
        if self._action == "unlock_gate":
            ok = await self.hass.async_add_executor_job(self._api.unlock_gate, self._serial)
            _LOGGER.log(
                logging.INFO if ok else logging.ERROR,
                "EZVIZ %s: 'Sblocca Cancello' %s.",
                model,
                "OK" if ok else "FALLITO",
            )
        elif self._action == "unlock_door":
            ok = await self.hass.async_add_executor_job(self._api.unlock_door, self._serial)
            _LOGGER.log(
                logging.INFO if ok else logging.ERROR,
                "EZVIZ %s: 'Sblocca Porta' %s.",
                model,
                "OK" if ok else "FALLITO",
            )
