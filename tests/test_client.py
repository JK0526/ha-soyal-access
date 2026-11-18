import asyncio
import unittest
from unittest.mock import AsyncMock, Mock, patch

from custom_components.soyal_access.client import BusClient, BusClosedError
from custom_components.soyal_access.protocol import (
    Frame, ProtocolError, ReplyFormat, ReplyKind, encode,
)
from tests.simulator import SimulatedGateway

STATION_1 = Frame(1, 0x25)
STATION_2 = Frame(2, 0x25)
EVENT_REPLY = ReplyFormat(frozenset({0x0B}), min_data_size=10)


def ack(station, code=4):
    return encode(Frame(0, code, bytes([station])))


class ClientTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.gateway = SimulatedGateway()
        port = await self.enterAsyncContext(self.gateway.listen())
        self.bus = await self.enterAsyncContext(
            BusClient("127.0.0.1", port, {1, 2}, timeout=0.25)
        )

    async def next_request(self):
        return await asyncio.wait_for(self.gateway.requests.get(), 1)

    async def test_split_coalesced_and_unrelated_responses(self):
        event = Frame(0, 0x0B, bytes([2]) + bytes(24))
        raw = encode(event)
        noise = ack(1) + encode(Frame(0, 4)) + encode(Frame(2, 4, b"\x02"))
        self.gateway.responses.put_nowait((noise + raw[:1], raw[1:9], raw[9:]))
        reply = await self.bus.transact(STATION_2, EVENT_REPLY)
        self.assertEqual((reply.kind, reply.source, reply.frame), (ReplyKind.DATA, 2, event))
        self.assertEqual(await self.next_request(), STATION_2)

    async def test_shared_bus_serializes_stations(self):
        first = asyncio.create_task(self.bus.transact(STATION_1, EVENT_REPLY))
        self.assertEqual(await self.next_request(), STATION_1)
        second = asyncio.create_task(self.bus.transact(STATION_2, EVENT_REPLY))
        with self.assertRaises(TimeoutError):
            await asyncio.wait_for(self.gateway.requests.get(), 0.03)
        self.gateway.responses.put_nowait((ack(1),))
        self.assertEqual((await first).kind, ReplyKind.ACK)
        self.assertEqual(await self.next_request(), STATION_2)
        self.gateway.responses.put_nowait((ack(2, 5),))
        self.assertEqual((await second).kind, ReplyKind.NACK)

    async def test_timeout_reconnect_without_replay(self):
        # A stalled script holds the connection open until the client closes.
        self.gateway.responses.put_nowait(None)
        with self.assertRaises(TimeoutError):
            await self.bus.transact(STATION_1, EVENT_REPLY)
        self.assertEqual(await self.next_request(), STATION_1)
        self.gateway.responses.put_nowait((ack(2),))
        self.assertEqual((await self.bus.transact(STATION_2, EVENT_REPLY)).source, 2)
        self.assertEqual(await self.next_request(), STATION_2)
        self.assertTrue(self.gateway.requests.empty())

    async def test_eof_bad_checksum_and_recovery(self):
        for chunks, error in (
            ((ack(1)[:3],), ConnectionError),
            ((ack(1)[:-1] + b"\x00",), ProtocolError),
        ):
            self.gateway.responses.put_nowait(chunks)
            with self.assertRaises(error):
                await self.bus.transact(STATION_1, EVENT_REPLY)
            self.assertEqual(await self.next_request(), STATION_1)
        self.gateway.responses.put_nowait((ack(1),))
        self.assertEqual((await self.bus.transact(STATION_1, EVENT_REPLY)).source, 1)

    async def test_connection_failure_and_recovery(self):
        connect = AsyncMock(side_effect=ConnectionRefusedError)
        with patch("asyncio.open_connection", connect):
            with self.assertRaises(ConnectionRefusedError):
                await self.bus.transact(STATION_1, EVENT_REPLY)
            connect.assert_awaited_once()
        self.gateway.responses.put_nowait((ack(1),))
        self.assertEqual((await self.bus.transact(STATION_1, EVENT_REPLY)).source, 1)

    async def test_shutdown_error_aborts_transport(self):
        reader = AsyncMock()
        reader.read.return_value = ack(1)
        writer = Mock()
        writer.drain = AsyncMock()
        writer.wait_closed = AsyncMock(side_effect=ConnectionResetError)
        with patch("asyncio.open_connection", AsyncMock(return_value=(reader, writer))):
            reply = await self.bus.transact(STATION_1, EVENT_REPLY)
        self.assertEqual(reply.source, 1)
        writer.close.assert_called_once()
        writer.transport.abort.assert_called_once()

    async def test_cancel_queued_and_active_then_reconnect(self):
        self.gateway.responses.put_nowait(None)
        first = asyncio.create_task(self.bus.transact(STATION_1, EVENT_REPLY))
        self.assertEqual(await self.next_request(), STATION_1)
        queued = asyncio.create_task(self.bus.transact(STATION_2, EVENT_REPLY))
        await asyncio.sleep(0)
        for task in (queued, first):
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
        self.gateway.responses.put_nowait((ack(2),))
        self.assertEqual((await self.bus.transact(STATION_2, EVENT_REPLY)).source, 2)
        self.assertEqual(await self.next_request(), STATION_2)

    async def test_close_cancels_active_and_rejects_waiters(self):
        self.gateway.responses.put_nowait(None)
        first = asyncio.create_task(self.bus.transact(STATION_1, EVENT_REPLY))
        self.assertEqual(await self.next_request(), STATION_1)
        queued = asyncio.create_task(self.bus.transact(STATION_2, EVENT_REPLY))
        await asyncio.sleep(0)
        await self.bus.close()
        with self.assertRaises(asyncio.CancelledError):
            await first
        with self.assertRaises(BusClosedError):
            await queued
        self.assertTrue(self.gateway.requests.empty())

    async def test_policy_rejects_requests_before_connect(self):
        for request in (
            Frame(3, 0x25), Frame(1, 0x37), Frame(2, 0x2D),
            Frame(1, 0x21, b"\x84"), Frame(1, 0x21, b"\x82"),
            Frame(2, 0x21, b"\x83"), Frame(1, 0x18, b"\x00"),
        ):
            with self.subTest(request=request), self.assertRaises(ValueError):
                await self.bus.transact(request, EVENT_REPLY)
        self.assertTrue(self.gateway.requests.empty())

    def test_invalid_client_settings(self):
        for host, port, stations, timeout in (
            ("example.invalid", 1, {1}, 1),
            ("127.0.0.1", 1, set(), 1),
            ("127.0.0.1", 1, {0}, 1),
            ("127.0.0.1", 0, {1}, 1),
            ("127.0.0.1", 1, {1}, float("inf")),
        ):
            with self.subTest(host=host, port=port, stations=stations, timeout=timeout):
                with self.assertRaises(ValueError):
                    BusClient(host, port, stations, timeout=timeout)
