"""Explicit pulse-open action."""

from homeassistant.components.button import ButtonEntity

from .entity import ControlEntity


async def async_setup_entry(hass, entry, async_add_entities):
    manager = entry.runtime_data
    async_add_entities(
        PulseButton(manager, station, "pulse_open") for station in manager.nodes
    )


class PulseButton(ControlEntity, ButtonEntity):
    async def async_press(self):
        await self.manager.control(self.station, 0x84)
