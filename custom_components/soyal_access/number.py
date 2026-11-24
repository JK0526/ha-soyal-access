"""Native hold-open timeout configuration for Node 02."""

from homeassistant.components.number import NumberEntity, NumberMode
from homeassistant.const import UnitOfTime
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.entity import EntityCategory


async def async_setup_entry(hass, entry, async_add_entities):
    async_add_entities([HoldOpenTimeoutNumber(entry.runtime_data)])


class HoldOpenTimeoutNumber(NumberEntity):
    _attr_has_entity_name = True
    _attr_translation_key = "hold_open_timeout"
    _attr_should_poll = False
    _attr_icon = "mdi:timer-cog-outline"
    _attr_entity_category = EntityCategory.CONFIG
    _attr_native_min_value = 1
    _attr_native_max_value = 1440
    _attr_native_step = 1
    _attr_native_unit_of_measurement = UnitOfTime.MINUTES
    _attr_mode = NumberMode.BOX

    def __init__(self, hub):
        self._manager = hub.hold_open_timeout
        self._attr_device_info = hub.node_device
        self._attr_unique_id = f"{hub.entry_id}:2:hold_open_timeout"

    @property
    def native_value(self):
        return self._manager.minutes

    async def async_added_to_hass(self):
        self.async_on_remove(
            async_dispatcher_connect(
                self.hass, self._manager.signal, self.async_write_ha_state
            )
        )

    async def async_set_native_value(self, value):
        await self._manager.async_configure(minutes=value)
