"""Test the integration lifecycle against a passive loopback gateway."""

import asyncio
import sqlite3
from contextlib import asynccontextmanager
from hashlib import sha256

import pytest
from homeassistant.config_entries import SOURCE_RECONFIGURE, SOURCE_USER
from homeassistant.const import EVENT_HOMEASSISTANT_STOP, STATE_OFF, STATE_ON
from homeassistant.core import Context
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.soyal_access.const import DOMAIN

pytestmark = pytest.mark.usefixtures("enable_custom_integrations", "socket_enabled")


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
        storage = tmp_path / ".storage"
        storage.mkdir(exist_ok=True)
        key = sha256(repr((data["host"], port)).encode()).hexdigest()
        uncertain = storage / f"soyal_access.{key}"
        uncertain.write_text(
            '{"version":1,"data":{"uncertain":[2],"kind":{"2":"control"}}}'
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
        with pytest.raises(HomeAssistantError, match="Legacy uncertainty"):
            await entry.runtime_data.confirm_pulse()
        assert tcp_id(hass, entry) == old_tcp.entity_id
        assert entities.async_get(old_tcp.entity_id).device_id == old_gateway.id
        assert (
            entities.async_get_entity_id(
                "button", DOMAIN, f"{entry.entry_id}:2:pulse_open"
            )
            == old_control.entity_id
        )
        state = hass.states.get(old_control.entity_id)
        assert state is None or state.state == "unavailable"

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


@pytest.mark.parametrize("pulse", [False, True])
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
async def test_node02_query(
    hass, hass_admin_user, hass_read_only_user, response, expected, pulse
):
    from custom_components.soyal_access.protocol import Frame, encode

    if not pulse and response in {"ack", "nack"}:
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
        assert hass.states.get(button).state == "unavailable"
        assert hass.states.get(sensor).state == "not_queried"
        assert requests.empty()
        authorization = {"entry_id": entry.entry_id, "verified_safe": True}
        with pytest.raises(HomeAssistantError):
            await hass.services.async_call(
                DOMAIN,
                "confirm_node02_query",
                authorization,
                context=Context(user_id=hass_read_only_user.id),
                blocking=True,
            )
        await hass.services.async_call(
            DOMAIN,
            "confirm_node02_query",
            authorization,
            context=Context(user_id=hass_admin_user.id),
            blocking=True,
        )
        pulse_button = registry.async_get_entity_id(
            "button", DOMAIN, f"{entry.entry_id}:2:pulse_open"
        )
        assert hass.states.get(pulse_button).state == "unavailable"
        await hass.services.async_call(
            DOMAIN,
            "confirm_node02_pulse",
            authorization,
            context=Context(user_id=hass_admin_user.id),
            blocking=True,
        )
        if pulse:
            button = pulse_button
        task = asyncio.create_task(
            hass.services.async_call(
                "button", "press", {"entity_id": button}, blocking=True
            )
        )
        tx = "7E 05 02 21 84 58 FF" if pulse else "7E 06 02 21 00 00 DC FF"
        assert await next_item(requests) == bytes.fromhex(tx)
        with pytest.raises(HomeAssistantError):
            await hub.query()
        with pytest.raises(HomeAssistantError, match="not sent"):
            await hub.pulse()
        assert hass.states.get(button).state != "unavailable"
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
        if pulse and expected != "success":
            with pytest.raises(
                HomeAssistantError,
                match="rejected" if expected == "rejected" else "outcome unconfirmed",
            ):
                await task
        else:
            await task
        await hass.async_block_till_done()
        state = hass.states.get(sensor)
        assert state.state == ("not_queried" if pulse else expected)
        if pulse:
            assert hub.can_pulse == (expected == "success")
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
            with pytest.raises(HomeAssistantError):
                hub.confirm()
        assert requests.empty()
        assert peers.empty()
        assert await hass.config_entries.async_reload(entry.entry_id)
        await hass.async_block_till_done()
        await next_item(peers)
        assert hass.states.get(button).state == "unavailable"
        assert requests.empty()
        assert await hass.config_entries.async_unload(entry.entry_id)
