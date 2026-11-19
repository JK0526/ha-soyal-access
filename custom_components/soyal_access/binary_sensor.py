"""Station and gateway connectivity diagnostics."""

from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
)
from homeassistant.helpers.entity import EntityCategory

from .entity import SoyalEntity


async def async_setup_entry(hass, entry, async_add_entities):
    manager = entry.runtime_data
    async_add_entities(
        [
            GatewayLink(manager, None, "tcp_connected"),
            *(StationLink(manager, node, "reachable") for node in manager.nodes),
            *(Uncertain(manager, node, "uncertain") for node in manager.nodes),
        ]
    )


class GatewayLink(SoyalEntity, BinarySensorEntity):
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_device_class = BinarySensorDeviceClass.CONNECTIVITY

    @property
    def is_on(self):
        return self.manager.coordinator.connected


class StationLink(GatewayLink):
    @property
    def is_on(self):
        return self.manager.coordinator.status[self.station].reachable


class Uncertain(SoyalEntity, BinarySensorEntity):
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    @property
    def is_on(self):
        return (
            self.station in self.manager.control.uncertain
            and self.manager.coordinator.status[self.station].outcome != "pending"
        )
