"""Test the integration lifecycle against a passive loopback gateway."""

import asyncio
import sqlite3
from contextlib import asynccontextmanager
from hashlib import sha256

import pytest
from homeassistant.config_entries import SOURCE_RECONFIGURE, SOURCE_USER
from homeassistant.const import EVENT_HOMEASSISTANT_STOP, STATE_OFF, STATE_ON
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.soyal_access.const import DOMAIN

pytestmark = pytest.mark.usefixtures("enable_custom_integrations", "socket_enabled")


@pytest.fixture(autouse=True)
def manual_operations_only(monkeypatch):
    """Keep the original packet-by-packet tests focused on manual operations."""
    from custom_components.soyal_access.hub import Hub

    def skip_validation(hub):
        hub._validation_polls = 2
        hub.notify()

    monkeypatch.setattr(Hub, "start_polling", skip_validation)


@asynccontextmanager
async def gateway():
    connections = asyncio.Queue()
    received = asyncio.Queue()

    async def accept(reader, writer):
        connections.put_nowait(writer)
        try:
            received.put_nowait(await reader.read(4096))
        finally:
            writer.close()
            await writer.wait_closed()

    async with await asyncio.start_server(accept, "127.0.0.1", 0) as server:
        yield server.sockets[0].getsockname()[1], connections, received


async def next_item(queue):
    return await asyncio.wait_for(queue.get(), 2)


async def setup_entry(hass, port, **legacy):
    entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id=f"[127.0.0.1]:{port}",
        data={"host": "127.0.0.1", "port": port, **legacy},
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    return entry


def tcp_id(hass, entry):
    return er.async_get(hass).async_get_entity_id(
        "binary_sensor", DOMAIN, f"{entry.entry_id}:gateway:tcp_connected"
    )


async def test_config_flow_and_duplicate(hass):
    async with gateway() as (port, connections, received):

        async def form():
            return await hass.config_entries.flow.async_init(
                DOMAIN, context={"source": SOURCE_USER}
            )

        result = await form()
        assert result["type"] == FlowResultType.FORM
        assert {key.schema for key in result["data_schema"].schema} == {"host", "port"}
        assert connections.empty()
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {"host": "invalid host", "port": port}
        )
        assert result["errors"] == {"host": "invalid_host"}
        data = {"host": "127.0.0.1", "port": port}
        result = await hass.config_entries.flow.async_configure(result["flow_id"], data)
        assert result["type"] == FlowResultType.CREATE_ENTRY
        await hass.async_block_till_done()
        entry = result["result"]
        await next_item(connections)
        assert hass.states.get(tcp_id(hass, entry)).state == STATE_ON
        duplicate = await form()
        duplicate = await hass.config_entries.flow.async_configure(
            duplicate["flow_id"], data
        )
        assert duplicate["reason"] == "already_configured"
        assert connections.empty()
        assert await hass.config_entries.async_unload(entry.entry_id)
        assert await next_item(received) == b""


async def test_tcp_failure_is_off(hass):
    async with await asyncio.start_server(lambda *_: None, "127.0.0.1", 0) as server:
        port = server.sockets[0].getsockname()[1]
    entry = await setup_entry(hass, port)
    assert hass.states.get(tcp_id(hass, entry)).state == STATE_OFF
    assert entry.runtime_data.transport._receiver is None


async def test_remote_eof_reload_unload_shutdown(hass):
    async with gateway() as (port, connections, received):
        entry = await setup_entry(hass, port)
        entity_id = tcp_id(hass, entry)
        transport = entry.runtime_data.transport
        peer = await next_item(connections)
        task = transport._receiver
        await asyncio.gather(transport.start(), transport.start())
        assert connections.empty()
        assert transport._receiver is task
        assert received.empty()
        peer.write(b"unsolicited TCP data")
        await peer.drain()
        peer.close()
        await peer.wait_closed()
        await asyncio.wait_for(task, 2)
        assert await next_item(received) == b""
        await hass.async_block_till_done()
        assert hass.states.get(entity_id).state == STATE_OFF
        assert connections.empty()

        assert await hass.config_entries.async_reload(entry.entry_id)
        await hass.async_block_till_done()
        assert transport._receiver is None
        await next_item(connections)
        assert tcp_id(hass, entry) == entity_id
        assert hass.states.get(entity_id).state == STATE_ON

        transport = entry.runtime_data.transport
        task = transport._receiver
        assert await hass.config_entries.async_reload(entry.entry_id)
        await hass.async_block_till_done()
        assert await next_item(received) == b""
        await next_item(connections)
        assert task.done() and transport._receiver is None

        transport = entry.runtime_data.transport
        task = transport._receiver
        assert await hass.config_entries.async_unload(entry.entry_id)
        assert await next_item(received) == b""
        assert task.done() and transport._receiver is None
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
        await next_item(connections)

        transport = entry.runtime_data.transport
        task = transport._receiver
        hass.bus.async_fire(EVENT_HOMEASSISTANT_STOP)
        await hass.async_block_till_done()
        assert await next_item(received) == b""
        assert task.done() and transport._receiver is None
        assert connections.empty()


async def test_legacy_entry_registry_and_disk_are_preserved(hass, tmp_path):
    hass.config.config_dir = str(tmp_path)
    async with gateway() as (port, connections, received):
        data = {
            "host": "127.0.0.1",
            "port": port,
            "mode": "raw_tcp",
            "stations": "1,2",
            "timeout": 2.0,
            "reconnect_delay": 1.0,
            "enable_control": True,
            "node02_event_preview": True,
        }
        entry = MockConfigEntry(
            domain=DOMAIN, unique_id=f"[127.0.0.1]:{port}", data=data
        )
        entry.add_to_hass(hass)
        devices = dr.async_get(hass)
        entities = er.async_get(hass)
        old_gateway = devices.async_get_or_create(
            config_entry_id=entry.entry_id, identifiers={(DOMAIN, entry.entry_id)}
        )
        node = devices.async_get_or_create(
            config_entry_id=entry.entry_id,
            identifiers={(DOMAIN, f"{entry.entry_id}:2")},
        )
        old_tcp = entities.async_get_or_create(
            "binary_sensor",
            DOMAIN,
            f"{entry.entry_id}:gateway:tcp_connected",
            config_entry=entry,
            device_id=old_gateway.id,
            suggested_object_id="old_gateway",
        )
        old_control = entities.async_get_or_create(
            "button",
            DOMAIN,
            f"{entry.entry_id}:2:pulse_open",
            config_entry=entry,
            device_id=node.id,
            suggested_object_id="old_control",
        )
        old_hold = entities.async_get_or_create(
            "switch",
            DOMAIN,
            f"{entry.entry_id}:2:hold_open",
            config_entry=entry,
            device_id=node.id,
            suggested_object_id="old_hold",
        )
        storage = tmp_path / ".storage"
        storage.mkdir(exist_ok=True)
        key = sha256(repr((data["host"], port)).encode()).hexdigest()
        uncertain = storage / f"soyal_access.{key}"
        uncertain.write_text(
            '{"version":1,"data":{"uncertain":[2],"kind":{"2":"legacy_unknown"}}}'
        )
        db = tmp_path / "soyal_access" / f"{entry.entry_id}.sqlite3"
        db.parent.mkdir()
        with sqlite3.connect(db) as conn:
            conn.execute("CREATE TABLE access_events (id INTEGER PRIMARY KEY)")
            conn.execute("INSERT INTO access_events VALUES (1)")
        original = {p: p.read_bytes() for p in (db, uncertain)}

        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
        await next_item(connections)
        assert dict(entry.data) == data
        assert tcp_id(hass, entry) == old_tcp.entity_id
        assert entities.async_get(old_tcp.entity_id).device_id == old_gateway.id
        assert (
            entities.async_get_entity_id(
                "button", DOMAIN, f"{entry.entry_id}:2:pulse_open"
            )
            == old_control.entity_id
        )
        assert (
            entities.async_get_entity_id(
                "switch", DOMAIN, f"{entry.entry_id}:2:hold_open"
            )
            == old_hold.entity_id
        )
        assert hass.states.get(old_hold.entity_id).state == "unknown"
        state = hass.states.get(old_control.entity_id)
        assert state is not None and state.state != "unavailable"

        flow = await hass.config_entries.flow.async_init(
            DOMAIN, context={"source": SOURCE_RECONFIGURE, "entry_id": entry.entry_id}
        )
        flow = await hass.config_entries.flow.async_configure(
            flow["flow_id"], {"host": "127.0.0.1", "port": port}
        )
        assert flow["reason"] == "reconfigure_successful"
        await hass.async_block_till_done()
        assert dict(entry.data) == data
        assert await next_item(received) == b""
        await next_item(connections)
        assert tcp_id(hass, entry) == old_tcp.entity_id
        assert await hass.config_entries.async_unload(entry.entry_id)
        assert await next_item(received) == b""
        assert {p: p.read_bytes() for p in original} == original


@pytest.mark.parametrize(
    ("domain", "key", "action", "tx"),
    [
        ("button", "manual_query", "press", "7E 06 02 21 00 00 DC FF"),
        ("button", "pulse_open", "press", "7E 05 02 21 84 58 FF"),
        ("switch", "hold_open", "turn_on", "7E 05 02 21 82 5E 03"),
        ("switch", "hold_open", "turn_off", "7E 05 02 21 83 5F 05"),
    ],
)
@pytest.mark.parametrize(
    ("response", "expected"),
    [
        ("valid", "success"),
        ("ack", "success"),
        ("nack", "rejected"),
        ("source", "invalid_response"),
        ("checksum", "invalid_response"),
        ("coalesced", "invalid_response"),
        ("header", "invalid_response"),
        ("length", "invalid_response"),
        ("code", "invalid_response"),
        ("truncated", "invalid_response"),
        ("timeout", "timeout"),
        ("disconnect", "communication_failed"),
    ],
)
async def test_node02_operations(hass, response, expected, domain, key, action, tx):
    from custom_components.soyal_access.protocol import Frame, encode

    control = key != "manual_query"
    if not control and response in {"ack", "nack"}:
        expected = "invalid_response"
    requests = asyncio.Queue()
    peers = asyncio.Queue()

    async def peer(reader, writer):
        try:
            peers.put_nowait(writer)
            while payload := await reader.read(4096):
                requests.put_nowait(payload)
        finally:
            writer.close()
            await writer.wait_closed()

    async with await asyncio.start_server(peer, "127.0.0.1", 0) as server:
        port = server.sockets[0].getsockname()[1]
        entry = await setup_entry(hass, port, timeout=0.3)
        hub = entry.runtime_data
        writer = await next_item(peers)
        registry = er.async_get(hass)
        button = registry.async_get_entity_id(
            "button", DOMAIN, f"{entry.entry_id}:2:manual_query"
        )
        sensor = registry.async_get_entity_id(
            "sensor", DOMAIN, f"{entry.entry_id}:2:query_result"
        )
        assert hass.states.get(button).state != "unavailable"
        assert hass.states.get(sensor).state == "not_queried"
        pulse_button = registry.async_get_entity_id(
            "button", DOMAIN, f"{entry.entry_id}:2:pulse_open"
        )
        assert hass.states.get(pulse_button).state != "unavailable"
        target = registry.async_get_entity_id(
            domain, DOMAIN, f"{entry.entry_id}:2:{key}"
        )
        hold_switch = registry.async_get_entity_id(
            "switch", DOMAIN, f"{entry.entry_id}:2:hold_open"
        )
        initial = hass.states.get(hold_switch)
        assert initial.state == "unknown"
        assert initial.attributes["assumed_state"] is True
        assert requests.empty()

        async def perform():
            await hass.services.async_call(
                domain, action, {"entity_id": target}, blocking=True
            )

        raw_ack = encode(Frame(0, 4, b"\x02"))
        # Establish the opposite command before failure cases to verify rejection
        # retains the last success while an ambiguous outcome clears it.
        enabled = action == "turn_on"
        if domain == "switch" and expected != "success":
            prime = asyncio.create_task(hub.set_hold_open(not enabled))
            opposite = "7E 05 02 21 83 5F 05" if enabled else "7E 05 02 21 82 5E 03"
            assert await next_item(requests) == bytes.fromhex(opposite)
            writer.write(raw_ack)
            await prime
            assert hub.hold_open is not enabled

        task = asyncio.create_task(perform())
        assert await next_item(requests) == bytes.fromhex(tx)
        for operation in (
            hub.query,
            hub.pulse,
            lambda: hub.set_hold_open(True),
            lambda: hub.set_hold_open(False),
        ):
            with pytest.raises(HomeAssistantError, match="not sent"):
                await operation()
        assert hass.states.get(target).state != "unavailable"
        raw = encode(Frame(0, 3, b"\x02\x73\x10\x00\x18"))
        match response:
            case "valid":
                writer.write(raw[:3])
                await writer.drain()
                await asyncio.sleep(0.01)
                writer.write(raw[3:])
            case "ack" | "nack":
                writer.write(encode(Frame(0, 4 if response == "ack" else 5, b"\x02")))
            case "source":
                writer.write(encode(Frame(0, 3, b"\x01" + raw[5:-2])))
            case "checksum":
                writer.write(raw[:-1] + bytes([raw[-1] ^ 1]))
            case "coalesced":
                writer.write(raw + raw)
            case "header":
                writer.write(b"\x00" + raw[1:])
            case "length":
                writer.write(raw[:1] + b"\x08" + raw[2:])
            case "code":
                writer.write(encode(Frame(0, 4, raw[4:-2])))
            case "truncated":
                writer.write(raw[:5])
                await writer.drain()
                writer.close()
            case "disconnect":
                writer.close()
        if control and expected != "success":
            with pytest.raises(
                HomeAssistantError,
                match="rejected" if expected == "rejected" else "outcome unconfirmed",
            ):
                await task
        else:
            await task
        await hass.async_block_till_done()
        state = hass.states.get(sensor)
        assert state.state == ("not_queried" if control else expected)
        if control:
            assert hub.can_pulse == (expected in {"success", "rejected"})
            if expected not in {"success", "rejected"}:
                assert hub.transport.tainted
                assert hass.states.get(tcp_id(hass, entry)).state == STATE_OFF
        elif expected == "success":
            assert state.attributes["last_success"]
            assert state.attributes["elapsed_ms"] > 0
            assert hub.can_query
            followup = asyncio.create_task(hub.query())
            assert await next_item(requests) == bytes.fromhex("7E 06 02 21 00 00 DC FF")
            writer.write(raw)
            await followup
            assert hub.result == "success"
        else:
            assert not hub.can_query
            assert not hub.can_pulse
            assert hass.states.get(tcp_id(hass, entry)).state == STATE_OFF
        if domain == "switch":
            if expected == "success":
                assert hub.hold_open is enabled
                assert hass.states.get(hold_switch).state == (
                    "on" if enabled else "off"
                )
            elif expected == "rejected":
                assert hub.hold_open is not enabled
            else:
                assert hub.hold_open is None
                assert hass.states.get(hold_switch).state == "unavailable"
        assert requests.empty()
        assert peers.empty()
        assert await hass.config_entries.async_reload(entry.entry_id)
        await hass.async_block_till_done()
        reloaded_peer = await next_item(peers)
        assert hass.states.get(hold_switch).state == "unknown"
        assert entry.runtime_data.hold_open is None
        assert (
            registry.async_get_entity_id(
                "switch", DOMAIN, f"{entry.entry_id}:2:hold_open"
            )
            == hold_switch
        )
        assert hass.states.get(button).state != "unavailable"
        if response == "timeout":
            reloaded = entry.runtime_data
            reloaded_peer.write(raw)
            await reloaded_peer.drain()
            await asyncio.wait_for(reloaded.transport._receiver, 2)
            with pytest.raises(HomeAssistantError, match="not sent"):
                await reloaded.pulse()

        assert requests.empty()
        assert await hass.config_entries.async_unload(entry.entry_id)
