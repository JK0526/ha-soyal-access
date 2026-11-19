"""SOYAL Gateway connectivity."""

from homeassistant.const import EVENT_HOMEASSISTANT_STOP

from .const import PLATFORMS
from .hub import Hub


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
