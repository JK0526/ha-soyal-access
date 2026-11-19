"""One TCP connection and one in-flight transaction per loaded config entry."""

import asyncio
import logging
from time import monotonic

from .protocol import HEADER, Frame, ProtocolError, decode, encode

_QUERY = encode(Frame(2, 0x21, b"\x00\x00"))
_CONTROLS = {code: encode(Frame(2, 0x21, bytes([code]))) for code in (0x82, 0x83, 0x84)}


class NotSentError(ConnectionError):
    """The transaction was rejected before any socket write."""


_LOGGER = logging.getLogger(__name__)


class Transport:
    def __init__(self, host, port, timeout, link_changed):
        self._host = host
        self._port = port
        self._timeout = timeout
        self._link_changed = link_changed
        self._lifecycle = asyncio.Lock()
        self._receiver = None
        self._writer = None
        self.connected = False
        self.pending = None
        self.tainted = False
        self._control_pending = False

    async def start(self):
        async with self._lifecycle:
            if self._receiver is not None or self.tainted:
                return
            try:
                async with asyncio.timeout(self._timeout):
                    reader, writer = await asyncio.open_connection(
                        self._host, self._port
                    )
            except OSError as err:
                _LOGGER.warning("Gateway TCP connection failed: %s", err)
                return
            self._writer = writer
            self._receiver = asyncio.create_task(
                self._receive(reader, writer), name="soyal_access_tcp_rx"
            )
            self.connected = True
            self._link_changed()

    async def _receive(self, reader, writer):
        buffer = bytearray()
        try:
            while chunk := await reader.read(4096):
                pending = self.pending
                if pending is None or pending.done():
                    self.tainted = True
                    break
                buffer.extend(chunk)
                lengths = (5, 9) if self._control_pending else (9,)
                if buffer[0] != HEADER or (
                    len(buffer) >= 2 and buffer[1] not in lengths
                ):
                    raise ProtocolError("Unexpected I/O response header or length")
                if len(buffer) < 2 or len(buffer) < buffer[1] + 2:
                    continue
                frame = decode(buffer)
                codes = {5: (4, 5), 9: (3,)}[buffer[1]]
                if frame.node != 0 or frame.code not in codes or frame.payload[0] != 2:
                    raise ProtocolError("Unexpected I/O response type or source")
                buffer.clear()
                pending.set_result((monotonic(), frame.code))
        except (OSError, ProtocolError) as err:
            if self.pending is not None and not self.pending.done():
                self.pending.set_exception(err)
        finally:
            if self.pending is not None and not self.pending.done():
                error = (
                    ProtocolError("Truncated I/O response")
                    if buffer
                    else ConnectionError("Gateway disconnected")
                )
                self.pending.set_exception(error)
            writer.close()
            self._set_disconnected()

    async def query_node02(self):
        elapsed, _ = await self._exchange(_QUERY, control=False)
        return elapsed

    async def control_node02(self, action):
        _, code = await self._exchange(_CONTROLS[action], control=True)
        return code

    async def _exchange(self, packet, *, control):
        if (
            not self.connected
            or self._writer is None
            or self._writer.is_closing()
            or self.tainted
            or self.pending is not None
        ):
            raise NotSentError("Gateway cannot accept a transaction")
        future = asyncio.get_running_loop().create_future()
        self._control_pending = control
        self.pending = future
        started = monotonic()
        try:
            async with asyncio.timeout(self._timeout):
                self._writer.write(packet)
                await self._writer.drain()
                received, code = await future
                return received - started, code
        except (TimeoutError, OSError, ProtocolError, asyncio.CancelledError):
            self.tainted = True
            await self.close()
            raise
        finally:
            self.pending = None
            if not future.done():
                future.cancel()
            elif not future.cancelled():
                future.exception()

    def _set_disconnected(self):
        if self.connected:
            self.connected = False
            self._link_changed()

    async def _wait_closed(self, writer):
        try:
            async with asyncio.timeout(self._timeout):
                await writer.wait_closed()
        except OSError:
            writer.transport.abort()
        except asyncio.CancelledError:
            writer.transport.abort()
            raise

    async def close(self):
        async with self._lifecycle:
            if self._receiver is not None:
                self._writer.close()
                self._receiver.cancel()
                await asyncio.gather(self._receiver, return_exceptions=True)
                self._receiver = None
                await self._wait_closed(self._writer)
                self._writer = None
                self._set_disconnected()
