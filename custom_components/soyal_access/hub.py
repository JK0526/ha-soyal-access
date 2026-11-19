"""Own the components and lifecycle of one SOYAL config entry."""

from pathlib import Path

from homeassistant.helpers.device_registry import DeviceInfo

from .backup import backup_lock
from .const import DOMAIN
from .control import Control
from .coordinator import Coordinator
from .event_store import EventStore
from .event_sync import EventPreview
from .transport import Transport


class Hub:
    def __init__(self, hass, entry, *, simulate_preview=False):
        data = entry.data
        self.entry_id = entry.entry_id
        self.signal = f"{DOMAIN}_{self.entry_id}"
        self.nodes = tuple(map(int, data["stations"].split(",")))
        self.coordinator = Coordinator(hass, self.signal, self.nodes)
        simulate_preview = simulate_preview and data["host"] in ("127.0.0.1", "::1")
        self.transport = Transport(
            data["host"],
            data["port"],
            set(self.nodes),
            timeout=data["timeout"],
            enable_control=data.get("enable_control") is True,
            link_changed=self.coordinator.link_changed,
            simulate_preview=simulate_preview,
        )
        self.control = Control(
            hass, data, self.transport, self.coordinator, self.stop_background
        )
        self.events = EventStore(
            Path(hass.config.path("soyal_access", f"{self.entry_id}.sqlite3")),
            self.entry_id,
            backup_lock(hass),
        )
        self.preview = EventPreview(
            self.transport,
            self.events,
            self.control,
            enabled=simulate_preview
            and data.get("node02_event_preview") is True
            and 2 in self.nodes,
            limit=data.get("node02_event_limit", 1),
        )

    async def load(self):
        await self.control.load()
        await self.events.open()

    def start_monitoring(self):
        self.coordinator.start(self.transport, self.control)
        self.preview.start()

    async def stop_background(self):
        await self.coordinator.stop()
        await self.preview.stop()

    async def close(self):
        self.control.closed = True
        await self.stop_background()
        await self.transport.close()
        async with self.transport.operation_lock:
            await self.events.close()

    def device_info(self, station=None):
        if station is None:
            return DeviceInfo(
                identifiers={(DOMAIN, self.entry_id)},
                name="SOYAL Gateway",
            )
        return DeviceInfo(
            identifiers={(DOMAIN, f"{self.entry_id}:{station}")},
            via_device=(DOMAIN, self.entry_id),
            name=f"SOYAL AR-725E — Node {station:02d}",
            manufacturer="SOYAL",
            model="AR-725E",
        )
