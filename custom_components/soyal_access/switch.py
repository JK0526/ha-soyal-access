"""Hold-open command state for the configured node."""

from homeassistant.components.switch import SwitchEntity
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.entity import EntityCategory


async def async_setup_entry(hass, entry, async_add_entities):
    async_add_entities(
        [
            HoldOpenSwitch(entry.runtime_data),
            HoldOpenProtectionSwitch(entry.runtime_data),
        ]
    )


class HoldOpenSwitch(SwitchEntity):
    _attr_has_entity_name = True
    _attr_translation_key = "hold_open"
    _attr_assumed_state = True
    _attr_should_poll = False

    def __init__(self, hub):
        self._hub = hub
        self._attr_device_info = hub.node_device
        self._attr_unique_id = f"{hub.node_uid}:hold_open"

    @property
    def extra_state_attributes(self):
        return self._hub.hold_open_timeout.attributes

    @property
    def available(self):
        return self._hub.can_pulse

    @property
    def is_on(self):
        return self._hub.hold_open

    async def async_added_to_hass(self):
        self.async_on_remove(
            async_dispatcher_connect(
                self.hass, self._hub.hold_open_timeout.signal, self.async_write_ha_state
            )
        )
        self.async_on_remove(
            async_dispatcher_connect(
                self.hass, self._hub.signal, self.async_write_ha_state
            )
        )

    async def async_turn_on(self, **kwargs):
        user_id = self._context.user_id if self._context else None
        await self._hub.set_hold_open(True, user_id=user_id)

    async def async_turn_off(self, **kwargs):
        user_id = self._context.user_id if self._context else None
        await self._hub.set_hold_open(False, user_id=user_id)


class HoldOpenProtectionSwitch(SwitchEntity):
    """Local configuration; remains available when the gateway is offline."""

    _attr_has_entity_name = True
    _attr_translation_key = "hold_open_timeout_protection"
    _attr_should_poll = False
    _attr_icon = "mdi:shield-check"
    _attr_entity_category = EntityCategory.CONFIG

    def __init__(self, hub):
        self._manager = hub.hold_open_timeout
        self._attr_device_info = hub.node_device
        self._attr_unique_id = f"{hub.node_uid}:hold_open_timeout_protection"

    @property
    def is_on(self):
        return self._manager.enabled

    async def async_added_to_hass(self):
        self.async_on_remove(
            async_dispatcher_connect(
                self.hass, self._manager.signal, self.async_write_ha_state
            )
        )

    async def async_turn_on(self, **kwargs):
        await self._manager.async_configure(enabled=True)

    async def async_turn_off(self, **kwargs):
        await self._manager.async_configure(enabled=False)
