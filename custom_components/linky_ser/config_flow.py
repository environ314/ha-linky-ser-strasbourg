"""Config flow Linky SER : email + mot de passe + tarif."""
from __future__ import annotations

import logging
from typing import Any

import aiohttp
import voluptuous as vol

from homeassistant import config_entries
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResult
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.util import dt as dt_util

from .api import LinkySerClient, LinkySerClientError
from .const import (CONF_EMAIL, CONF_INITIAL_HISTORY, CONF_PASSWORD,
                   CONF_PRICE, DEFAULT_PRICE, DOMAIN)

_LOGGER = logging.getLogger(__name__)

STEP_USER = vol.Schema({
    vol.Required(CONF_EMAIL): str,
    vol.Required(CONF_PASSWORD): str,
    vol.Required(CONF_PRICE, default=DEFAULT_PRICE): vol.Coerce(float),
    vol.Required(CONF_INITIAL_HISTORY, default=365): vol.All(
        vol.Coerce(int), vol.Range(min=7, max=1095)),
})


async def _test_login(hass: HomeAssistant, email: str,
                      password: str) -> tuple[str | None, str | None]:
    """Vérifie le login : retourne (erreur éventuelle, id PASC)."""
    session = async_get_clientsession(hass)
    client = LinkySerClient(session, email, password)
    try:
        pasc = await client.login()
        return None, str(pasc)
    except (LinkySerClientError, aiohttp.ClientError) as err:
        return str(err), None


class LinkySerConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Configuration via l'UI HA."""

    VERSION = 1

    async def async_step_user(self, user_input: dict[str, Any] | None = None
                             ) -> FlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            err, pasc = await _test_login(self.hass, user_input[CONF_EMAIL],
                                          user_input[CONF_PASSWORD])
            if err is None:
                # un seul compteur par point de comptage (PASC) : refuse
                # la création d'une entrée en double sur le même PASC
                await self.async_set_unique_id(str(pasc))
                self._abort_if_unique_id_configured()
                return self.async_create_entry(
                    title=f"Linky SER ({user_input[CONF_EMAIL]})",
                    data={
                        CONF_EMAIL: user_input[CONF_EMAIL],
                        CONF_PASSWORD: user_input[CONF_PASSWORD],
                        CONF_INITIAL_HISTORY:
                            user_input[CONF_INITIAL_HISTORY],
                    },
                    options={CONF_PRICE: user_input[CONF_PRICE]},
                )
            errors["base"] = "invalid_auth"
            _LOGGER.warning("Linky SER login refusé: %s", err)
        return self.async_show_form(step_id="user",
                                     data_schema=STEP_USER,
                                     errors=errors)

    @classmethod
    def async_get_options_flow(cls, entry):
        from .options_flow import LinkySerOptionsFlow
        return LinkySerOptionsFlow(entry)
