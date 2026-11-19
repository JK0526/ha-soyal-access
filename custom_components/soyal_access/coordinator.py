"""Gateway and station health scheduling and HA state notifications."""

import asyncio
from dataclasses import dataclass
from datetime import datetime

from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.dispatcher import async_dispatcher_send
from homeassistant.util import dt as dt_util

from .client import StationReplyUncertainError
from .protocol import Frame, ProtocolError, ReplyKind
from .transport import IO_REPLY


@dataclass(slots=True)
class StationStatus:
    reachable: bool | None = None
    last_response: datetime | None = None
    errors: int = 0
    outcome: str = "idle"


class Coordinator:
    def __init__(self, hass, signal, nodes):
        self.hass = hass
        self.signal = signal
        self.status = {node: StationStatus() for node in nodes}
        self.connected = False
        self.errors = 0
        self.health_error = None
        self.task = None

    def notify(self):
        async_dispatcher_send(self.hass, self.signal)

    def link_changed(self, connected):
        if self.connected != connected:
            self.connected = connected
            self.notify()

    def start(self, transport, control):
        self.task = asyncio.create_task(self._monitor(transport, control))

    async def stop(self):
        if self.task is not None:
            self.task.cancel()
            await asyncio.gather(self.task, return_exceptions=True)

    async def _monitor(self, transport, control):
        while True:
            await asyncio.sleep(15)
            await self.poll_once(transport, control)

    async def poll_once(self, transport, control):
        async with transport.background_slot() as acquired:
            if acquired and not control.paused:
                await self._poll(transport, control)

    async def _poll(self, transport, control):
        try:
            await transport.connect()
        except OSError:
            self.health_error = "tcp_connect_failed"
            self.connected = False
            for status in self.status.values():
                status.reachable = False
            self.notify()
            return

        node = 2
        if node not in self.status or transport.waiting or control.paused:
            return
        status = self.status[node]
        was_reachable = status.reachable
        try:
            if node in control.uncertain:
                status.reachable = False
                return
            reply = await transport.transact(Frame(node, 0x21, b"\x00\x00"), IO_REPLY)
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
                if isinstance(err, ProtocolError) and transport.is_uncertain(node)
                else "read_failed"
            )
            await control.record_read_uncertainty(node, "health_read")
        except asyncio.CancelledError:
            await control.record_read_uncertainty(node, "health_read")
            raise
        finally:
            if status.reachable != was_reachable:
                self.notify()
