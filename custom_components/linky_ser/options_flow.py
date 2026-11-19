"""Options flow Linky SER : tarif €/kWh."""
from __future__ import annotations

import voluptuous as vol

from homeassistant import config_entries
from homeassistant.core import callback

from .const import CONF_PRICE


class LinkySerOptionsFlow(config_entries.OptionsFlow):
    """Modifier le tarif (les données restent, seule la suite change)."""

    def __init__(self, entry) -> None:
        self.entry = entry

    async def async_step_init(self, user_input=None):
        if user_input is not None:
            return self.async_create_entry(
                title="", data={CONF_PRICE: user_input[CONF_PRICE]})
        return self.async_show_form(
            step_id="init",
            data_schema=vol.Schema({
                vol.Required(CONF_PRICE,
                             default=self.entry.options.get(
                                 CONF_PRICE, 0.1348)): vol.Coerce(float),
            }))
