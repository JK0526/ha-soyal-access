"""Most recent manual query result."""

from homeassistant.components.sensor import SensorDeviceClass, SensorEntity
from homeassistant.helpers.dispatcher import async_dispatcher_connect


async def async_setup_entry(hass, entry, async_add_entities):
    async_add_entities(
        [
            QueryResult(entry.runtime_data),
        ]
    )


class QueryResult(SensorEntity):
    _attr_has_entity_name = True
    _attr_should_poll = False
    _attr_translation_key = "query_result"
    _attr_device_class = SensorDeviceClass.ENUM

    def __init__(self, hub):
        self._attr_options = [
            "not_queried",
            "success",
            "timeout",
            "communication_failed",
            "invalid_response",
        ]
        self._hub = hub
        self._attr_device_info = hub.node_device
        self._attr_unique_id = f"{hub.entry_id}:2:{self._attr_translation_key}"

    @property
    def native_value(self):
        return self._hub.result

    @property
    def extra_state_attributes(self):
        return {
            "last_success": self._hub.last_success,
            "elapsed_ms": self._hub.elapsed_ms,
        }

    async def async_added_to_hass(self):
        self.async_on_remove(
            async_dispatcher_connect(
                self.hass, self._hub.signal, self.async_write_ha_state
            )
        )
