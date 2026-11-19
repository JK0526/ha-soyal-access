"""Entity identity and push updates shared across platforms."""

from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.entity import Entity


class SoyalEntity(Entity):
    _attr_should_poll = False
    _attr_has_entity_name = True

    def __init__(self, manager, station, key):
        self.manager = manager
        self.station = station
        self._attr_unique_id = f"{manager.entry_id}:{station or 'gateway'}:{key}"
        self._attr_device_info = manager.device_info(station)
        self._attr_translation_key = key

    async def async_added_to_hass(self):
        await super().async_added_to_hass()
        self.async_on_remove(
            async_dispatcher_connect(
                self.hass, self.manager.signal, self.async_write_ha_state
            )
        )


class ControlEntity(SoyalEntity):
    @property
    def available(self):
        return self.manager.control.control_block_reason(self.station) is None
