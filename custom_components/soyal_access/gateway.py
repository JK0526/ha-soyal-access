"""Shared gateway lifecycle, transaction checks, and write-ahead uncertainty."""

import asyncio
import logging
from dataclasses import dataclass
from datetime import datetime
from hashlib import sha256
from pathlib import Path

from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.dispatcher import async_dispatcher_send
from homeassistant.helpers.storage import Store
from homeassistant.util import dt as dt_util

from .client import BusClient, BusClosedError
from .const import DOMAIN
from .protocol import Frame, ProtocolError, ReplyFormat, ReplyKind

_LOGGER = logging.getLogger(__name__)


@dataclass(slots=True)
class StationStatus:
    reachable: bool | None = None
    last_response: datetime | None = None
    errors: int = 0
    outcome: str = "idle"


class GatewayManager:
    """Own one serialized command stream for the configured endpoint."""

    def __init__(self, hass, entry):
        self.hass = hass
        self.entry_id = entry.entry_id
        self.signal = f"{DOMAIN}_{self.entry_id}"
        self.nodes = tuple(map(int, entry.data["stations"].split(",")))
        self.status = {node: StationStatus() for node in self.nodes}
        self.enabled = entry.data["enable_control"]
        self.connected = False
        self.waiting = 0
        self.active = False
        self.errors = 0
        self.uncertain: set[int] = set()
        self._lock = asyncio.Lock()
        self._closed = False
        self._retry_after = 0.0
        self._delay = entry.data["reconnect_delay"]
        endpoint = repr((entry.data["host"], entry.data["port"])).encode()
        key = sha256(endpoint).hexdigest()
        self._store = Store(
            hass, 1, f"{DOMAIN}.{key}", private=True, atomic_writes=True
        )
        self._client = None
        if self.enabled:
            self._client = BusClient(
                entry.data["host"],
                entry.data["port"],
                set(self.nodes),
                timeout=entry.data["timeout"],
                enable_control=True,
                link_changed=self._link_changed,
            )

    def _storage_evidence_exists(self):
        path = Path(self._store.path)
        return path.exists() or any(path.parent.glob(f"{path.name}.corrupt.*"))

    async def load(self):
        saved = await self._store.async_load()
        if saved is None:
            # HA renames malformed JSON and returns None. Do not treat that as first setup.
            if not await self.hass.async_add_executor_job(
                self._storage_evidence_exists
            ):
                return
        elif (
            isinstance(saved, dict)
            and isinstance(nodes := saved.get("uncertain"), list)
            and all(type(node) is int and 1 <= node <= 255 for node in nodes)
        ):
            self.uncertain = set(nodes)
            return
        _LOGGER.error("SOYAL pending control state is invalid; control is blocked")
        self.uncertain.update(self.nodes)
        for status in self.status.values():
            status.outcome = "storage_error"

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

    def _notify(self):
        async_dispatcher_send(self.hass, self.signal)

    def _link_changed(self, connected):
        self.connected = connected
        self._notify()

    async def _save(self):
        await self._store.async_save({"uncertain": sorted(self.uncertain)})

    async def control(self, station, action):
        if self._client is None or self._closed:
            raise HomeAssistantError("Gateway control is disabled")
        self.waiting += 1
        self._notify()
        try:
            await self._lock.acquire()
        finally:
            self.waiting -= 1
            self._notify()
        try:
            if self._closed or station in self.uncertain:
                raise HomeAssistantError("Station control is blocked")
            if asyncio.get_running_loop().time() < self._retry_after:
                raise HomeAssistantError("Gateway reconnect cooldown is active")
            self.active = True
            self._notify()
            await self._control_locked(station, action)
        finally:
            self.active = False
            self._lock.release()
            self._notify()

    async def _control_locked(self, station, action):
        status = self.status[station]
        status.outcome = "pending"
        self.uncertain.add(station)
        self._notify()
        try:
            # Persist before sending so HA restarts cannot erase ambiguity.
            await self._save()
        except (OSError, HomeAssistantError) as err:
            status.outcome = "storage_error"
            raise HomeAssistantError("Unable to save control state") from err
        try:
            reply = await self._client.transact(
                Frame(station, 0x21, bytes([action])), ReplyFormat()
            )
        except (OSError, ProtocolError, BusClosedError) as err:
            self.errors += 1
            status.errors += 1
            status.reachable = False
            self._retry_after = asyncio.get_running_loop().time() + self._delay
            status.outcome = (
                "uncertain" if self._client.is_uncertain(station) else "disconnected"
            )
            _LOGGER.warning(
                "SOYAL control at station %02d: %s (%s)",
                station,
                status.outcome,
                type(err).__name__,
            )
            if status.outcome == "disconnected":
                await self._clear_confirmed(station)
            raise HomeAssistantError(f"SOYAL control {status.outcome}") from err
        except asyncio.CancelledError:
            status.outcome = "uncertain"
            raise
        status.reachable = True
        status.last_response = dt_util.utcnow()
        status.outcome = "ack" if reply.kind is ReplyKind.ACK else "nack"
        await self._clear_confirmed(station)
        if reply.kind is not ReplyKind.ACK:
            raise HomeAssistantError("Controller rejected the command")

    async def _clear_confirmed(self, station):
        self.uncertain.remove(station)
        try:
            await self._save()
        except (OSError, HomeAssistantError) as err:
            self.uncertain.add(station)
            self.status[station].outcome = "storage_error"
            raise HomeAssistantError("Unable to clear saved control state") from err
        except asyncio.CancelledError:
            self.uncertain.add(station)
            self.status[station].outcome = "uncertain"
            raise

    async def close(self):
        self._closed = True
        if self._client is not None:
            await self._client.close()
        async with self._lock:
            pass
