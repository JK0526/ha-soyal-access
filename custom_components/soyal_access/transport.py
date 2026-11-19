"""One receive-only TCP connection per loaded config entry."""

import asyncio
import logging

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

    async def start(self):
        async with self._lifecycle:
            if self._receiver is not None:
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
        try:
            while await reader.read(4096):
                # Consume incoming bytes to observe EOF without interpreting frames.
                pass
        except OSError as err:
            _LOGGER.debug("Gateway TCP connection lost: %s", err)
        finally:
            writer.close()
            self._set_disconnected()
            await self._wait_closed(writer)

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
