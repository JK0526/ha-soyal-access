"""Synthetic FIFO fixtures and localhost integration tests."""

import asyncio

import pytest
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import entity_registry as er

from custom_components.soyal_access.const import DOMAIN
from custom_components.soyal_access.fifo import parse_event
from custom_components.soyal_access.protocol import Frame, ProtocolError, decode, encode
from tests.test_ha import next_item, setup_entry
from tests.test_stage5 import IO, NACK, POLL, eventually, gateway, send, validate

pytestmark = pytest.mark.usefixtures("enable_custom_integrations", "socket_enabled")
FIFO = bytes.fromhex("7E 04 02 25 D8 FF")
ACK = encode(Frame(0, 4, b"\x02"))


def synthetic_event(code=0x0B, station=2, month=11):
    payload = bytes([station, 7, 6, 5, 4, 19, month, 26, 2, 0x12, 0x34])
    return encode(Frame(0, code, payload + bytes(14)))


@pytest.mark.parametrize(
    ("code", "kind"),
    [
        (0x0B, "card_access"),
        (0x10, "egress"),
        (0x1C, "pin_access"),
        (0xFA, "unknown_event"),
    ],
)
def test_synthetic_event_layout(code, kind):
    raw = synthetic_event(code)
    event = parse_event(decode(raw))
    assert event == {
        "event_code": code,
        "event_type": kind,
        "source_node": 2,
        "event_time_local": "2026-11-19T05:06:07",
        "port": 2,
        "user_address": 0x1234,
        "raw_frame_hex": raw.hex(" ").upper(),
    }


def test_invalid_rtc_keeps_raw():
    raw = synthetic_event(month=13)
    event = parse_event(decode(raw))
    assert event["event_time_local"] is None
    assert event["raw_frame_hex"] == raw.hex(" ").upper()


@pytest.mark.parametrize(
    "frame", [decode(synthetic_event(station=1)), Frame(0, 0x10, b"\x02")]
)
def test_parser_rejects_wrong_source_or_shape(frame):
    with pytest.raises(ProtocolError):
        parse_event(frame)


async def test_fifo_entity_priority_and_non_destructive_repeated_read(hass, tmp_path):
    hass.config.config_dir = str(tmp_path)
    async with gateway(allow_fifo=True) as (port, connections):
        entry = await setup_entry(hass, port, timeout=1)
        hub = entry.runtime_data
        writer, requests, _ = await next_item(connections)
        await validate(hub, writer, requests)
        registry = er.async_get(hass)
        button = registry.async_get_entity_id(
            "button", DOMAIN, f"{entry.entry_id}:2:read_fifo_event"
        )
        result = registry.async_get_entity_id(
            "sensor", DOMAIN, f"{entry.entry_id}:2:fifo_read_result"
        )
        assert hass.states.get(result).state == "not_queried"
        assert (
            registry.async_get(button).device_id == registry.async_get(result).device_id
        )

        hold = asyncio.create_task(hub.set_hold_open(True))
        assert (await next_item(requests))[1] == bytes.fromhex("7E 05 02 21 82 5E 03")
        await send(writer, ACK)
        await hold
        assert hub.hold_open is True

        async def press():
            await hass.services.async_call(
                "button", "press", {"entity_id": button}, blocking=True
            )

        assert (await next_item(requests))[1] == POLL
        read = asyncio.create_task(press())
        await eventually(lambda: hub.fifo_pending)
        control = asyncio.create_task(hub.pulse())
        await eventually(lambda: hub.busy)
        await send(writer, IO)
        assert (await next_item(requests))[1] == bytes.fromhex("7E 05 02 21 84 58 FF")
        await send(writer, ACK)
        await control
        assert (await next_item(requests))[1] == FIFO
        with pytest.raises(HomeAssistantError, match="not sent"):
            await hub.read_oldest_event()
        raw = synthetic_event()
        await asyncio.sleep(0.41)
        assert requests.empty()
        for fragment in (raw[:5], raw[5:19]):
            await send(writer, fragment)
            await asyncio.sleep(0.01)
            assert not read.done()
        await send(writer, raw[19:])
        await read
        assert hass.states.get(result).state == "event"
        assert hass.states.get(result).attributes["user_address"] == 0x1234
        assert hass.states.get(result).attributes["read_at"]
        assert hass.states.get(result).attributes["storage_status"] == "saved"
        record_id = hass.states.get(result).attributes["database_record_id"]
        assert hub.unlock_output is False and hub.hold_open is True
        assert hub.node_online and hub.result == "not_queried"

        # Two reads return the same head. The integration publishes no access event.
        observed = []
        unsubscribe = hass.bus.async_listen(
            "*", lambda event: observed.append(event.event_type)
        )
        try:
            for reply, expected in ((raw, "event"), (NACK, "nack"), (ACK, "empty")):
                task = asyncio.create_task(press())
                assert (await next_item(requests))[1] == FIFO
                await send(writer, reply)
                await task
                state = hass.states.get(result)
                assert state.state == expected
                assert ("raw_frame_hex" in state.attributes) == (expected == "event")
                if expected == "event":
                    assert state.attributes["storage_status"] == "already_saved"
                    assert state.attributes["database_record_id"] == record_id
                    assert state.attributes["read_count"] == 2
                else:
                    assert "storage_status" not in state.attributes
                assert (
                    hub.node_online
                    and hub.hold_open is True
                    and hub.unlock_output is False
                )
            await hass.async_block_till_done()
        finally:
            unsubscribe()
        assert set(observed) <= {"call_service", "state_changed", "state_reported"}
        assert (await next_item(requests))[1] == POLL
        await send(writer, IO)
        assert await hass.config_entries.async_reload(entry.entry_id)
        await hass.async_block_till_done()
        assert hass.states.get(result).state == "not_queried"
        writer, requests, _ = await next_item(connections)
        await validate(entry.runtime_data, writer, requests)
        task = asyncio.create_task(press())
        assert (await next_item(requests))[1] == FIFO
        await send(writer, raw)
        await task
        assert hass.states.get(result).attributes["database_record_id"] == record_id
        assert hass.states.get(result).attributes["read_count"] == 3
        assert await hass.config_entries.async_unload(entry.entry_id)


@pytest.mark.parametrize(
    ("reply", "expected"),
    [
        (None, "timeout"),
        (synthetic_event(station=1), "invalid_response"),
        (synthetic_event()[:-1] + b"\x00", "invalid_response"),
        (synthetic_event()[:1] + b"\x1c", "invalid_response"),
        (synthetic_event() + ACK, "invalid_response"),
        (IO, "invalid_response"),
        (b"", "communication_failed"),
    ],
)
async def test_fifo_failure_recovers_without_replaying(
    hass, monkeypatch, tmp_path, reply, expected
):
    hass.config.config_dir = str(tmp_path)
    monkeypatch.setattr("custom_components.soyal_access.hub.RECONNECT_DELAYS", (0.1,))
    async with gateway(allow_fifo=True) as (port, connections):
        entry = await setup_entry(hass, port, timeout=0.25)
        hub = entry.runtime_data
        writer, requests, closed = await next_item(connections)
        await validate(hub, writer, requests)
        task = asyncio.create_task(hub.read_oldest_event())
        assert (await next_item(requests))[1] == FIFO
        if reply == b"":
            writer.close()
        elif reply is not None:
            await send(writer, reply)
        await task
        await asyncio.wait_for(closed.wait(), 2)
        assert hub.fifo_result == expected
        assert not hub.fifo_attributes
        assert not hub.fifo_storage.path.exists()
        assert hub.unlock_output is None and not hub.node_online
        assert requests.empty()
        writer, requests, _ = await next_item(connections)
        await validate(hub, writer, requests)
        assert hub.can_pulse and hub.fifo_result == expected
        assert hub.unlock_output is False
        assert requests.empty()
        assert await hass.config_entries.async_unload(entry.entry_id)
