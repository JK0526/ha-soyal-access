"""SOYAL Access Control."""

from .const import DOMAIN, PLATFORMS


async def async_setup_entry(hass, entry):
    """Load one shared gateway manager."""
    from homeassistant.helpers import device_registry as dr

    from .gateway import GatewayManager
    from .websocket import async_register

    manager = GatewayManager(hass, entry)
    await manager.load()
    entry.runtime_data = manager
    async_register(hass)
    registry = dr.async_get(hass)
    registry.async_get_or_create(
        config_entry_id=entry.entry_id, **manager.device_info()
    )
    for station in manager.nodes:
        registry.async_get_or_create(
            config_entry_id=entry.entry_id, **manager.device_info(station)
        )
    try:
        await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    except BaseException:
        await manager.close()
        raise
    manager.start_monitoring()
    return True


async def async_unload_entry(hass, entry):
    """Unload platforms and close the shared transport."""
    if not await hass.config_entries.async_unload_platforms(entry, PLATFORMS):
        return False
    await entry.runtime_data.close()
    return True


async def async_remove_config_entry_device(hass, entry, device_entry):
    """Allow removal of stations absent from the current configuration."""
    return (DOMAIN, entry.entry_id) not in device_entry.identifiers and not any(
        (DOMAIN, f"{entry.entry_id}:{node}") in device_entry.identifiers
        for node in map(int, entry.data["stations"].split(","))
    )
