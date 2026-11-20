"""Read-only access event history for dashboard cards."""

import asyncio
import logging
import sqlite3

from homeassistant.components.sensor import SensorEntity
from homeassistant.core import callback
from homeassistant.helpers.dispatcher import async_dispatcher_connect

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(hass, entry, async_add_entities):
    async_add_entities([AccessEventHistory(entry.runtime_data)], True)


class AccessEventHistory(SensorEntity):
    _attr_has_entity_name = True
    _attr_should_poll = False
    _attr_translation_key = "access_event_history"
    _unrecorded_attributes = frozenset({"events"})

    def __init__(self, hub):
        self._hub = hub
        self._attr_unique_id = f"{hub.entry_id}:2:access_event_history"
        self._attr_device_info = hub.node_device
        self._attr_extra_state_attributes = {"events": []}
        self._history_lock = asyncio.Lock()

    async def async_added_to_hass(self):
        await super().async_added_to_hass()
        self.async_on_remove(
            async_dispatcher_connect(
                self.hass, self._hub.events_signal, self._events_saved
            )
        )
        # Refresh after subscribing to include events committed during setup.
        await self.async_update()

    @callback
    def _events_saved(self):
        self.async_schedule_update_ha_state(True)

    async def async_update(self):
        async with self._history_lock:
            try:
                count, events = await self.hass.async_add_executor_job(
                    self._hub.fifo_storage.read_history
                )
            except (OSError, sqlite3.Error) as err:
                self._attr_available = False
                _LOGGER.warning("SOYAL access history unavailable: %s", err)
                return
            self._attr_native_value = count
            self._attr_extra_state_attributes = {"events": events}
            self._attr_available = True
