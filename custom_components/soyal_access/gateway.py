"""Shared gateway lifecycle, transaction checks, and write-ahead uncertainty."""

import asyncio
import logging
import sqlite3
from dataclasses import dataclass
from datetime import datetime
from hashlib import sha256
from pathlib import Path

from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.dispatcher import async_dispatcher_send
from homeassistant.helpers.storage import Store
from homeassistant.util import dt as dt_util

from .backup import backup_lock
from .client import BusClient, BusClosedError, StationReplyUncertainError
from .const import DOMAIN
from .event_store import EventStore
from .event_sync import EVENTS, FifoSync
from .protocol import Frame, ProtocolError, ReplyFormat, ReplyKind, encode
from .session import SessionClient

_LOGGER = logging.getLogger(__name__)

# 0x21 door control returns a source ID followed by four I/O data bytes.
# Other commands can choose their own data codes and payload sizes.
_CONTROL_21_REPLY = ReplyFormat(frozenset({0x03}), exact_data_size=4)


@dataclass(slots=True)
class StationStatus:
    reachable: bool | None = None
    last_response: datetime | None = None
    errors: int = 0
    outcome: str = "idle"


class GatewayManager:
    """Own one serialized command stream for the configured endpoint."""

    def __init__(
        self,
        hass,
        entry,
        *,
        simulate_fifo=False,
        allow_remove=False,
        simulate_preview=False,
    ):
        self.hass = hass
        self.entry_id = entry.entry_id
        self.signal = f"{DOMAIN}_{self.entry_id}"
        self.nodes = tuple(map(int, entry.data["stations"].split(",")))
        self.status = {node: StationStatus() for node in self.nodes}
        self.enabled = entry.data.get("enable_control") is True
        self.connected = False
        self.waiting = 0
        self.active = False
        self.errors = 0
        self.uncertain: set[int] = set()
        self._lock = asyncio.Lock()
        self._closed = False
        self._recovery_station = None
        self._recovery_ready = False
        self._invalid_storage = False
        self.pending_kind = {}
        self.health_error = None
        self._retry_after = 0.0
        self._delay = entry.data["reconnect_delay"]
        endpoint = repr((entry.data["host"], entry.data["port"])).encode()
        key = sha256(endpoint).hexdigest()
        self._store = Store(
            hass, 1, f"{DOMAIN}.{key}", private=True, atomic_writes=True
        )
        self.events = EventStore(
            Path(hass.config.path("soyal_access", f"{entry.entry_id}.sqlite3")),
            entry.entry_id,
            backup_lock(hass),
        )
        simulate_preview = simulate_preview and entry.data["host"] in (
            "127.0.0.1",
            "::1",
        )
        self.event_preview_enabled = (
            simulate_preview and entry.data.get("node02_event_preview") is True
        )
        self.event_preview_limit = entry.data.get("node02_event_limit", 1)
        self.event_preview_attempts = 0
        self._event_task = None
        self.event_poll_status = "disabled"
        self.event_duplicates = 0
        self._monitor_task = None
        self._client = (
            BusClient(
                entry.data["host"],
                entry.data["port"],
                set(self.nodes),
                timeout=entry.data["timeout"],
                enable_control=self.enabled,
                simulate_fifo=True,
                link_changed=self._link_changed,
            )
            if simulate_fifo
            else SessionClient(
                entry.data["host"],
                entry.data["port"],
                set(self.nodes),
                timeout=entry.data["timeout"],
                enable_control=self.enabled,
                link_changed=self._link_changed,
                simulate_preview=simulate_preview,
            )
        )

        self.event_sync = (
            FifoSync(
                self.events,
                self._client,
                allow_remove=allow_remove,
                control_uncertain=self.uncertain,
            )
            if simulate_fifo
            else None
        )

    def start_monitoring(self):
        self._monitor_task = asyncio.create_task(self._monitor())
        if self.event_preview_enabled and 2 in self.nodes:
            self.event_poll_status = "waiting"
            self._event_task = asyncio.create_task(self._poll_events())

    async def _poll_events(self):
        # Completion-based delay prevents overlap or catch-up bursts.
        while self.event_preview_attempts < self.event_preview_limit:
            await asyncio.sleep(0.5)
            if self.waiting or self._lock.locked():
                continue
            async with self._lock:
                if 2 in self.uncertain or self._client.is_uncertain(2):
                    self.event_poll_status = "uncertain"
                    return
                try:
                    self.event_preview_attempts += 1
                    reply = await self._client.transact(Frame(2, 0x25), EVENTS)
                    if reply.kind is not ReplyKind.DATA:
                        self.event_poll_status = (
                            "empty_ack_unverified"
                            if reply.kind is ReplyKind.ACK
                            else "nack"
                        )
                        return
                    raw = encode(reply.frame)
                    state = await self.events.state(2)
                    if state["head"] == raw and state["event_id"] is not None:
                        self.event_duplicates += 1
                        self.event_poll_status = "duplicate_head"
                        return
                    elif state["phase"] == "idle" and state["head"] is None:
                        await self.events.save_head(2, raw)
                    else:
                        self.event_poll_status = "checkpoint_mismatch"
                        return
                    self.event_poll_status = "oldest_event_saved"
                except asyncio.CancelledError:
                    if self._client.is_uncertain(2):
                        self.uncertain.add(2)
                        await self._save()
                        self._notify()
                    raise
                except (
                    OSError,
                    ValueError,
                    RuntimeError,
                    HomeAssistantError,
                    sqlite3.Error,
                ) as err:
                    self.event_poll_status = "query_failed_" + type(err).__name__
                    if self._client.is_uncertain(2):
                        self.uncertain.add(2)
                        await self._save()
                        self._notify()
                    return

    async def _monitor(self):
        # Startup delay avoids competing with entry initialization.
        while True:
            await asyncio.sleep(15)
            try:
                await self._client.connect()
            except OSError:
                self.health_error = "tcp_connect_failed"
                self.connected = False
                for status in self.status.values():
                    status.reachable = False
                self._notify()
                continue
            for node in self.nodes:
                if node != 2:
                    continue
                status = self.status[node]
                was_reachable = status.reachable
                if self.waiting:
                    continue
                async with self._lock:
                    if self._recovery_station is not None or node in self.uncertain:
                        status.reachable = False
                        self._notify()
                        continue
                    try:
                        reply = await self._client.transact(
                            Frame(node, 0x21, b"\x00\x00"), _CONTROL_21_REPLY
                        )
                        if reply.kind is not ReplyKind.DATA:
                            raise ProtocolError("Monitor requires I/O data")
                        self.health_error = None
                        status.reachable = True
                        status.last_response = dt_util.utcnow()
                    except (
                        OSError,
                        HomeAssistantError,
                        ProtocolError,
                        StationReplyUncertainError,
                    ) as err:
                        status.reachable = False
                        status.errors += 1
                        self.errors += 1
                        self.health_error = (
                            "malformed_reply"
                            if isinstance(err, ProtocolError)
                            and self._client.is_uncertain(node)
                            else "read_failed"
                        )
                        await self._record_read_uncertainty(node)
                    except asyncio.CancelledError:
                        await self._record_read_uncertainty(node)
                        raise
                    finally:
                        if status.reachable != was_reachable:
                            self._notify()

    def _storage_evidence_exists(self):
        path = Path(self._store.path)
        return path.exists() or any(path.parent.glob(f"{path.name}.corrupt.*"))

    async def load(self):
        saved = await self._store.async_load()
        await self.events.open()
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
        await self._store.async_save(
            {
                "uncertain": sorted(self.uncertain),
                "kind": {
                    str(node): self.pending_kind.get(node, "legacy_unknown")
                    for node in self.uncertain
                },
            }
        )

    async def _record_read_uncertainty(self, node):
        if self._client.is_uncertain(node):
            self.uncertain.add(node)
            self.pending_kind[node] = "health_read"
            await self._save()

    async def prepare_recovery(self, station):
        if (
            self._closed
            or self._invalid_storage
            or station not in self.nodes
            or station not in self.uncertain
            or self._recovery_station is not None
        ):
            raise HomeAssistantError("Station is not eligible for recovery")
        self._recovery_station = station
        self._notify()
        for task in (self._monitor_task, self._event_task):
            if task is not None:
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
        async with self._lock:
            await self._client.close()
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
        async with self._lock:
            if (
                self._closed
                or not self._recovery_ready
                or self._recovery_station != station
            ):
                raise HomeAssistantError("Prepare recovery for this station first")
            await self._check_recovery_record(station)
            await self._clear_confirmed(station)
            self._recovery_ready = False
            self._notify()

    def control_block_reason(self, station):
        """State why the station cannot accept a control command."""
        if self._closed:
            return "gateway_closed"
        if self._recovery_station is not None:
            return "recovery_paused"
        if not self.enabled:
            return "disabled_in_settings"
        if station in self.uncertain:
            outcome = self.status[station].outcome
            if outcome in ("storage_error", "pending"):
                return outcome
            return "uncertain"
        return None

    async def control(self, station, action):
        if not self.enabled or self._closed:
            raise HomeAssistantError("Gateway control is disabled")
        self.waiting += 1
        self._notify()
        try:
            await self._lock.acquire()
        finally:
            self.waiting -= 1
            self._notify()
        try:
            if self.control_block_reason(station) is not None:
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
        self.pending_kind[station] = "control"
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
                Frame(station, 0x21, bytes([action])), _CONTROL_21_REPLY
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
            self.status[station].outcome = "storage_error"
            raise HomeAssistantError("Unable to clear saved control state") from err
        except asyncio.CancelledError:
            self.uncertain.add(station)
            self.status[station].outcome = "uncertain"
            raise

    async def close(self):
        self._closed = True
        if self._monitor_task is not None:
            self._monitor_task.cancel()
            await asyncio.gather(self._monitor_task, return_exceptions=True)
        if self._event_task is not None:
            self._event_task.cancel()
            await asyncio.gather(self._event_task, return_exceptions=True)
        if self.event_sync is not None:
            await self.event_sync.close()
        if self._client is not None:
            await self._client.close()
        async with self._lock:
            await self.events.close()
