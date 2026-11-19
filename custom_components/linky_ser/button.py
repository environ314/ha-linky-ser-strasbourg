"""Bouton de synchronisation manuelle Linky SER."""
from __future__ import annotations

import logging

from homeassistant.components.button import ButtonEntity
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from . import LinkySerHub
from .const import DOMAIN

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(hass: HomeAssistant, entry, async_add_entities
                            ) -> None:
    hub: LinkySerHub = hass.data[DOMAIN][entry.entry_id]
    async_add_entities([LinkySerSyncButton(hub, entry)])


class LinkySerSyncButton(ButtonEntity):
    """Déclenche un cycle OAuth + import à la demande."""

    _attr_name = "Linky SER sync"
    _attr_icon = "mdi:sync"

    def __init__(self, hub: LinkySerHub, entry) -> None:
        self.hub = hub
        self._attr_unique_id = f"{entry.entry_id}_sync"

    async def async_press(self) -> None:
        await self.hub.sync_now()
