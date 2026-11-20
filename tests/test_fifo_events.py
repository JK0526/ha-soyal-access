"""Tests for the background FIFO reader sharing the existing Hub transport."""

import asyncio
import sqlite3

import pytest

from custom_components.soyal_access.protocol import Frame, encode
from tests.test_ha import next_item, setup_entry
from tests.test_stage5 import IO, NACK, POLL, eventually, gateway, send, validate

pytestmark = pytest.mark.usefixtures("enable_custom_integrations", "socket_enabled")

FIFO = encode(Frame(2, 0x25))
DELETE = encode(Frame(2, 0x37))
EMPTY = encode(Frame(0, 4, b"\x02"))
STATUS_ACK = bytes.fromhex("7E 0A 00 04 02 34 10 00 18 73 B6 8B")
STATUS_NACK = bytes.fromhex("7E 0A 00 05 02 34 10 00 18 73 B7 8D")


def event_frame():
    payload = bytes([2, 7, 6, 5, 4, 19, 11, 26, 2, 0x12, 0x34]) + bytes(14)
    return encode(Frame(0, 0x0B, payload))


def event_count(hub):
    with sqlite3.connect(hub.fifo_storage.path) as db:
        return db.execute("SELECT COUNT(*) FROM access_events").fetchone()[0]


async def next_fifo_request(requests, writer, packet):
    async with asyncio.timeout(4):
        while True:
            _, current = await requests.get()
            if current == packet:
                return
            if current == POLL:
                await send(writer, IO)
                continue
            if packet == POLL and current == FIFO:
                await send(writer, EMPTY)
                continue
            else:
                raise AssertionError(f"Unexpected request: {current.hex(' ')}")


async def test_event_is_committed_before_delete_and_polling_continues(
    hass, monkeypatch
):
    monkeypatch.setattr("custom_components.soyal_access.hub.FIFO_INTERVAL", 0.9)
    async with gateway(allow_fifo=True, allow_pop=True) as (port, connections):
        entry = await setup_entry(hass, port, timeout=0.5)
        hub = entry.runtime_data
        writer, requests, _ = await next_item(connections)
        await validate(hub, writer, requests)

        await next_fifo_request(requests, writer, FIFO)
        await send(writer, event_frame())
        await eventually(lambda: hub._fifo_delete_waiting_for_poll)
        await next_fifo_request(requests, writer, POLL)
        assert event_count(hub) == 1
        await send(writer, IO)
        await next_fifo_request(requests, writer, DELETE)
        await send(writer, STATUS_ACK)

        await next_fifo_request(requests, writer, POLL)
        await eventually(lambda: hub.node_online)
        assert event_count(hub) == 1
        assert await hass.config_entries.async_unload(entry.entry_id)


@pytest.mark.parametrize("empty_reply", [EMPTY, STATUS_ACK])
async def test_empty_queue_is_normal_and_does_not_delete(
    hass, monkeypatch, empty_reply
):
    monkeypatch.setattr("custom_components.soyal_access.hub.FIFO_INTERVAL", 0.9)
    async with gateway(allow_fifo=True, allow_pop=True) as (port, connections):
        entry = await setup_entry(hass, port, timeout=0.5)
        hub = entry.runtime_data
        writer, requests, _ = await next_item(connections)
        await validate(hub, writer, requests)

        await next_fifo_request(requests, writer, FIFO)
        await send(writer, empty_reply)
        await next_fifo_request(requests, writer, POLL)
        assert event_count(hub) == 0
        assert await hass.config_entries.async_unload(entry.entry_id)


async def test_storage_failure_does_not_send_delete(hass, monkeypatch):
    import sqlite3

    monkeypatch.setattr("custom_components.soyal_access.hub.FIFO_INTERVAL", 0.9)
    async with gateway(allow_fifo=True, allow_pop=True) as (port, connections):
        entry = await setup_entry(hass, port, timeout=0.5)
        hub = entry.runtime_data
        writer, requests, _ = await next_item(connections)
        await validate(hub, writer, requests)

        def fail_save(*_args):
            raise sqlite3.OperationalError("simulated commit failure")

        monkeypatch.setattr(hub.fifo_storage, "save_event", fail_save)
        await next_fifo_request(requests, writer, FIFO)
        await send(writer, event_frame())
        await next_fifo_request(requests, writer, POLL)
        assert event_count(hub) == 0
        assert await hass.config_entries.async_unload(entry.entry_id)


async def test_delete_nack_does_not_count_as_success_or_repeat_delete(
    hass, monkeypatch
):
    monkeypatch.setattr("custom_components.soyal_access.hub.FIFO_INTERVAL", 0.9)
    async with gateway(allow_fifo=True, allow_pop=True) as (port, connections):
        entry = await setup_entry(hass, port, timeout=0.5)
        hub = entry.runtime_data
        writer, requests, _ = await next_item(connections)
        await validate(hub, writer, requests)

        await next_fifo_request(requests, writer, FIFO)
        await send(writer, event_frame())
        await next_fifo_request(requests, writer, DELETE)
        await send(writer, STATUS_NACK)
        await next_fifo_request(requests, writer, POLL)
        assert not hub._fifo_delete_pending
        assert await hass.config_entries.async_unload(entry.entry_id)


async def test_poll_nack_keeps_delete_waiting_for_valid_poll(hass, monkeypatch):
    monkeypatch.setattr("custom_components.soyal_access.hub.FIFO_INTERVAL", 0.9)
    async with gateway(allow_fifo=True, allow_pop=True) as (port, connections):
        entry = await setup_entry(hass, port, timeout=0.5)
        hub = entry.runtime_data
        writer, requests, _ = await next_item(connections)
        await validate(hub, writer, requests)

        await next_fifo_request(requests, writer, FIFO)
        await send(writer, event_frame())
        await eventually(lambda: hub._fifo_delete_waiting_for_poll)
        await next_fifo_request(requests, writer, POLL)
        await send(writer, NACK)
        await eventually(lambda: hub._fifo_delete_waiting_for_poll)
        assert hub._fifo_delete_pending
        await next_fifo_request(requests, writer, POLL)
        assert hub._fifo_delete_waiting_for_poll
        await send(writer, IO)
        await next_fifo_request(requests, writer, DELETE)
        await send(writer, STATUS_ACK)
        assert await hass.config_entries.async_unload(entry.entry_id)


async def test_poll_timeout_reconnects_and_validates_before_delete(hass, monkeypatch):
    monkeypatch.setattr("custom_components.soyal_access.hub.FIFO_INTERVAL", 0.9)
    monkeypatch.setattr("custom_components.soyal_access.hub.RECONNECT_DELAYS", (0.1,))
    async with gateway(allow_fifo=True, allow_pop=True) as (port, connections):
        entry = await setup_entry(hass, port, timeout=0.25)
        hub = entry.runtime_data
        writer, requests, closed = await next_item(connections)
        await validate(hub, writer, requests)

        await next_fifo_request(requests, writer, FIFO)
        await send(writer, event_frame())
        await eventually(lambda: hub._fifo_delete_waiting_for_poll)
        await next_fifo_request(requests, writer, POLL)
        await asyncio.wait_for(closed.wait(), 2)
        assert hub._fifo_delete_pending
        assert hub._fifo_delete_waiting_for_poll

        new_writer, new_requests, _ = await next_item(connections)
        await validate(hub, new_writer, new_requests)
        await next_fifo_request(new_requests, new_writer, DELETE)
        await send(new_writer, STATUS_ACK)
        assert await hass.config_entries.async_unload(entry.entry_id)


async def test_delete_timeout_reconnects_and_does_not_replay_37(hass, monkeypatch):
    monkeypatch.setattr("custom_components.soyal_access.hub.FIFO_INTERVAL", 0.9)
    monkeypatch.setattr("custom_components.soyal_access.hub.RECONNECT_DELAYS", (0.1,))
    async with gateway(allow_fifo=True, allow_pop=True) as (port, connections):
        entry = await setup_entry(hass, port, timeout=0.25)
        hub = entry.runtime_data
        writer, requests, closed = await next_item(connections)
        await validate(hub, writer, requests)

        await next_fifo_request(requests, writer, FIFO)
        await send(writer, event_frame())
        await next_fifo_request(requests, writer, DELETE)
        await asyncio.wait_for(closed.wait(), 2)

        new_writer, new_requests, _ = await next_item(connections)
        await validate(hub, new_writer, new_requests)
        await next_fifo_request(new_requests, new_writer, FIFO)
        await send(new_writer, EMPTY)
        await next_fifo_request(new_requests, new_writer, POLL)
        assert event_count(hub) == 1
        assert await hass.config_entries.async_unload(entry.entry_id)


async def test_invalid_extended_empty_reply_reconnects_without_storage(
    hass, monkeypatch
):
    monkeypatch.setattr("custom_components.soyal_access.hub.FIFO_INTERVAL", 0.9)
    monkeypatch.setattr("custom_components.soyal_access.hub.RECONNECT_DELAYS", (0.1,))
    async with gateway(allow_fifo=True, allow_pop=True) as (port, connections):
        entry = await setup_entry(hass, port, timeout=0.25)
        hub = entry.runtime_data
        writer, requests, closed = await next_item(connections)
        await validate(hub, writer, requests)

        await next_fifo_request(requests, writer, FIFO)
        invalid_ack = encode(Frame(0, 4, bytes.fromhex("02 34 10 00 18 72")))
        await send(writer, invalid_ack)
        await asyncio.wait_for(closed.wait(), 2)
        new_writer, new_requests, _ = await next_item(connections)
        await validate(hub, new_writer, new_requests)
        assert event_count(hub) == 0
        assert await hass.config_entries.async_unload(entry.entry_id)


async def test_control_takes_priority_over_saved_event_delete(hass, monkeypatch):
    monkeypatch.setattr("custom_components.soyal_access.hub.FIFO_INTERVAL", 0.9)
    async with gateway(allow_fifo=True, allow_pop=True) as (port, connections):
        entry = await setup_entry(hass, port, timeout=0.5)
        hub = entry.runtime_data
        writer, requests, _ = await next_item(connections)
        await validate(hub, writer, requests)

        await next_fifo_request(requests, writer, FIFO)
        await send(writer, event_frame())
        await eventually(lambda: hub._fifo_delete_pending)
        pulse = asyncio.create_task(hub.pulse())
        await eventually(lambda: hub.busy)
        await next_fifo_request(requests, writer, bytes.fromhex("7E 05 02 21 84 58 FF"))
        await send(writer, EMPTY)
        await pulse
        await next_fifo_request(requests, writer, DELETE)
        await send(writer, STATUS_ACK)
        assert event_count(hub) == 1
        assert await hass.config_entries.async_unload(entry.entry_id)
