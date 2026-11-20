"""SOYAL Gateway connectivity."""

from homeassistant.const import EVENT_HOMEASSISTANT_STOP
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er

from .const import DOMAIN, PLATFORMS
from .hub import Hub

_REMOVED_ENTITIES = {
    ("button", "manual_query"),
    ("sensor", "query_result"),
    ("button", "read_fifo_event"),
    ("button", "sync_one_fifo_event"),
    ("sensor", "fifo_read_result"),
    ("sensor", "fifo_sync_status"),
    ("switch", "fifo_auto_sync"),
}


def _remove_legacy_features(hass, entry):
    """Remove only the retired entity registry entries for this config entry."""
    data = dict(entry.data)
    for key in ("desired_auto_sync", "fifo_auto_sync"):
        data.pop(key, None)
    if data != entry.data:
        hass.config_entries.async_update_entry(entry, data=data)

    registry = er.async_get(hass)
    for domain, suffix in _REMOVED_ENTITIES:
        unique_id = f"{entry.entry_id}:2:{suffix}"
        entity_id = registry.async_get_entity_id(domain, DOMAIN, unique_id)
        if (
            entity_id
            and registry.async_get(entity_id).config_entry_id == entry.entry_id
        ):
            registry.async_remove(entity_id)


async def async_setup_entry(hass, entry):
    """Open one socket and expose its connection state."""
    _remove_legacy_features(hass, entry)
    gateway_device = dr.async_get(hass).async_get_or_create(
        config_entry_id=entry.entry_id,
        identifiers={(DOMAIN, entry.entry_id)},
        name="SOYAL Gateway",
    )
    hub = Hub(hass, entry, gateway_device.id)
    await hub.warn_legacy_uncertainty()
    await hub.initialize_fifo_storage()
    entry.runtime_data = hub
    try:
        await hub.transport.start()
        await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    except BaseException:
        await hub.transport.close()
        raise

    hub.start_polling()

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
