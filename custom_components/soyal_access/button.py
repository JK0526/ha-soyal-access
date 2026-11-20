"""Manual Node 02 pulse control."""

from homeassistant.components.button import ButtonEntity
from homeassistant.helpers.dispatcher import async_dispatcher_connect


async def async_setup_entry(hass, entry, async_add_entities):
    async_add_entities([PulseButton(entry.runtime_data)])


class PulseButton(ButtonEntity):
    _attr_has_entity_name = True
    _attr_translation_key = "pulse_open"

    def __init__(self, hub):
        self._hub = hub
        self._attr_device_info = hub.node_device
        self._attr_unique_id = f"{hub.entry_id}:2:{self._attr_translation_key}"

    @property
    def available(self):
        return self._hub.can_pulse

    async def async_added_to_hass(self):
        self.async_on_remove(
            async_dispatcher_connect(
                self.hass, self._hub.signal, self.async_write_ha_state
            )
        )

    async def async_press(self):
        await self._hub.pulse()
