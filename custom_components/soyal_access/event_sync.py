"""Explicit, bounded historical import. Wire deletion is restricted to loopback."""

import asyncio
import sqlite3
from collections import defaultdict
from time import monotonic

from homeassistant.exceptions import HomeAssistantError

from .protocol import Frame, ProtocolError, ReplyFormat, ReplyKind, encode

EVENTS = ReplyFormat(frozenset({0x0B, 0x10, 0x1C}), min_data_size=24)
DELETE = ReplyFormat()


class SyncStopped(RuntimeError):
    """Manual investigation is required before proceeding."""


class FifoSync:
    def __init__(self, store, client, *, allow_remove=False, control_uncertain=None):
        self.control_uncertain = control_uncertain
        self.store = store
        self.client = client
        self.allow_remove = allow_remove
        self._locks = defaultdict(asyncio.Lock)
        self.rate = {}
        self._active = set()
        self._closed = False

    async def _head(self, station):
        reply = await self.client.transact(Frame(station, 0x25), EVENTS)
        if reply.kind is not ReplyKind.DATA:
            # No confirmed empty-queue encoding. Even ACK is inconclusive.
            raise SyncStopped("unknown_queue_head")
        return encode(reply.frame)

    async def batch(self, station, *, limit=16, interval=0.1):
        if self._closed:
            raise SyncStopped("closed")
        task = asyncio.current_task()
        self._active.add(task)
        try:
            return await self._batch(station, limit, interval)
        finally:
            self._active.discard(task)

    async def close(self):
        self._closed = True
        tasks = tuple(self._active)
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)

    async def _batch(self, station, limit, interval):
        if limit < 1 or interval < 0:
            raise ValueError("Invalid batch limits")
        count = 0
        started = monotonic()
        async with self._locks[station]:
            for _ in range(limit):
                if (
                    self.control_uncertain is not None
                    and station in self.control_uncertain
                ):
                    raise SyncStopped("control_uncertain")
                state = await self.store.state(station)
                if state["phase"] != "idle":
                    raise SyncStopped(state["phase"])
                try:
                    head = await self._head(station)
                    if state["head"] is not None and head != state["head"]:
                        raise SyncStopped("checkpoint_mismatch")
                    await self.store.save_head(station, head)
                    if not self.allow_remove:
                        break
                    await self.store.mark_sent(station, head)
                    reply = await self.client.transact(Frame(station, 0x37), DELETE)
                    if reply.kind is not ReplyKind.ACK:
                        raise SyncStopped("delete_rejected")
                    following = await self._head(station)
                    if head == following:
                        raise SyncStopped("identical_head")
                    await self.store.advance(station, head, following)
                except (
                    OSError,
                    ProtocolError,
                    RuntimeError,
                    ValueError,
                    sqlite3.Error,
                ) as err:
                    reason = (
                        str(err) if isinstance(err, SyncStopped) else type(err).__name__
                    )
                    await self.store.stop(station, reason)
                    raise SyncStopped(reason) from err
                count += 1
                self.rate[station] = count / (monotonic() - started)
                await asyncio.sleep(interval)
        return count


class EventPreview:
    """Bounded loopback-only preview used by the TCP simulator."""

    def __init__(self, transport, events, control, *, enabled, limit):
        self.transport = transport
        self.events = events
        self.control = control
        self.enabled = enabled
        self.limit = limit
        self.attempts = 0
        self.duplicates = 0
        self.status = "disabled"
        self.task = None

    def start(self):
        if self.enabled:
            self.status = "waiting"
            self.task = asyncio.create_task(self._poll_events())

    async def stop(self):
        if self.task is not None:
            self.task.cancel()
            await asyncio.gather(self.task, return_exceptions=True)

    async def _poll_events(self):
        # Completion-based delay prevents overlap or catch-up bursts.
        while self.attempts < self.limit:
            await asyncio.sleep(0.5)
            async with self.transport.background_slot() as acquired:
                if not acquired or self.control.paused:
                    continue
                if 2 in self.control.uncertain or self.transport.is_uncertain(2):
                    self.status = "uncertain"
                    return
                try:
                    self.attempts += 1
                    reply = await self.transport.transact(Frame(2, 0x25), EVENTS)
                    if reply.kind is not ReplyKind.DATA:
                        self.status = (
                            "empty_ack_unverified"
                            if reply.kind is ReplyKind.ACK
                            else "nack"
                        )
                        return
                    raw = encode(reply.frame)
                    state = await self.events.state(2)
                    if state["head"] == raw and state["event_id"] is not None:
                        self.duplicates += 1
                        self.status = "duplicate_head"
                        return
                    elif state["phase"] == "idle" and state["head"] is None:
                        await self.events.save_head(2, raw)
                    else:
                        self.status = "checkpoint_mismatch"
                        return
                    self.status = "oldest_event_saved"
                except asyncio.CancelledError:
                    await self.control.record_read_uncertainty(2, "legacy_unknown")
                    raise
                except (
                    OSError,
                    ValueError,
                    RuntimeError,
                    HomeAssistantError,
                    sqlite3.Error,
                ) as err:
                    self.status = "query_failed_" + type(err).__name__
                    await self.control.record_read_uncertainty(2, "legacy_unknown")
                    return
