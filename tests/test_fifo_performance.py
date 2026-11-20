"""Loopback timing samples for the 200 ms poll under FIFO traffic."""

import asyncio
import math
import statistics
import time
from itertools import pairwise

import pytest

from custom_components.soyal_access.protocol import Frame, encode
from tests.test_fifo_events import DELETE, EMPTY, STATUS_ACK, event_frame
from tests.test_ha import next_item, setup_entry
from tests.test_stage5 import IO, POLL, eventually, gateway, send, validate

pytestmark = pytest.mark.usefixtures("enable_custom_integrations", "socket_enabled")
FIFO = encode(Frame(2, 0x25))


def summarize(intervals):
    ordered = sorted(intervals)
    p95 = ordered[max(0, math.ceil(0.95 * len(ordered)) - 1)]
    return {
        "avg_ms": round(statistics.mean(intervals) * 1000, 2),
        "max_ms": round(max(intervals) * 1000, 2),
        "p95_ms": round(p95 * 1000, 2),
    }


@pytest.mark.parametrize("scenario", ["no_fifo", "empty", "event"])
async def test_loopback_poll_timing(hass, monkeypatch, scenario):
    fifo_interval = 100 if scenario == "no_fifo" else 2.0
    monkeypatch.setattr(
        "custom_components.soyal_access.hub.FIFO_INTERVAL", fifo_interval
    )
    async with gateway(allow_fifo=True, allow_pop=True) as (port, connections):
        entry = await setup_entry(hass, port, timeout=0.5)
        hub = entry.runtime_data
        writer, requests, _ = await next_item(connections)
        await validate(hub, writer, requests)

        poll_times = []
        fifo_durations = []
        read_fifo = hub.transport.read_fifo_event
        delete_fifo = hub.transport.delete_fifo_event

        async def measured_read():
            started = time.monotonic()
            try:
                return await read_fifo()
            finally:
                fifo_durations.append(time.monotonic() - started)

        async def measured_delete():
            started = time.monotonic()
            try:
                return await delete_fifo()
            finally:
                fifo_durations.append(time.monotonic() - started)

        monkeypatch.setattr(hub.transport, "read_fifo_event", measured_read)
        monkeypatch.setattr(hub.transport, "delete_fifo_event", measured_delete)
        event_sent = False
        target_polls = 14
        async with asyncio.timeout(5):
            while len(poll_times) < target_polls:
                timestamp, packet = await requests.get()
                if packet == POLL:
                    poll_times.append(timestamp)
                    await send(writer, IO)
                elif packet == FIFO:
                    if scenario == "event" and not event_sent:
                        event_sent = True
                        await send(writer, event_frame())
                    else:
                        await send(writer, EMPTY)
                elif packet == DELETE:
                    await send(writer, STATUS_ACK)
                else:
                    raise AssertionError(f"Unexpected request: {packet.hex(' ')}")

        intervals = [right - left for left, right in pairwise(poll_times)]
        print(
            f"loopback scenario={scenario} polls={summarize(intervals)} "
            f"fifo_transaction_ms={[round(value * 1000, 2) for value in fifo_durations]}"
        )
        assert await hass.config_entries.async_unload(entry.entry_id)


async def test_loopback_fifo_timeout_recovery_timing(hass, monkeypatch):
    monkeypatch.setattr("custom_components.soyal_access.hub.FIFO_INTERVAL", 2.0)
    monkeypatch.setattr("custom_components.soyal_access.hub.RECONNECT_DELAYS", (0.1,))
    async with gateway(allow_fifo=True, allow_pop=True) as (port, connections):
        entry = await setup_entry(hass, port, timeout=0.25)
        hub = entry.runtime_data
        writer, requests, closed = await next_item(connections)
        await validate(hub, writer, requests)

        async with asyncio.timeout(4):
            while True:
                _, packet = await requests.get()
                if packet == FIFO:
                    await send(writer, event_frame())
                elif packet == DELETE:
                    break
                elif packet == POLL:
                    await send(writer, IO)
        await asyncio.wait_for(closed.wait(), 2)
        disconnected_at = asyncio.get_running_loop().time()
        monkeypatch.setattr("custom_components.soyal_access.hub.FIFO_INTERVAL", 100)

        new_writer, new_requests, _ = await next_item(connections)
        first_at, packet = await next_item(new_requests)
        assert packet == POLL
        await send(new_writer, IO)
        second_at, packet = await next_item(new_requests)
        assert packet == POLL
        await send(new_writer, IO)
        poll_times = [first_at, second_at]
        async with asyncio.timeout(4):
            while len(poll_times) < 7:
                timestamp, packet = await new_requests.get()
                assert packet == POLL
                poll_times.append(timestamp)
                await send(new_writer, IO)
        await eventually(lambda: hub.can_pulse)
        recovery_intervals = [right - left for left, right in pairwise(poll_times)]
        print(
            "loopback scenario=fifo_timeout "
            f"first_poll_after_disconnect_ms={round((first_at - disconnected_at) * 1000, 2)} "
            f"recovery_polls={summarize(recovery_intervals)}"
        )
        assert await hass.config_entries.async_unload(entry.entry_id)
