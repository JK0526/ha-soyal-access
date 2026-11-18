"""Persistent stream with one in-flight transaction and strict source matching."""

import asyncio

from .client import BusClient
from .protocol import FrameBuffer, encode


class SessionClient(BusClient):
    """Reuse the verified codec and quarantine across TCP reconnections."""

    def __init__(self, host, port, stations, *, timeout, enable_control, link_changed):
        # Transport host validation is provided by Config Flow.
        super().__init__(
            host,
            port,
            stations,
            timeout=timeout,
            enable_control=True,
            link_changed=link_changed,
        )
        self._allowed = {(0x21, b"\x00\x00")}
        if enable_control:
            self._allowed.update((0x21, bytes([code])) for code in (0x82, 0x83, 0x84))
        self._reader_task = None
        self._writer = None
        self._pending = None

    async def _disconnect(self):
        if self._writer is not None:
            self._writer.close()
            try:
                async with asyncio.timeout(self._timeout):
                    await self._writer.wait_closed()
            except (OSError, TimeoutError):
                self._writer.transport.abort()
            self._writer = None
        if self._reader_task is not None:
            self._reader_task.cancel()
            await asyncio.gather(self._reader_task, return_exceptions=True)
            self._reader_task = None
        self._link_changed(False)

    async def _receive(self, reader):
        parser = FrameBuffer()
        try:
            while chunk := await reader.read(4096):
                for frame in parser.feed(chunk):
                    if self._pending is None:
                        continue
                    request, expected, future = self._pending
                    reply = expected.classify(frame)
                    if (
                        reply is not None
                        and reply.source == request.node
                        and not future.done()
                    ):
                        future.set_result(reply)
            raise ConnectionError("Gateway disconnected")
        except (OSError, ValueError) as err:
            if self._pending is not None and not self._pending[2].done():
                self._pending[2].set_exception(err)
            self._link_changed(False)

    async def connect(self):
        async with self._lock:
            if self._closed:
                raise ConnectionError("Session closed")
            async with asyncio.timeout(self._timeout):
                await self._connect()

    async def _connect(self):
        if self._reader_task is None or self._reader_task.done():
            await self._disconnect()
            reader, self._writer = await asyncio.open_connection(self._host, self._port)
            self._reader_task = asyncio.create_task(self._receive(reader))
            self._link_changed(True)

    async def _exchange(self, request, reply_format):
        try:
            async with asyncio.timeout(self._timeout):
                await self._connect()
                future = asyncio.get_running_loop().create_future()
                self._pending = (request, reply_format, future)
                self._uncertain_stations.add(request.node)
                self._writer.write(encode(request))
                await self._writer.drain()
                reply = await future
                self._uncertain_stations.remove(request.node)
                return reply
        except BaseException:
            await self._disconnect()
            raise
        finally:
            self._pending = None

    async def close(self):
        await super().close()
        await self._disconnect()
