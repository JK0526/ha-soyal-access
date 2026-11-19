"""Door actions and durable station quarantine."""

import asyncio
import logging
from hashlib import sha256
from pathlib import Path

from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.storage import Store
from homeassistant.util import dt as dt_util

from .client import BusClosedError
from .const import DOMAIN
from .protocol import Frame, ProtocolError, ReplyKind
from .transport import IO_REPLY

_LOGGER = logging.getLogger(__name__)


class Control:
    def __init__(self, hass, data, transport, coordinator, stop_background):
        self.hass = hass
        self.transport = transport
        self.coordinator = coordinator
        self._stop_background = stop_background
        self.enabled = data.get("enable_control") is True
        self.uncertain: set[int] = set()
        self.pending_kind = {}
        self.closed = False
        self._recovery_station = None
        self._recovery_ready = False
        self._invalid_storage = False
        self._retry_after = 0.0
        self._delay = data["reconnect_delay"]
        endpoint = repr((data["host"], data["port"])).encode()
        key = sha256(endpoint).hexdigest()
        self._store = Store(
            hass, 1, f"{DOMAIN}.{key}", private=True, atomic_writes=True
        )

    @property
    def paused(self):
        return self.closed or self._recovery_station is not None

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
            self.uncertain.update(nodes)
            kinds = saved.get("kind", {})
            for node in nodes:
                kind = kinds.get(str(node)) if isinstance(kinds, dict) else None
                self.pending_kind[node] = (
                    kind if kind in ("control", "health_read") else "legacy_unknown"
                )
            return
        _LOGGER.error("SOYAL pending control state is invalid; control is blocked")
        self._invalid_storage = True
        self.uncertain.update(self.coordinator.status)
        for status in self.coordinator.status.values():
            status.outcome = "storage_error"

    async def _save(self):
        await self._store.async_save(
            {
                "uncertain": sorted(self.uncertain),
                "kind": {
                    str(node): self.pending_kind.get(node, "legacy_unknown")
                    for node in self.uncertain
                },
            }
        )

    async def record_read_uncertainty(self, node, kind):
        if self.transport.is_uncertain(node):
            self.uncertain.add(node)
            self.pending_kind[node] = kind
            try:
                await self._save()
            finally:
                self.coordinator.notify()

    async def prepare_recovery(self, station):
        if (
            self.closed
            or self._invalid_storage
            or station not in self.coordinator.status
            or station not in self.uncertain
            or self._recovery_station is not None
        ):
            raise HomeAssistantError("Station is not eligible for recovery")
        self._recovery_station = station
        self.coordinator.notify()
        await self._stop_background()
        async with self.transport.operation_lock:
            await self.transport.close()
            await self._check_recovery_record(station)
            self._recovery_ready = True
            return self.pending_kind.get(station, "legacy_unknown")

    async def _check_recovery_record(self, station):
        saved = await self._store.async_load()
        if (
            not isinstance(saved, dict)
            or not isinstance(saved.get("uncertain"), list)
            or set(saved["uncertain"]) != self.uncertain
            or station not in self.uncertain
        ):
            raise HomeAssistantError("Persisted uncertainty could not be verified")

    async def confirm_recovery(self, station):
        async with self.transport.operation_lock:
            if (
                self.closed
                or not self._recovery_ready
                or self._recovery_station != station
            ):
                raise HomeAssistantError("Prepare recovery for this station first")
            await self._check_recovery_record(station)
            await self._clear_confirmed(station)
            self._recovery_ready = False
            self.coordinator.notify()

    def control_block_reason(self, station):
        """State why the station cannot accept a control command."""
        if self.closed:
            return "gateway_closed"
        if self._recovery_station is not None:
            return "recovery_paused"
        if not self.enabled:
            return "disabled_in_settings"
        if station in self.uncertain:
            outcome = self.coordinator.status[station].outcome
            if outcome in ("storage_error", "pending"):
                return outcome
            return "uncertain"
        return None

    async def execute(self, station, action):
        if not self.enabled or self.closed:
            raise HomeAssistantError("Gateway control is disabled")
        async with self.transport.control_slot(self.coordinator.notify):
            if self.control_block_reason(station) is not None:
                raise HomeAssistantError("Station control is blocked")
            if asyncio.get_running_loop().time() < self._retry_after:
                raise HomeAssistantError("Gateway reconnect cooldown is active")
            self.transport.active = True
            self.coordinator.notify()
            try:
                await self._control_locked(station, action)
            finally:
                self.transport.active = False
                self.coordinator.notify()

    async def _control_locked(self, station, action):
        status = self.coordinator.status[station]
        status.outcome = "pending"
        self.pending_kind[station] = "control"
        self.uncertain.add(station)
        self.coordinator.notify()
        try:
            # Persist before sending so HA restarts cannot erase ambiguity.
            await self._save()
        except (OSError, HomeAssistantError) as err:
            status.outcome = "storage_error"
            raise HomeAssistantError("Unable to save control state") from err
        try:
            reply = await self.transport.transact(
                Frame(station, 0x21, bytes([action])), IO_REPLY
            )
        except (OSError, ProtocolError, BusClosedError) as err:
            self.coordinator.errors += 1
            status.errors += 1
            status.reachable = False
            self._retry_after = asyncio.get_running_loop().time() + self._delay
            status.outcome = (
                "uncertain" if self.transport.is_uncertain(station) else "disconnected"
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
        status.outcome = {
            ReplyKind.ACK: "ack",
            ReplyKind.NACK: "nack",
            ReplyKind.DATA: "data_response",
        }[reply.kind]
        await self._clear_confirmed(station)
        if reply.kind is ReplyKind.NACK:
            raise HomeAssistantError("Controller rejected the command")

    async def _clear_confirmed(self, station):
        self.uncertain.remove(station)
        try:
            await self._save()
        except (OSError, HomeAssistantError) as err:
            self.uncertain.add(station)
            self.coordinator.status[station].outcome = "storage_error"
            raise HomeAssistantError("Unable to clear saved control state") from err
        except asyncio.CancelledError:
            self.uncertain.add(station)
            self.coordinator.status[station].outcome = "uncertain"
            raise
