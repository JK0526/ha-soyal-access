"""Traffic and communication diagnostics."""

from typing import ClassVar

from homeassistant.components.sensor import SensorDeviceClass, SensorEntity
from homeassistant.helpers.entity import EntityCategory

from .entity import SoyalEntity
from .history import EVENT_TYPES


async def async_setup_entry(hass, entry, async_add_entities):
    manager = entry.runtime_data
    async_add_entities(
        [
            QueueDepth(manager, None, "queue_depth"),
            ErrorCount(manager, None, "bus_errors"),
            *(ErrorCount(manager, node, "errors") for node in manager.nodes),
            *(LastResponse(manager, node, "last_response") for node in manager.nodes),
            *(Outcome(manager, node, "outcome") for node in manager.nodes),
        ]
    )

    async_add_entities(
        [
            LatestStoredEvent(manager, node, "latest_stored_event")
            for node in manager.nodes
        ],
        update_before_add=True,
    )


class LatestStoredEvent(SoyalEntity, SensorEntity):
    """Non-identifying summary of the last stored event."""

    _attr_should_poll = True

    async def async_update(self):
        event, phase = await self.manager.events.station_summary(self.station)
        self._attr_extra_state_attributes = {"sync_phase": phase}
        if event is None:
            self._attr_native_value = None
            return
        code = event["event_type"]
        self._attr_native_value = EVENT_TYPES.get(code, f"0x{code:02X}")
        self._attr_extra_state_attributes.update(
            controller_time=event["controller_time"],
            imported_at=event["received_at"].replace(" ", "T") + "Z",
            origin=event["origin"],
        )


class DiagnosticSensor(SoyalEntity, SensorEntity):
    _attr_entity_category = EntityCategory.DIAGNOSTIC


class QueueDepth(DiagnosticSensor):
    @property
    def native_value(self):
        return self.manager.waiting

    @property
    def extra_state_attributes(self):
        return {"in_flight": self.manager.active}


class ErrorCount(DiagnosticSensor):
    @property
    def native_value(self):
        if self.station is None:
            return self.manager.errors
        return self.manager.status[self.station].errors


class LastResponse(DiagnosticSensor):
    _attr_device_class = SensorDeviceClass.TIMESTAMP

    @property
    def native_value(self):
        return self.manager.status[self.station].last_response


class Outcome(DiagnosticSensor):
    _attr_device_class = SensorDeviceClass.ENUM
    _attr_options: ClassVar[list[str]] = [
        "idle",
        "pending",
        "ack",
        "data_response",
        "nack",
        "disconnected",
        "uncertain",
        "storage_error",
    ]

    @property
    def native_value(self):
        status = self.manager.status[self.station]
        if status.outcome == "idle" and self.station in self.manager.uncertain:
            return "uncertain"
        return status.outcome
