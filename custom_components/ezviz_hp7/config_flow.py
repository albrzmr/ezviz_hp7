from __future__ import annotations
import voluptuous as vol
from homeassistant import config_entries
from .const import DOMAIN, CONF_REGION, CONF_SERIAL
from .api import Hp7Api

DATA_SCHEMA = vol.Schema({
    vol.Required("username"): str,
    vol.Required("password"): str,
    vol.Required(CONF_REGION, default="eu"): vol.In(["eu", "us", "cn", "as"]),
})

SERIAL_SCHEMA = vol.Schema({
    vol.Required(CONF_SERIAL): str,
})

class ConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    VERSION = 1

    def __init__(self) -> None:
        self._cached_creds: dict | None = None
        self._device_options: dict[str, str] | None = None  # serial -> label
        self._api: Hp7Api | None = None

    async def async_step_user(self, user_input=None):
        # Step 1: credenziali + regione
        if user_input is None:
            return self.async_show_form(step_id="user", data_schema=DATA_SCHEMA)

        self._api = Hp7Api(user_input["username"], user_input["password"], user_input[CONF_REGION])

        # Login e tentativo discovery
        try:
            ok = await self.hass.async_add_executor_job(self._api.login)
            if not ok:
                raise RuntimeError("login_failed")

            devices = {}
            if hasattr(self._api, "list_devices"):
                devices = await self.hass.async_add_executor_job(self._api.list_devices)
        except Exception:
            # Mostra errore credenziali/connessione
            return self.async_show_form(
                step_id="user",
                data_schema=DATA_SCHEMA,
                errors={"base": "auth"},
            )

        # estrazione delle opzioni dopo list_devices()
        options: dict[str, str] = {}
        for serial, info in devices.items():
            label = f"{(info.get('name') or info.get('device_name') or 'Device')}".strip()
            options[serial] = f"{label} ({serial})"
        

        self._cached_creds = user_input

        if options:
            # Vai alla scelta del serial dall’elenco
            self._device_options = options
            return await self.async_step_pick_serial()

        # Fallback: chiedi seriale manuale
        return await self.async_step_enter_serial()

    async def async_step_pick_serial(self, user_input=None):
        assert self._device_options is not None, "Device list not prepared"

        schema = vol.Schema({
            vol.Required(CONF_SERIAL): vol.In(list(self._device_options.keys()))
        })

        if user_input is None:
            return self.async_show_form(step_id="pick_serial", data_schema=schema)

        serial = user_input[CONF_SERIAL]

        await self.async_set_unique_id(serial)
        self._abort_if_unique_id_configured()

        if self._api:
            await self.hass.async_add_executor_job(self._api.detect_capabilities, serial)
            model = self._api.model
        else:
            model = "HP7"

        data = {**(self._cached_creds or {}), CONF_SERIAL: serial}
        title = f"EZVIZ {model} ({serial})"
        return self.async_create_entry(title=title, data=data)

    async def async_step_enter_serial(self, user_input=None):
        # Fallback manuale quando la discovery è vuota o non disponibile
        if user_input is None:
            return self.async_show_form(step_id="enter_serial", data_schema=SERIAL_SCHEMA)

        serial = user_input[CONF_SERIAL]

        await self.async_set_unique_id(serial)
        self._abort_if_unique_id_configured()

        if self._api:
            await self.hass.async_add_executor_job(self._api.detect_capabilities, serial)
            model = self._api.model
        else:
            model = "HP7"

        data = {**(self._cached_creds or {}), CONF_SERIAL: serial}
        title = f"EZVIZ {model} ({serial})"
        return self.async_create_entry(title=title, data=data)
