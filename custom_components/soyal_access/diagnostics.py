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
        stations[node]["control_block_reason"] = manager.control_block_reason(node)
        stations[node]["pending_kind"] = (
            manager.pending_kind.get(node) if node in manager.uncertain else None
        )
    return {
        "health_error": manager.health_error,
        "automatic_sync": False,
        "event_preview_attempts": manager.event_preview_attempts,
        "event_preview_limit": manager.event_preview_limit,
        "node02_event_poll": manager.event_poll_status,
        "repeated_queue_heads": manager.event_duplicates,
        "transactions": getattr(manager._client, "transaction_count", 0),
        "transaction_seconds": getattr(manager._client, "transaction_seconds", 0.0),
        "control_enabled": manager.enabled,
        "stations": stations,
    }
