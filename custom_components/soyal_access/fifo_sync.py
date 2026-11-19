"""Single FIFO worker with write-ahead deletion intent and read-only recovery."""

import asyncio
import logging
import sqlite3
from hashlib import sha256

from homeassistant.exceptions import HomeAssistantError
from homeassistant.util import dt as dt_util

from .fifo import parse_event
from .protocol import ProtocolError, encode
from .transport import NotSentError

_LOGGER = logging.getLogger(__name__)
ENABLE_KEY = "fifo_auto_sync"
IDLE_DELAY = 1.0
EVENT_DELAY = 0.2


def utc_now():
    return dt_util.utcnow().isoformat()


class FifoSync:
    def __init__(self, hass, entry, hub, storage):
        self.hass = hass
        self.entry = entry
        self.hub = hub
        self.storage = storage
        self.task = None
        self._single_task = None
        self._last_single = {}
        self._wake = asyncio.Event()
        self.state = "idle" if self.enabled else "disabled"
        self.attributes = {}
        self._advance_candidate = None

    @property
    def enabled(self):
        return self.entry.data.get(ENABLE_KEY) is True

    def set_enabled(self, enabled):
        if self._single_task is not None:
            if enabled:
                raise HomeAssistantError("Single FIFO synchronization is running")
            return
        self._advance_candidate = None
        self.hass.config_entries.async_update_entry(
            self.entry, data={**self.entry.data, ENABLE_KEY: enabled}
        )
        self._wake.set()
        if not enabled or self.state == "disabled":
            self._report("idle" if enabled else "disabled")
        else:
            self.hub.notify()

    def start(self):
        self.task = asyncio.create_task(self._run(), name="soyal_access_fifo_sync")

    async def sync_one(self):
        if self.enabled:
            raise HomeAssistantError("Automatic FIFO synchronization must be disabled")
        if not self.hub.can_query:
            raise HomeAssistantError("Gateway or Node 02 is unavailable")
        if (
            self._single_task
            or self.hub.fifo_operation.locked()
            or self.hub.fifo_pending
        ):
            raise HomeAssistantError("FIFO operation already running")
        task = self._single_task = asyncio.create_task(self._run_single())
        try:
            await task
        finally:
            self._single_task = None

    async def _run_single(self):
        async with self.hub.fifo_operation:
            self._report("communication_failed")
            await self._cycle()
            result = self._last_single["last_single_sync_result"]
            if result not in ("empty", "popped_confirmed"):
                raise HomeAssistantError(f"Single FIFO synchronization: {result}")

    async def stop(self):
        if self._single_task is not None:
            self._single_task.cancel()
            await asyncio.gather(self._single_task, return_exceptions=True)

        if self.task is not None:
            self.task.cancel()
            await asyncio.gather(self.task, return_exceptions=True)
            self.task = None

    def _report(self, state, **attributes):
        if self._single_task is not None:
            self._last_single = {
                "last_single_sync_result": state,
                "last_single_sync_utc": utc_now(),
                **attributes,
            }
        if not self.enabled:
            state, attributes = "disabled", {}
        attributes = {**self._last_single, **attributes}
        if (state, attributes) != (self.state, self.attributes):
            self.state, self.attributes = state, attributes
            self.hub.notify()

    def _ready(self):
        return (self.enabled or self._single_task is not None) and self.hub.can_query

    async def _run(self):
        while True:
            if not self.enabled:
                self._wake.clear()
                await self._wake.wait()
                continue
            delay = IDLE_DELAY
            if self._ready() and not self.hub.fifo_pending:
                async with self.hub.fifo_operation:
                    if self._ready() and not self.hub.fifo_pending:
                        delay = await self._cycle()
            await asyncio.sleep(delay)

    def _check_wire_priority(self, session=None):
        if (
            not self._ready()
            or self.hub.busy
            or self.hub.fifo_pending
            or (session is not None and session != self.hub.transport.session_id)
        ):
            raise NotSentError("FIFO synchronization yielded")

    async def _read(self):
        async with self.hub._transaction:
            self._check_wire_priority()
            return await self.hub.transport.read_oldest_event()

    async def _verified_pop(self, expected_raw, session):
        async with self.hub._transaction:
            self._check_wire_priority(session)
            try:
                frame = await self.hub.transport.read_oldest_event()
            except (OSError, ProtocolError) as err:
                # The removal was never attempted, regardless of the read result.
                raise NotSentError("FIFO head could not be revalidated") from err
            if frame.code in (4, 5) or encode(frame) != expected_raw:
                return None
            self._check_wire_priority(session)
            return await self.hub.transport.pop_oldest_event()

    async def _cycle(self):
        previous_candidate = self._advance_candidate
        self._advance_candidate = None
        pending = None
        try:
            pending = await self.storage.progress()
            if pending and pending[1] == "pop_uncertain":
                self._report("pop_uncertain", pending_event_id=pending[0])
                if self._single_task is not None:
                    return IDLE_DELAY
            session = self.hub.transport.session_id
            frame = await self._read()
            if frame.code in (4, 5):
                if pending and pending[1] == "pop_uncertain":
                    # A same/empty/negative response does not resolve a lost ACK.
                    return IDLE_DELAY
                if pending or frame.code == 5:
                    self._report(
                        "paused",
                        last_error_type="fifo_nack"
                        if frame.code == 5
                        else "head_mismatch",
                    )
                else:
                    self._report("empty" if self._single_task else "idle")
                return IDLE_DELAY

            event = parse_event(frame)
            event["read_at"] = utc_now()
            raw = bytes.fromhex(event["raw_frame_hex"])
            if pending and pending[1] == "pop_uncertain":
                if raw != pending[2]:
                    candidate = (pending[0], session, raw)
                    if candidate != previous_candidate:
                        self._advance_candidate = candidate
                        return IDLE_DELAY
                    await self.storage.transition(
                        pending[0],
                        "pop_uncertain",
                        "advanced_observed",
                        utc_now(),
                        sha256(raw).hexdigest(),
                    )
                    self._report("idle", last_resolution="advanced_observed")
                # Never send a removal in an uncertainty reconciliation cycle.
                return IDLE_DELAY
            if pending and raw != pending[2]:
                self._report(
                    "paused",
                    last_error_type="head_mismatch",
                    pending_event_id=pending[0],
                )
                return IDLE_DELAY

            event_id = await self.storage.save_head(event)
            self._report(
                "saved" if self._single_task else "syncing", pending_event_id=event_id
            )
            if (
                not self._ready()
                or self.hub.fifo_pending
                or session != self.hub.transport.session_id
            ):
                return IDLE_DELAY

            # A crash after this commit is ambiguous even if the write hasn't started.
            await self.storage.transition(event_id, "saved", "pop_uncertain", utc_now())
            pending = (event_id, "pop_uncertain", raw)
            try:
                code = await self._verified_pop(raw, session)
            except NotSentError:
                # This branch is reached only when the removal was never attempted.
                await self.storage.transition(
                    event_id, "pop_uncertain", "saved", utc_now()
                )
                return IDLE_DELAY
            if code is None:
                await self.storage.transition(
                    event_id, "pop_uncertain", "saved", utc_now()
                )
                self._report(
                    "paused", pending_event_id=event_id, last_error_type="head_mismatch"
                )
                return IDLE_DELAY
            if code != 4:
                self._report(
                    "pop_uncertain",
                    pending_event_id=event_id,
                    last_error_type="pop_nack",
                )
                return IDLE_DELAY

            await self.storage.transition(
                event_id, "pop_uncertain", "popped_confirmed", utc_now()
            )
            if self._single_task:
                self._report("popped_confirmed", last_event_id=event_id)
            else:
                self._report("idle", last_sync_utc=utc_now())
            return EVENT_DELAY
        except NotSentError:
            return IDLE_DELAY
        except (sqlite3.Error, OSError, ProtocolError) as err:
            # SQLite may fail on either side of the wire operation. If an intent
            # committed, its journal entry remains the authority after restart.
            error = type(err).__name__
            if (
                isinstance(err, (OSError, sqlite3.Error))
                and not self.hub.transport.tainted
            ):
                _LOGGER.warning("FIFO synchronization paused (%s)", error)
            if pending and pending[1] == "pop_uncertain":
                self._report(
                    "pop_uncertain", pending_event_id=pending[0], last_error_type=error
                )
            elif isinstance(err, sqlite3.Error) or (
                isinstance(err, OSError) and self.hub.can_query
            ):
                self._report("storage_error", last_error_type=error)
            else:
                self._report(
                    "communication_failed" if self._single_task else "paused",
                    last_error_type=error,
                )
            return IDLE_DELAY
