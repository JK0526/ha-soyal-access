"""Serialized asyncio RAW TCP transport for Stage 1 loopback simulation."""

import asyncio
from typing import Self

from .protocol import Frame, FrameBuffer, Reply, ReplyFormat, encode

# Exact request shapes allowed on the Stage 1 wire.
_ALLOWED = frozenset({(0x18, b""), (0x21, b"\x00\x00"), (0x25, b"")})


class BusClosedError(RuntimeError):
    """The bus has been shut down permanently."""


class StationReplyUncertainError(RuntimeError):
    """A prior request may still produce a reply from this station."""


class BusClient:
    """Share one instance across every station on a simulated RS485 bus."""

    def __init__(
        self, host: str, port: int, stations: set[int], *, timeout: float = 2.0
    ) -> None:
        if host not in ("127.0.0.1", "::1"):
            raise ValueError("Stage 1 requires a numeric loopback address")
        if not stations or not all(1 <= station <= 255 for station in stations):
            raise ValueError("Configure nonzero byte-sized stations")
        if not 1 <= port <= 65535 or not 0 < timeout < float("inf"):
            raise ValueError("Configure a valid port and positive finite timeout")
        self._host = host
        self._port = port
        self._stations = frozenset(stations)
        self._timeout = timeout
        self._lock = asyncio.Lock()
        self._active: asyncio.Task[Reply] | None = None
        self._closed = False
        self._uncertain_stations: set[int] = set()

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.close()

    async def transact(self, request: Frame, reply_format: ReplyFormat) -> Reply:
        """Run one transaction. The timeout starts after acquiring the bus."""
        if request.node not in self._stations:
            raise ValueError("Station is not configured for this bus")
        if (request.code, request.payload) not in _ALLOWED:
            raise ValueError("Request is outside the Stage 1 allowlist")

        async with self._lock:
            if self._closed:
                raise BusClosedError("SOYAL bus is closed")
            if request.node in self._uncertain_stations:
                raise StationReplyUncertainError(
                    f"Station {request.node} may still have a pending reply"
                )
            self._active = asyncio.create_task(self._exchange(request, reply_format))
            try:
                return await self._active
            finally:
                self._active = None

    async def _exchange(self, request: Frame, reply_format: ReplyFormat) -> Reply:
        # Each exchange has a separate socket and buffer. Never replay a request.
        writer = None
        try:
            async with asyncio.timeout(self._timeout):
                reader, writer = await asyncio.open_connection(self._host, self._port)
                packet = encode(request)
                # A write can succeed in part even when drain fails or is cancelled.
                self._uncertain_stations.add(request.node)
                writer.write(packet)
                await writer.drain()

                parser = FrameBuffer()
                while chunk := await reader.read(4096):
                    for frame in parser.feed(chunk):
                        reply = reply_format.classify(frame)
                        if reply is not None and reply.source == request.node:
                            self._uncertain_stations.remove(request.node)
                            return reply
                raise ConnectionError("SOYAL peer closed before a matching reply")
        finally:
            if writer is not None:
                writer.close()
                try:
                    async with asyncio.timeout(self._timeout):
                        await writer.wait_closed()
                except (OSError, TimeoutError):
                    writer.transport.abort()

    async def close(self) -> None:
        """Cancel the active exchange and prevent queued requests from starting."""
        if not self._closed:
            self._closed = True
            if self._active is not None:
                self._active.cancel()
        async with self._lock:
            pass
