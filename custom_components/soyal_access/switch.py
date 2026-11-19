"""Hold-open command with no physical state feedback."""

from homeassistant.components.switch import SwitchEntity

from .entity import ControlEntity


async def async_setup_entry(hass, entry, async_add_entities):
    manager = entry.runtime_data
    async_add_entities(
        HoldSwitch(manager, station, "hold_open") for station in manager.nodes
    )


class HoldSwitch(ControlEntity, SwitchEntity):
    _attr_assumed_state = True
    _attr_is_on = None

    async def async_turn_on(self, **kwargs):
        await self.manager.control.execute(self.station, 0x82)

    async def async_turn_off(self, **kwargs):
        await self.manager.control.execute(self.station, 0x83)
