"""One TCP connection and one in-flight transaction per loaded config entry."""

import asyncio
import logging
from time import monotonic

from .protocol import HEADER, Frame, ProtocolError, decode, encode

_FIFO = encode(Frame(2, 0x25))
_POP = encode(Frame(2, 0x37))
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
        self.session_id = 0
        self.pending = None
        self.tainted = False
        self._expected = {9: (3,)}
        self.last_io = None

    async def start(self):
        async with self._lifecycle:
            if self.connected or self.pending is not None:
                return
            await self._close_stream()
            try:
                async with asyncio.timeout(self._timeout):
                    reader, writer = await asyncio.open_connection(
                        self._host, self._port
                    )
            except OSError as err:
                _LOGGER.warning("Gateway TCP connection failed: %s", err)
                return
            self.session_id += 1
            self.tainted = False
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
                if buffer[0] != HEADER or (
                    len(buffer) >= 2 and buffer[1] not in self._expected
                ):
                    raise ProtocolError("Unexpected SOYAL response header or length")
                if len(buffer) < 2 or len(buffer) < buffer[1] + 2:
                    continue
                frame = decode(buffer)
                codes = self._expected[buffer[1]]
                valid_code = (
                    frame.code not in (4, 5) if codes is None else frame.code in codes
                )
                if frame.node != 0 or not valid_code or frame.payload[0] != 2:
                    raise ProtocolError("Unexpected SOYAL response type or source")
                if buffer[1] == 9:
                    self.last_io = bytes(buffer)
                buffer.clear()
                pending.set_result((monotonic(), frame))
        except (OSError, ProtocolError) as err:
            self.tainted = True
            if self.pending is not None and not self.pending.done():
                self.pending.set_exception(err)
        finally:
            if self.pending is not None and not self.pending.done():
                self.tainted = True
                error = (
                    ProtocolError("Truncated SOYAL response")
                    if buffer
                    else ConnectionError("Gateway disconnected")
                )
                self.pending.set_exception(error)
            writer.close()
            self._set_disconnected()

    async def query_node02(self):
        elapsed, _ = await self._exchange(_QUERY, expected={9: (3,)})
        return elapsed

    async def poll_node02(self):
        _, frame = await self._exchange(_QUERY, expected={9: (3,), 5: (5,)})
        return frame if frame.code == 3 else None

    async def control_node02(self, action):
        _, frame = await self._exchange(
            _CONTROLS[action], expected={9: (3,), 5: (4, 5)}
        )
        return frame.code

    async def read_oldest_event(self):
        _, frame = await self._exchange(_FIFO, expected={5: (4, 5), 29: None})
        return frame

    async def pop_oldest_event(self):
        _, frame = await self._exchange(_POP, expected={5: (4, 5), 10: (4, 5)})
        return frame.code

    async def _exchange(self, packet, *, expected):
        if (
            not self.connected
            or self._writer is None
            or self._writer.is_closing()
            or self.tainted
            or self.pending is not None
        ):
            raise NotSentError("Gateway cannot accept a transaction")
        future = asyncio.get_running_loop().create_future()
        self._expected = expected
        self.pending = future
        started = monotonic()
        try:
            async with asyncio.timeout(self._timeout):
                self._writer.write(packet)
                await self._writer.drain()
                received, frame = await future
                if self.tainted:
                    raise ProtocolError("Unexpected data after the response")
                return received - started, frame
        except (TimeoutError, OSError, ProtocolError, asyncio.CancelledError):
            self.tainted = True
            self._link_changed()
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
            await self._close_stream()

    async def _close_stream(self):
        if self._writer is None:
            return
        self._writer.close()
        self._receiver.cancel()
        await asyncio.gather(self._receiver, return_exceptions=True)
        if self.pending is not None and not self.pending.done():
            self.tainted = True
            self.pending.set_exception(ConnectionError("Gateway disconnected"))
        await self._wait_closed(self._writer)
        self._receiver = None
        self._writer = None
        self._set_disconnected()
