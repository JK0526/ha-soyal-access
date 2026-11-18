"""Non-identifying event synchronization diagnostics."""


async def async_get_config_entry_diagnostics(hass, entry):
    manager = entry.runtime_data
    stations = {}
    for node in manager.nodes:
        state = await manager.events.state(node)
        stations[node] = {
            key: state[key]
            for key in (
                "phase",
                "saved",
                "processed",
                "error",
                "possibly_sent",
                "last_success",
            )
        }
        stations[node]["events_per_second"] = (
            manager.event_sync.rate.get(node, 0.0) if manager.event_sync else 0.0
        )
    return {"automatic_sync": False, "stations": stations}
