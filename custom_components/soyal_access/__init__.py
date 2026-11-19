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
    async def clear_fifo_pause(call):
        if call.data["verified"] is not True:
            raise HomeAssistantError(
                "Verify the FIFO protocol issue before clearing the pause"
            )
        entry = hass.config_entries.async_get_entry(call.data["entry_id"])
        if (
            entry is None
            or entry.domain != DOMAIN
            or entry.state != ConfigEntryState.LOADED
        ):
            raise HomeAssistantError("SOYAL config entry is not loaded")
        await entry.runtime_data.fifo_sync.clear_pause()

    async_register_admin_service(
        hass,
        DOMAIN,
        "clear_fifo_pause",
        clear_fifo_pause,
        schema=vol.Schema(
            {vol.Required("entry_id"): str, vol.Required("verified"): bool}
        ),
    )
    return True


async def async_setup_entry(hass, entry):
    """Open one socket and expose its connection state."""
    hub = Hub(hass, entry)
    await hub.warn_legacy_uncertainty()
    entry.runtime_data = hub
    try:
        await hub.transport.start()
        await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    except BaseException:
        await hub.transport.close()
        raise

    hub.start_polling()
    hub.fifo_sync.start()

    async def stop(_event):
        await hub.stop()

    entry.async_on_unload(hass.bus.async_listen_once(EVENT_HOMEASSISTANT_STOP, stop))
    return True


async def async_unload_entry(hass, entry):
    """Unload the sensor and release the socket."""
    if not await hass.config_entries.async_unload_platforms(entry, PLATFORMS):
        return False
    await entry.runtime_data.stop()
    return True
