"""Most recent manual query result."""

from homeassistant.components.sensor import SensorDeviceClass, SensorEntity
from homeassistant.helpers.dispatcher import async_dispatcher_connect


async def async_setup_entry(hass, entry, async_add_entities):
    async_add_entities(
        [
            QueryResult(entry.runtime_data),
            FifoResult(entry.runtime_data),
            FifoSyncStatus(entry.runtime_data),
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


class FifoResult(QueryResult):
    _attr_translation_key = "fifo_read_result"
    _unrecorded_attributes = frozenset({"raw_frame_hex"})

    def __init__(self, hub):
        super().__init__(hub)
        self._attr_options = [
            "not_queried",
            "empty",
            "event",
            "nack",
            "timeout",
            "invalid_response",
            "communication_failed",
        ]

    @property
    def native_value(self):
        return self._hub.fifo_result

    @property
    def extra_state_attributes(self):
        return self._hub.fifo_attributes


class FifoSyncStatus(QueryResult):
    _attr_translation_key = "fifo_sync_status"

    def __init__(self, hub):
        super().__init__(hub)
        self._attr_options = [
            "disabled",
            "waiting_for_gateway",
            "idle",
            "syncing",
            "paused",
            "storage_error",
            "pop_uncertain",
        ]

    @property
    def native_value(self):
        return self._hub.fifo_sync.state

    @property
    def extra_state_attributes(self):
        return self._hub.fifo_sync.attributes
