"""Scriptable loopback TCP peer with two simulated stations."""

import asyncio
from contextlib import asynccontextmanager

from custom_components.soyal_access.protocol import Frame, FrameBuffer


class SimulatedGateway:
    def __init__(self) -> None:
        self.requests: asyncio.Queue[Frame] = asyncio.Queue()
        self.responses: asyncio.Queue[tuple[bytes, ...] | None] = asyncio.Queue()
        self._handlers: set[asyncio.Task] = set()

    @asynccontextmanager
    async def listen(self):
        server = await asyncio.start_server(self._handle, "127.0.0.1", 0)
        async with server:
            try:
                yield server.sockets[0].getsockname()[1]
            finally:
                tasks = tuple(self._handlers)
                for task in tasks:
                    task.cancel()
                await asyncio.gather(*tasks, return_exceptions=True)

    async def _handle(self, reader, writer):
        task = asyncio.current_task()
        self._handlers.add(task)
        try:
            parser = FrameBuffer()
            while chunk := await reader.read(4096):
                requests = parser.feed(chunk)
                if requests:
                    for request in requests:
                        self.requests.put_nowait(request)
                    response = await self.responses.get()
                    if response is None:
                        await reader.read()
                        break
                    for response_chunk in response:
                        writer.write(response_chunk)
                        await writer.drain()
                        await asyncio.sleep(0)
                    break
        finally:
            writer.close()
            try:
                await writer.wait_closed()
            finally:
                self._handlers.discard(task)
