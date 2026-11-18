"""Home Assistant lifecycle and control tests against a loopback peer."""

import asyncio
import os
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest
import voluptuous as vol
from homeassistant.config_entries import (
    SOURCE_RECONFIGURE,
    SOURCE_USER,
    ConfigEntryState,
)
from homeassistant.const import STATE_UNKNOWN
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.soyal_access.const import DEFAULTS, DOMAIN
from custom_components.soyal_access.protocol import Frame, encode
from tests.fixtures import FIELD_REQUESTS
from tests.simulator import SimulatedGateway

pytestmark = [
    pytest.mark.asyncio,
    pytest.mark.usefixtures("enable_custom_integrations", "socket_enabled"),
]


def settings(**changes):
    return DEFAULTS | {"host": "127.0.0.1"} | changes


def acknowledge(station, code=4):
    return encode(Frame(0, code, bytes([station])))


def io_reply(station, io=0x40):
    return encode(Frame(0, 0x03, bytes((station, 0xA0, io, 0, 0))))


def control_replies(response_kind):
    if response_kind == "ack":
        return [acknowledge(node) for node, *_ in FIELD_REQUESTS[:6]]
    if response_kind == "field":
        path = os.environ.get("SOYAL_CONTROL_RX")
        if path is None:
            pytest.skip("Set SOYAL_CONTROL_RX to replay the six local control captures")
        return [
            bytes.fromhex(line)
            for line in Path(path).read_text(encoding="ascii").splitlines()
        ]
    return [
        io_reply(node, 0 if action == "83" else 0x40)
        for node, _, action, _ in FIELD_REQUESTS[:6]
    ]


def entity_id(hass, entry, platform, station, key):
    return er.async_get(hass).async_get_entity_id(
        platform, DOMAIN, f"{entry.entry_id}:{station}:{key}"
    )


async def service(hass, entry, platform, station, key, action):
    await hass.services.async_call(
        platform,
        action,
        {"entity_id": entity_id(hass, entry, platform, station, key)},
        blocking=True,
    )


async def setup_gateway(hass, **changes):
    entry = MockConfigEntry(domain=DOMAIN, data=settings(**changes))
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    return entry


async def test_config_flow_and_reconfigure(hass):
    with patch("asyncio.open_connection", new_callable=AsyncMock) as connect:
        result = await hass.config_entries.flow.async_init(
            DOMAIN, context={"source": SOURCE_USER}
        )
        assert result["type"] is FlowResultType.FORM
        with pytest.raises(vol.Invalid):
            result["data_schema"](settings(timeout="nan"))
        invalid = await hass.config_entries.flow.async_configure(
            result["flow_id"], settings(host="https://invalid/", stations="01,1")
        )
        assert invalid["errors"] == {
            "host": "invalid_host",
            "stations": "invalid_stations",
        }
        created = await hass.config_entries.flow.async_configure(
            result["flow_id"], settings(host="Example.TEST.", stations="01,2")
        )
        assert created["type"] is FlowResultType.CREATE_ENTRY
        entry = created["result"]
        assert entry.data["host"] == "example.test"
        assert entry.data["stations"] == "1,2"
        duplicate = await hass.config_entries.flow.async_init(
            DOMAIN, context={"source": SOURCE_USER}, data=settings(host="example.test")
        )
        assert duplicate["reason"] == "already_configured"
        await hass.async_block_till_done()
        existing = entity_id(hass, entry, "button", 2, "pulse_open")
        form = await hass.config_entries.flow.async_init(
            DOMAIN, context={"source": SOURCE_RECONFIGURE, "entry_id": entry.entry_id}
        )
        changed = await hass.config_entries.flow.async_configure(
            form["flow_id"], settings(host="example.test", stations="2,3")
        )
        assert changed["reason"] == "reconfigure_successful"
        await hass.async_block_till_done()
        assert entry.runtime_data.nodes == (2, 3)
        assert entity_id(hass, entry, "button", 2, "pulse_open") == existing
        connect.assert_not_awaited()


@pytest.mark.parametrize("response_kind", ("ack", "anonymous", "field"))
async def test_devices_actions_and_lifecycle(hass, response_kind):
    responses = control_replies(response_kind)
    gateway = SimulatedGateway()
    async with gateway.listen() as port:
        entry = await setup_gateway(hass, port=port, enable_control=True)
        registry = dr.async_get(hass)
        for node in (1, 2):
            device = registry.async_get_device(
                identifiers={(DOMAIN, f"{entry.entry_id}:{node}")}
            )
            assert device.name.endswith(f"Node {node:02d}")
            switch_id = entity_id(hass, entry, "switch", node, "hold_open")
            assert hass.states.get(switch_id).state == STATE_UNKNOWN
        assert gateway.requests.empty()
        actions = {
            "84": ("button", "pulse_open", "press"),
            "82": ("switch", "hold_open", "turn_on"),
            "83": ("switch", "hold_open", "turn_off"),
        }
        for (node, _, data, field_hex), reply in zip(
            FIELD_REQUESTS[:6], responses, strict=True
        ):
            gateway.responses.put_nowait((io_reply(3) + reply[:3], reply[3:]))
            platform, key, action = actions[data]
            await service(hass, entry, platform, node, key, action)
            assert encode(gateway.requests.get_nowait()) == bytes.fromhex(field_hex)
            status = entry.runtime_data.status[node]
            assert status.last_response is not None
            assert status.outcome == (
                "ack" if response_kind == "ack" else "data_response"
            )
            assert node not in entry.runtime_data.uncertain
            assert (
                hass.states.get(
                    entity_id(hass, entry, "switch", node, "hold_open")
                ).state
                == STATE_UNKNOWN
            )
            assert not entry.runtime_data.connected
        assert hass.states.get(switch_id).state == STATE_UNKNOWN
        assert hass.states.get(switch_id).attributes["assumed_state"]
        old = entry.runtime_data
        assert await hass.config_entries.async_reload(entry.entry_id)
        await hass.async_block_till_done()
        assert old is not entry.runtime_data
        assert hass.states.get(switch_id).state == STATE_UNKNOWN
        assert gateway.requests.empty()
        assert await hass.config_entries.async_unload(entry.entry_id)
        assert entry.state is ConfigEntryState.NOT_LOADED


async def test_nack_serialization_timeout_and_reload(hass):
    gateway = SimulatedGateway()
    async with gateway.listen() as port:
        entry = await setup_gateway(
            hass, port=port, enable_control=True, timeout=0.25, reconnect_delay=0
        )
        manager = entry.runtime_data
        first = asyncio.create_task(manager.control(1, 0x84))
        assert (await asyncio.wait_for(gateway.requests.get(), 1)).node == 1
        second = asyncio.create_task(manager.control(2, 0x82))
        with pytest.raises(TimeoutError):
            await asyncio.wait_for(gateway.requests.get(), 0.02)
        assert manager.waiting == 1 and manager.active
        gateway.responses.put_nowait((acknowledge(1, 5),))
        with pytest.raises(HomeAssistantError, match="rejected"):
            await first
        assert manager.status[1].outcome == "nack"
        assert (await asyncio.wait_for(gateway.requests.get(), 1)).node == 2
        gateway.responses.put_nowait((acknowledge(2),))
        await second

        gateway.responses.put_nowait(None)
        with pytest.raises(HomeAssistantError, match="uncertain"):
            await manager.control(1, 0x84)
        assert gateway.requests.get_nowait().node == 1
        assert manager.errors == 1
        assert await hass.config_entries.async_reload(entry.entry_id)
        await hass.async_block_till_done()
        manager = entry.runtime_data
        assert manager.uncertain == {1}
        assert (
            hass.states.get(
                entity_id(hass, entry, "binary_sensor", 1, "uncertain")
            ).state
            == "on"
        )
        with pytest.raises(HomeAssistantError, match="blocked"):
            await manager.control(1, 0x83)
        assert gateway.requests.empty()
        gateway.responses.put_nowait((io_reply(1) + io_reply(2),))
        await manager.control(2, 0x83)
        assert gateway.requests.get_nowait().node == 2
        assert manager.uncertain == {1}
        assert manager.status[2].outcome == "data_response"


async def test_connection_failure_cooldown_and_explicit_retry(hass):
    gateway = SimulatedGateway()
    async with gateway.listen() as port:
        entry = await setup_gateway(hass, port=port, enable_control=True)
        manager = entry.runtime_data
        with (
            patch("asyncio.open_connection", side_effect=ConnectionRefusedError),
            pytest.raises(HomeAssistantError, match="disconnected"),
        ):
            await manager.control(1, 0x84)
        assert not manager.uncertain
        with pytest.raises(HomeAssistantError, match="cooldown"):
            await manager.control(1, 0x84)
        await asyncio.sleep(1.01)
        gateway.responses.put_nowait((acknowledge(1),))
        await manager.control(1, 0x84)
        assert gateway.requests.get_nowait().node == 1
        assert gateway.requests.empty()


async def test_disabled_storage_failure_and_unload_in_flight(hass):
    disabled = await setup_gateway(hass)
    with pytest.raises(HomeAssistantError, match="disabled"):
        await disabled.runtime_data.control(1, 0x84)
    gateway = SimulatedGateway()
    async with gateway.listen() as port:
        entry = await setup_gateway(hass, port=port, enable_control=True)
        manager = entry.runtime_data
        with (
            patch.object(manager._store, "async_save", side_effect=OSError),
            pytest.raises(HomeAssistantError, match="save"),
        ):
            await manager.control(1, 0x84)
        assert gateway.requests.empty()
        gateway.responses.put_nowait(None)
        active = asyncio.create_task(manager.control(2, 0x84))
        assert (await asyncio.wait_for(gateway.requests.get(), 1)).node == 2
        assert await hass.config_entries.async_unload(entry.entry_id)
        with pytest.raises(asyncio.CancelledError):
            await active
        assert not manager.connected


async def test_invalid_journal_blocks_all_stations(hass):
    with patch(
        "custom_components.soyal_access.gateway.Store.async_load",
        new=AsyncMock(return_value={"uncertain": ["invalid"]}),
    ):
        entry = await setup_gateway(hass, enable_control=True)
    manager = entry.runtime_data
    assert manager.uncertain == {1, 2}
    for status in manager.status.values():
        assert status.outcome == "storage_error"
    with pytest.raises(HomeAssistantError, match="blocked"):
        await manager.control(1, 0x84)


async def test_ack_with_failed_journal_clear_stays_blocked(hass):
    gateway = SimulatedGateway()
    async with gateway.listen() as port:
        entry = await setup_gateway(hass, port=port, enable_control=True)
        manager = entry.runtime_data
        save = manager._store.async_save

        async def fail_clear(data):
            if not data["uncertain"]:
                raise OSError
            await save(data)

        gateway.responses.put_nowait((acknowledge(1),))
        with (
            patch.object(manager._store, "async_save", side_effect=fail_clear),
            pytest.raises(HomeAssistantError, match="clear"),
        ):
            await manager.control(1, 0x84)
        assert gateway.requests.get_nowait().node == 1
        assert manager.status[1].outcome == "storage_error"
        assert manager.uncertain == {1}
        assert await hass.config_entries.async_reload(entry.entry_id)
        assert entry.runtime_data.uncertain == {1}


@pytest.mark.parametrize(
    "reply",
    [
        pytest.param(io_reply(2), id="wrong-station"),
        pytest.param(io_reply(0), id="unknown-source"),
        pytest.param(encode(Frame(0, 4)), id="source-less-ack"),
        pytest.param(
            io_reply(1)[:-1] + bytes([io_reply(1)[-1] ^ 1]), id="bad-checksum"
        ),
        pytest.param(encode(Frame(0, 0x03, bytes([1]) + bytes(3))), id="too-short"),
        pytest.param(encode(Frame(0, 0x03, bytes([1]) + bytes(12))), id="rtc-length"),
        pytest.param(encode(Frame(0, 0x09, bytes([1]) + bytes(4))), id="wrong-code"),
    ],
)
async def test_invalid_control_response_leaves_station_uncertain(hass, reply):
    gateway = SimulatedGateway()
    async with gateway.listen() as port:
        entry = await setup_gateway(hass, port=port, enable_control=True)
        gateway.responses.put_nowait((reply,))
        with pytest.raises(HomeAssistantError, match="uncertain"):
            await service(hass, entry, "button", 1, "pulse_open", "press")
        assert gateway.requests.get_nowait() == Frame(1, 0x21, b"\x84")
        assert gateway.requests.empty()
        assert entry.runtime_data.uncertain == {1}
        assert entry.runtime_data.status[1].last_response is None
