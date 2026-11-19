"""SOYAL Gateway connectivity."""

import voluptuous as vol
from homeassistant.config_entries import ConfigEntryState
from homeassistant.const import EVENT_HOMEASSISTANT_STOP
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers.service import async_register_admin_service

from .const import DOMAIN, PLATFORMS
from .hub import Hub

CONFIG_SCHEMA = cv.config_entry_only_config_schema(DOMAIN)


async def async_setup(hass, config):
    async def confirm(call):
        entry = hass.config_entries.async_get_entry(call.data["entry_id"])
        if (
            not call.context.user_id
            or entry is None
            or entry.domain != DOMAIN
            or entry.state is not ConfigEntryState.LOADED
        ):
            raise HomeAssistantError(
                "Select a loaded SOYAL entry as a signed-in administrator"
            )
        if call.service == "confirm_node02_pulse":
            await entry.runtime_data.confirm_pulse()
        else:
            entry.runtime_data.confirm()

    for service in ("confirm_node02_query", "confirm_node02_pulse"):
        async_register_admin_service(
            hass,
            DOMAIN,
            service,
            confirm,
            vol.Schema(
                {
                    vol.Required("entry_id"): str,
                    vol.Required("verified_safe"): vol.All(bool, vol.In([True])),
                }
            ),
        )
    return True


async def async_setup_entry(hass, entry):
    """Open one socket and expose its connection state."""
    hub = Hub(hass, entry)
    entry.runtime_data = hub
    try:
        await hub.transport.start()
        await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    except BaseException:
        await hub.transport.close()
        raise

    async def stop(_event):
        await hub.transport.close()

    entry.async_on_unload(hass.bus.async_listen_once(EVENT_HOMEASSISTANT_STOP, stop))
    return True


async def async_unload_entry(hass, entry):
    """Unload the sensor and release the socket."""
    if not await hass.config_entries.async_unload_platforms(entry, PLATFORMS):
        return False
    await entry.runtime_data.transport.close()
    return True
