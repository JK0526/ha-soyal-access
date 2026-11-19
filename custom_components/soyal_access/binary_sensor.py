"""Gateway TCP connectivity entity."""

from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
)
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.entity import EntityCategory

from .const import DOMAIN


async def async_setup_entry(hass, entry, async_add_entities):
    async_add_entities([GatewayLink(entry.entry_id, entry.runtime_data)])


class GatewayLink(BinarySensorEntity):
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_device_class = BinarySensorDeviceClass.CONNECTIVITY
    _attr_should_poll = False
    _attr_has_entity_name = True
    _attr_translation_key = "tcp_connected"

    def __init__(self, entry_id, hub):
        self._hub = hub
        self._attr_unique_id = f"{entry_id}:gateway:tcp_connected"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, entry_id)}, name="SOYAL Gateway"
        )

    @property
    def is_on(self):
        return self._hub.transport.connected

    async def async_added_to_hass(self):
        await super().async_added_to_hass()
        self.async_on_remove(
            async_dispatcher_connect(
                self.hass, self._hub.signal, self.async_write_ha_state
            )
        )
