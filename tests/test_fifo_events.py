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
STATUS_ACK_UNLOCKED = bytes.fromhex("7E 0A 00 04 02 34 50 00 18 73 F6 0B")
STATUS_NACK = bytes.fromhex("7E 0A 00 05 02 34 10 00 18 73 B7 8D")


def event_frame(node_id=2):
    payload = bytes([node_id, 7, 6, 5, 4, 19, 11, 26, 2, 0x12, 0x34]) + bytes(14)
    return encode(Frame(0, 0x0B, payload))


def event_count(hub):
    with sqlite3.connect(hub.fifo_storage.path) as db:
        return db.execute("SELECT COUNT(*) FROM access_events").fetchone()[0]


async def next_fifo_request(requests, writer, packet, *, poll=POLL, io=IO):
    async with asyncio.timeout(4):
        while True:
            _, current = await requests.get()
            if current == packet:
                return
            if current == poll:
                await send(writer, io)
                continue
            if packet == POLL and current == FIFO:
                await send(writer, EMPTY)
                continue
            else:
                raise AssertionError(f"Unexpected request: {current.hex(' ')}")


@pytest.mark.parametrize(
    ("node_id", "io", "event", "ack"),
    [
        pytest.param(2, IO, event_frame(), STATUS_ACK, id="node02"),
        pytest.param(
            1,
            bytes.fromhex("7E 09 00 03 01 74 10 00 18 81 21"),
            bytes.fromhex(
                "7E 1D 00 0B 01 11 1B 10 04 05 08 1A 01 00 14 00 "
                "00 18 00 21 A3 01 80 F0 60 92 19 4E 58 FF 85"
            ),
            bytes.fromhex("7E 0A 00 04 01 34 10 00 18 74 B2 87"),
            id="node01-field-capture",
        ),
    ],
)
async def test_event_is_committed_before_delete_and_polling_continues(
    hass, monkeypatch, node_id, io, event, ack
):
    monkeypatch.setattr("custom_components.soyal_access.hub.FIFO_INTERVAL", 0.9)
    poll = encode(Frame(node_id, 0x21, b"\x00\x00"))
    async with gateway(allow_fifo=True, allow_pop=True, node_id=node_id) as (
        port,
        connections,
    ):
        entry = await setup_entry(hass, port, timeout=0.5, node_id=node_id)
        hub = entry.runtime_data
        writer, requests, closed = await next_item(connections)
        session_id = hub.transport.session_id
        for _ in range(2):
            assert (await next_item(requests))[1] == poll
            await send(writer, io)
        await eventually(lambda: hub.can_pulse)

        await next_fifo_request(
            requests, writer, encode(Frame(node_id, 0x25)), poll=poll, io=io
        )
        await send(writer, event)
        await eventually(lambda: hub._fifo_delete_waiting_for_poll)
        await next_fifo_request(requests, writer, poll, poll=poll, io=io)
        count, events = await hass.async_add_executor_job(
            hub.fifo_storage.read_history, node_id
        )
        assert count == 1
        assert (events[0]["node_id"], events[0]["event_type"]) == (
            node_id,
            "card_access",
        )
        await send(writer, io)
        await next_fifo_request(
            requests, writer, encode(Frame(node_id, 0x37)), poll=poll, io=io
        )
        await send(writer, ack)

        await next_fifo_request(requests, writer, poll, poll=poll, io=io)
        await send(writer, io)
        assert not closed.is_set()
        assert hub.transport.connected and hub.transport.session_id == session_id
        assert not hub.transport.tainted
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


async def test_unlocked_0x25_status_ack_keeps_tcp_session(hass, monkeypatch):
    monkeypatch.setattr("custom_components.soyal_access.hub.FIFO_INTERVAL", 0.9)
    async with gateway(allow_fifo=True, allow_pop=True) as (port, connections):
        entry = await setup_entry(hass, port, timeout=0.5)
        hub = entry.runtime_data
        writer, requests, closed = await next_item(connections)
        await validate(hub, writer, requests)
        session_id = hub.transport.session_id

        await next_fifo_request(requests, writer, FIFO)
        await send(writer, STATUS_ACK_UNLOCKED)
        await next_fifo_request(requests, writer, POLL)
        await send(writer, bytes.fromhex("7E 09 00 03 02 73 50 00 18 C5 A5"))
        await eventually(lambda: hub.unlock_output is True)
        assert hub.transport.connected
        assert hub.transport.session_id == session_id
        assert not closed.is_set()
        assert event_count(hub) == 0
        assert await hass.config_entries.async_unload(entry.entry_id)


async def test_unlocked_0x37_status_ack_keeps_tcp_session(hass, monkeypatch):
    monkeypatch.setattr("custom_components.soyal_access.hub.FIFO_INTERVAL", 0.9)
    async with gateway(allow_fifo=True, allow_pop=True) as (port, connections):
        entry = await setup_entry(hass, port, timeout=0.5)
        hub = entry.runtime_data
        writer, requests, closed = await next_item(connections)
        await validate(hub, writer, requests)
        session_id = hub.transport.session_id

        await next_fifo_request(requests, writer, FIFO)
        await send(writer, event_frame())
        await eventually(lambda: hub._fifo_delete_waiting_for_poll)
        await next_fifo_request(requests, writer, POLL)
        await send(writer, IO)
        await next_fifo_request(requests, writer, DELETE)
        assert event_count(hub) == 1
        await send(writer, STATUS_ACK_UNLOCKED)
        await next_fifo_request(requests, writer, POLL)
        await send(writer, bytes.fromhex("7E 09 00 03 02 73 50 00 18 C5 A5"))
        await eventually(lambda: hub.unlock_output is True)
        assert hub.transport.connected
        assert hub.transport.session_id == session_id
        assert not closed.is_set()
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
        invalid_ack = encode(Frame(0, 4, bytes.fromhex("02 35 10 00 18 73")))
        await send(writer, invalid_ack)
        await asyncio.wait_for(closed.wait(), 2)
        new_writer, new_requests, _ = await next_item(connections)
        await validate(hub, new_writer, new_requests)
        assert event_count(hub) == 0
        assert await hass.config_entries.async_unload(entry.entry_id)


@pytest.mark.parametrize(
    "invalid_ack",
    [
        pytest.param(
            encode(Frame(0, 4, bytes.fromhex("02 35 50 00 18 73"))), id="fixed-field"
        ),
        pytest.param(
            bytes.fromhex("7E 0A 00 04 01 34 10 00 18 74 B2 87"), id="wrong-source"
        ),
        pytest.param(STATUS_ACK[:-1] + b"\x8a", id="bad-checksum"),
    ],
)
async def test_0x37_rejects_invalid_status_ack(hass, monkeypatch, invalid_ack):
    monkeypatch.setattr("custom_components.soyal_access.hub.FIFO_INTERVAL", 0.9)
    async with gateway(allow_fifo=True, allow_pop=True) as (port, connections):
        entry = await setup_entry(hass, port, timeout=0.25)
        hub = entry.runtime_data
        writer, requests, closed = await next_item(connections)
        await validate(hub, writer, requests)

        await next_fifo_request(requests, writer, FIFO)
        await send(writer, event_frame())
        await eventually(lambda: hub._fifo_delete_waiting_for_poll)
        await next_fifo_request(requests, writer, POLL)
        await send(writer, IO)
        await next_fifo_request(requests, writer, DELETE)
        await send(writer, invalid_ack)
        await asyncio.wait_for(closed.wait(), 2)
        assert event_count(hub) == 1
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
        assert event_count(hub) == 2  # One FIFO observation and one accepted HA pulse.
        assert await hass.config_entries.async_unload(entry.entry_id)


@pytest.mark.parametrize(
    ("payload", "accepted"),
    [
        ("02 34 10 00 18 73", True),  # locked
        ("02 34 50 00 18 73", True),  # unlock output on
        ("02 34 12 00 18 73", True),  # other I/O bit set
        ("02 34 52 00 18 73", True),  # unlocked and other I/O bit set
        ("02 34 50 00 18 74", True),  # both variable bytes changed
        ("02 35 10 00 18 73", False),
        ("03 34 10 00 18 73", False),
        ("02 34 10 00 18", False),  # wrong length
    ],
)
def test_status_payload_mask_ignores_io_and_last_payload_byte(payload, accepted):
    from custom_components.soyal_access import transport

    assert (
        transport.Transport._matches_payload_mask(
            bytes.fromhex(payload),
            transport.Transport("", 0, 1, lambda: None, node_id=2)._fifo_status,
            transport._FIFO_STATUS_MASK,
        )
        is accepted
    )


async def test_storage_init_failure_disables_fifo_but_keeps_polling(hass, monkeypatch):
    monkeypatch.setattr("custom_components.soyal_access.hub.FIFO_INTERVAL", 0.3)

    def fail_initialize(_self):
        raise OSError("read-only config directory")

    monkeypatch.setattr(
        "custom_components.soyal_access.fifo_storage.FifoStorage.initialize",
        fail_initialize,
    )
    async with gateway(allow_fifo=True, allow_pop=True) as (port, connections):
        entry = await setup_entry(hass, port, timeout=0.5)
        hub = entry.runtime_data
        assert not hub.fifo_enabled
        writer, requests, _ = await next_item(connections)
        await validate(hub, writer, requests)
        try:
            async with asyncio.timeout(1.5):
                while True:
                    _, current = await requests.get()
                    assert current != FIFO
                    if current == POLL:
                        await send(writer, IO)
        except TimeoutError:
            pass
        assert hub.can_query
        assert await hass.config_entries.async_unload(entry.entry_id)


async def test_node05_poll_fifo_control_history_and_wrong_source(hass, monkeypatch):
    from homeassistant.exceptions import HomeAssistantError
    from homeassistant.helpers import entity_registry as er

    from custom_components.soyal_access.const import DOMAIN

    monkeypatch.setattr("custom_components.soyal_access.hub.FIFO_INTERVAL", 0.9)
    poll, read, delete, pulse = (
        encode(Frame(5, code, payload))
        for code, payload in (
            (0x21, b"\x00\x00"),
            (0x25, b""),
            (0x37, b""),
            (0x21, b"\x84"),
        )
    )
    io = encode(Frame(0, 3, bytes.fromhex("05 73 10 00 18")))
    ack = encode(Frame(0, 4, b"\x05"))
    status_ack = encode(Frame(0, 4, bytes.fromhex("05 34 50 00 18 73")))
    async with gateway(allow_fifo=True, allow_pop=True, node_id=5) as (
        port,
        connections,
    ):
        entry = await setup_entry(hass, port, timeout=0.5, node_id=5)
        hub = entry.runtime_data
        writer, requests, _ = await next_item(connections)
        for _ in range(2):
            assert (await next_item(requests))[1] == poll
            await send(writer, io)
        await eventually(lambda: hub.can_pulse)

        async def await_request(expected):
            async with asyncio.timeout(4):
                while (packet := (await requests.get())[1]) != expected:
                    assert packet == poll
                    await send(writer, io)

        await await_request(read)
        await send(writer, event_frame(5))
        await eventually(lambda: hub._fifo_delete_waiting_for_poll)
        assert (await next_item(requests))[1] == poll
        await send(writer, io)
        await await_request(delete)
        await send(writer, status_ack)
        control = asyncio.create_task(hub.pulse())
        await await_request(pulse)
        await send(writer, ack)
        await control
        sensor = er.async_get(hass).async_get_entity_id(
            "sensor", DOMAIN, f"{entry.entry_id}:5:access_event_history"
        )
        await eventually(lambda: hass.states.get(sensor).state == "2")
        rows = hass.states.get(sensor).attributes["events"]
        assert [(row["node_id"], row["source"]) for row in rows] == [
            (5, "ha"),
            (5, "soyal"),
        ]

        control = asyncio.create_task(hub.pulse())
        await await_request(pulse)
        await send(writer, EMPTY)  # Valid ACK framing, but the source is Node 02.
        with pytest.raises(HomeAssistantError, match="outcome unconfirmed"):
            await control
        assert hub.transport.tainted
        assert event_count(hub) == 2
        assert await hass.config_entries.async_unload(entry.entry_id)
