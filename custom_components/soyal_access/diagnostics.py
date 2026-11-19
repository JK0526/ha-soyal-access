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
        stations[node]["events_per_second"] = 0.0
        stations[node]["control_block_reason"] = manager.control.control_block_reason(
            node
        )
        stations[node]["pending_kind"] = (
            manager.control.pending_kind.get(node)
            if node in manager.control.uncertain
            else None
        )
    return {
        "health_error": manager.coordinator.health_error,
        "automatic_sync": False,
        "event_preview_attempts": manager.preview.attempts,
        "event_preview_limit": manager.preview.limit,
        "node02_event_poll": manager.preview.status,
        "repeated_queue_heads": manager.preview.duplicates,
        "transactions": getattr(manager.transport, "transaction_count", 0),
        "transaction_seconds": getattr(manager.transport, "transaction_seconds", 0.0),
        "control_enabled": manager.control.enabled,
        "stations": stations,
    }
