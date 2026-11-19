"""Gateway connectivity and Node 02 read-only sensors."""

from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
)
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.entity import EntityCategory

from .const import DOMAIN


async def async_setup_entry(hass, entry, async_add_entities):
    hub = entry.runtime_data
    async_add_entities(
        [
            GatewayLink(entry.entry_id, hub),
            NodeOnline(entry.entry_id, hub),
            UnlockOutput(entry.entry_id, hub),
        ]
    )


class SoyalBinarySensor(BinarySensorEntity):
    _attr_should_poll = False
    _attr_has_entity_name = True

    def __init__(self, hub):
        self._hub = hub

    async def async_added_to_hass(self):
        await super().async_added_to_hass()
        self.async_on_remove(
            async_dispatcher_connect(
                self.hass, self._hub.signal, self.async_write_ha_state
            )
        )


class GatewayLink(SoyalBinarySensor):
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_device_class = BinarySensorDeviceClass.CONNECTIVITY
    _attr_translation_key = "tcp_connected"

    def __init__(self, entry_id, hub):
        super().__init__(hub)
        self._attr_unique_id = f"{entry_id}:gateway:tcp_connected"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, entry_id)}, name="SOYAL Gateway"
        )

    @property
    def is_on(self):
        return self._hub.transport.connected


class NodeOnline(SoyalBinarySensor):
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_device_class = BinarySensorDeviceClass.CONNECTIVITY
    _attr_translation_key = "node_online"

    def __init__(self, entry_id, hub):
        super().__init__(hub)
        self._attr_unique_id = f"{entry_id}:2:node_online"
        self._attr_device_info = hub.node_device

    @property
    def is_on(self):
        return self._hub.node_online


class UnlockOutput(SoyalBinarySensor):
    _attr_translation_key = "unlock_output"

    def __init__(self, entry_id, hub):
        super().__init__(hub)
        self._attr_unique_id = f"{entry_id}:2:unlock_output"
        self._attr_device_info = hub.node_device

    @property
    def available(self):
        return self._hub.node_online and self._hub.unlock_output is not None

    @property
    def is_on(self):
        return self._hub.unlock_output
