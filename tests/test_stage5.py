"""Loopback tests for the real Stage 5 worker and HA entities."""

import asyncio
from contextlib import asynccontextmanager

import pytest
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import entity_registry as er

from custom_components.soyal_access.const import DOMAIN
from custom_components.soyal_access.protocol import Frame, decode, encode
from tests.test_ha import next_item, setup_entry

pytestmark = pytest.mark.usefixtures("enable_custom_integrations", "socket_enabled")

POLL = bytes.fromhex("7E 06 02 21 00 00 DC FF")
IO = bytes.fromhex("7E 09 00 03 02 73 10 00 18 85 25")
NACK = encode(Frame(0, 5, b"\x02"))


@asynccontextmanager
async def gateway():
    connections = asyncio.Queue()
    tasks = set()

    async def accept(reader, writer):
        task = asyncio.current_task()
        tasks.add(task)
        requests = asyncio.Queue()
        closed = asyncio.Event()
        connections.put_nowait((writer, requests, closed))
        try:
            while header := await reader.read(1):
                length = await reader.readexactly(1)
                frame = header + length + await reader.readexactly(length[0])
                decoded = decode(frame)
                assert decoded.node == 2
                assert decoded.code == 0x21
                assert decoded.payload in (b"\x00\x00", b"\x82", b"\x83", b"\x84")
                requests.put_nowait((asyncio.get_running_loop().time(), frame))
        finally:
            closed.set()
            writer.close()
            await writer.wait_closed()
            tasks.discard(task)

    async with await asyncio.start_server(accept, "127.0.0.1", 0) as server:
        try:
            yield server.sockets[0].getsockname()[1], connections
        finally:
            active = tuple(tasks)
            for task in active:
                task.cancel()
            await asyncio.gather(*active, return_exceptions=True)


async def send(writer, packet):
    writer.write(packet)
    await writer.drain()


def online_entity(hass, entry):
    entity = er.async_get(hass).async_get_entity_id(
        "binary_sensor", DOMAIN, f"{entry.entry_id}:2:node_online"
    )
    return hass.states.get(entity).state


async def eventually(predicate):
    async with asyncio.timeout(2):
        while not predicate():
            await asyncio.sleep(0.01)


async def validate(hub, writer, requests):
    for _ in range(2):
        assert (await next_item(requests))[1] == POLL
        assert not hub.can_pulse
        await send(writer, IO)
    await eventually(lambda: hub.can_pulse)


async def test_poll_spacing_priority_and_cleanup(hass):
    async with gateway() as (port, connections):
        entry = await setup_entry(hass, port, timeout=1)
        hub = entry.runtime_data
        writer, requests, closed = await next_item(connections)
        assert online_entity(hass, entry) == "off"
        first, frame = await next_item(requests)
        assert frame == POLL
        await send(writer, IO[:4])
        await send(writer, IO[4:])
        await eventually(lambda: online_entity(hass, entry) == "on")
        second, frame = await next_item(requests)
        assert frame == POLL
        assert 0.18 <= second - first < 0.65

        await send(writer, IO)
        await eventually(lambda: hub.can_pulse)
        assert (await next_item(requests))[1] == POLL
        # The pulse waits behind the already-transmitted read. No extra polls build up.
        pulse = asyncio.create_task(hub.pulse())
        await asyncio.sleep(0.43)
        assert requests.empty()
        assert not pulse.done()
        await send(writer, IO)
        _, frame = await next_item(requests)
        assert frame == bytes.fromhex("7E 05 02 21 84 58 FF")
        await send(writer, IO)
        await pulse

        for enabled, expected in (
            (True, "7E 05 02 21 82 5E 03"),
            (False, "7E 05 02 21 83 5F 05"),
        ):
            command = asyncio.create_task(hub.set_hold_open(enabled))
            _, frame = await next_item(requests)
            assert frame == bytes.fromhex(expected)
            await send(writer, IO)
            await command
            assert hub.hold_open is enabled

        _, frame = await next_item(requests)
        assert frame == POLL
        await send(writer, IO)
        assert hub.result == "not_queried"
        assert hub.transport.last_io == IO
        manual = asyncio.create_task(hub.query())
        _, frame = await next_item(requests)
        assert frame == POLL
        await send(writer, IO)
        await manual
        assert hub.result == "success"

        task = hub.poll_task
        assert await hass.config_entries.async_unload(entry.entry_id)
        assert task.done()
        await asyncio.wait_for(closed.wait(), 2)
        assert not hub.transport.connected
        assert requests.empty()
        assert connections.empty()


async def test_three_rejected_polls_then_recovers(hass):
    async with gateway() as (port, connections):
        entry = await setup_entry(hass, port, timeout=0.3)
        writer, requests, _ = await next_item(connections)
        assert (await next_item(requests))[1] == POLL
        await send(writer, IO)
        await eventually(lambda: online_entity(hass, entry) == "on")
        for failure in range(1, 4):
            assert (await next_item(requests))[1] == POLL
            await send(writer, NACK)
            await eventually(
                lambda failure=failure: entry.runtime_data._poll_failures == failure
            )
            assert online_entity(hass, entry) == ("off" if failure == 3 else "on")
            assert entry.runtime_data.transport.connected
        assert (await next_item(requests))[1] == POLL
        await send(writer, IO)
        await eventually(lambda: online_entity(hass, entry) == "on")
        assert await hass.config_entries.async_unload(entry.entry_id)
        assert connections.empty()


@pytest.mark.parametrize(
    "failure", ["timeout", "wrong_source", "idle_data", "disconnect"]
)
async def test_reconnect_isolates_control_and_never_replays(hass, monkeypatch, failure):
    monkeypatch.setattr(
        "custom_components.soyal_access.hub.RECONNECT_DELAYS", (0.12, 0.24)
    )
    async with gateway() as (port, connections):
        entry = await setup_entry(hass, port, timeout=0.1)
        hub = entry.runtime_data
        writer, requests, closed = await next_item(connections)
        await validate(hub, writer, requests)
        if failure == "idle_data":
            await send(writer, IO)
        elif failure == "disconnect":
            writer.close()
        else:
            pulse = asyncio.create_task(hub.pulse())
            assert (await next_item(requests))[1] == bytes.fromhex(
                "7E 05 02 21 84 58 FF"
            )
            if failure == "wrong_source":
                await send(writer, encode(Frame(0, 3, b"\x01\x73\x10\x00\x18")))
            with pytest.raises(HomeAssistantError, match="outcome unconfirmed"):
                await pulse

        await asyncio.wait_for(closed.wait(), 2)
        await eventually(lambda: online_entity(hass, entry) == "off")
        assert requests.empty()
        writer, requests, _ = await next_item(connections)
        assert not hub.can_pulse
        with pytest.raises(TimeoutError):
            await asyncio.wait_for(requests.get(), 0.06)
        with pytest.raises(HomeAssistantError, match="not sent"):
            await hub.pulse()
        await validate(hub, writer, requests)
        assert online_entity(hass, entry) == "on"
        registry = er.async_get(hass)
        for domain, key in (("button", "pulse_open"), ("switch", "hold_open")):
            entity_id = registry.async_get_entity_id(
                domain, DOMAIN, f"{entry.entry_id}:2:{key}"
            )
            assert hass.states.get(entity_id).state != "unavailable"
        assert requests.empty()

        old_task = hub.poll_task
        assert await hass.config_entries.async_reload(entry.entry_id)
        await hass.async_block_till_done()
        writer, requests, _ = await next_item(connections)
        assert old_task.done()
        await validate(entry.runtime_data, writer, requests)
        assert requests.empty()
        assert await hass.config_entries.async_unload(entry.entry_id)


async def test_timed_out_poll_rejects_waiting_control_and_backs_off(hass, monkeypatch):
    # Keep the relationship between the first two production delays.
    monkeypatch.setattr(
        "custom_components.soyal_access.hub.RECONNECT_DELAYS", (0.15, 0.30)
    )
    async with gateway() as (port, connections):
        entry = await setup_entry(hass, port, timeout=0.10)
        hub = entry.runtime_data
        writer, requests, closed = await next_item(connections)
        await validate(hub, writer, requests)
        assert (await next_item(requests))[1] == POLL
        pulse = asyncio.create_task(hub.pulse())
        with pytest.raises(HomeAssistantError, match="not sent"):
            await pulse
        await asyncio.wait_for(closed.wait(), 2)
        assert requests.empty()
        loop = asyncio.get_running_loop()
        _, requests, _ = await next_item(connections)
        assert (await next_item(requests))[1] == POLL
        writer, requests, _ = await next_item(connections)
        assert (await next_item(requests))[1] == POLL
        # An invalid packet on the third connection makes the next attempt wait.
        await send(writer, b"\x00")
        await eventually(lambda: not hub.transport.connected)
        disconnected = loop.time()
        writer, requests, _ = await next_item(connections)
        assert loop.time() - disconnected >= 0.25
        await validate(hub, writer, requests)
        assert not hub.busy
        assert await hass.config_entries.async_unload(entry.entry_id)


async def test_unload_cancels_poll_and_rejects_waiting_pulse(hass):
    async with gateway() as (port, connections):
        entry = await setup_entry(hass, port, timeout=1)
        hub = entry.runtime_data
        writer, requests, closed = await next_item(connections)
        await validate(hub, writer, requests)
        assert (await next_item(requests))[1] == POLL
        pulse = asyncio.create_task(hub.pulse())
        await eventually(lambda: hub.busy)
        task = hub.poll_task
        assert await asyncio.wait_for(
            hass.config_entries.async_unload(entry.entry_id), 2
        )
        with pytest.raises(HomeAssistantError, match="not sent"):
            await pulse
        await asyncio.wait_for(closed.wait(), 2)
        assert task.done()
        assert hub.transport.pending is None
        assert requests.empty()
