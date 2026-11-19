"""Hold-open command state for Node 02."""

from homeassistant.components.switch import SwitchEntity
from homeassistant.helpers.dispatcher import async_dispatcher_connect


async def async_setup_entry(hass, entry, async_add_entities):
    async_add_entities([HoldOpenSwitch(entry.runtime_data)])


class HoldOpenSwitch(SwitchEntity):
    _attr_has_entity_name = True
    _attr_translation_key = "hold_open"
    _attr_assumed_state = True
    _attr_should_poll = False

    def __init__(self, hub):
        self._hub = hub
        self._attr_device_info = hub.node_device
        self._attr_unique_id = f"{hub.entry_id}:2:hold_open"

    @property
    def available(self):
        return self._hub.can_pulse

    @property
    def is_on(self):
        return self._hub.hold_open

    async def async_added_to_hass(self):
        self.async_on_remove(
            async_dispatcher_connect(
                self.hass, self._hub.signal, self.async_write_ha_state
            )
        )

    async def async_turn_on(self, **kwargs):
        await self._hub.set_hold_open(True)

    async def async_turn_off(self, **kwargs):
        await self._hub.set_hold_open(False)
