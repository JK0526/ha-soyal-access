"""Home Assistant lifecycle and control tests against a loopback peer."""

import asyncio
import os
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest
from homeassistant.config_entries import (
    SOURCE_RECONFIGURE,
    SOURCE_USER,
    ConfigEntryState,
)
from homeassistant.const import STATE_UNKNOWN
from homeassistant.data_entry_flow import FlowResultType, InvalidData
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
        for value in ("nan", "inf", "-inf"):
            with pytest.raises(InvalidData):
                await hass.config_entries.flow.async_configure(
                    result["flow_id"], settings(timeout=value)
                )
        with pytest.raises(InvalidData):
            await hass.config_entries.flow.async_configure(
                result["flow_id"], settings(enable_control="false")
            )
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
    gateway = SimulatedGateway(persistent=True)
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
            status = entry.runtime_data.coordinator.status[node]
            assert status.last_response is not None
            assert status.outcome == (
                "ack" if response_kind == "ack" else "data_response"
            )
            assert node not in entry.runtime_data.control.uncertain
            assert (
                hass.states.get(
                    entity_id(hass, entry, "switch", node, "hold_open")
                ).state
                == STATE_UNKNOWN
            )
            assert isinstance(entry.runtime_data.coordinator.connected, bool)
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
        first = asyncio.create_task(manager.control.execute(1, 0x84))
        assert (await asyncio.wait_for(gateway.requests.get(), 1)).node == 1
        second = asyncio.create_task(manager.control.execute(2, 0x82))
        with pytest.raises(TimeoutError):
            await asyncio.wait_for(gateway.requests.get(), 0.02)
        assert manager.transport.waiting == 1 and manager.transport.active
        gateway.responses.put_nowait((acknowledge(1, 5),))
        with pytest.raises(HomeAssistantError, match="rejected"):
            await first
        assert manager.coordinator.status[1].outcome == "nack"
        assert (await asyncio.wait_for(gateway.requests.get(), 1)).node == 2
        gateway.responses.put_nowait((acknowledge(2),))
        await second

        gateway.responses.put_nowait(None)
        with pytest.raises(HomeAssistantError, match="uncertain"):
            await manager.control.execute(1, 0x84)
        assert gateway.requests.get_nowait().node == 1
        assert manager.coordinator.errors == 1
        assert await hass.config_entries.async_reload(entry.entry_id)
        await hass.async_block_till_done()
        manager = entry.runtime_data
        assert manager.control.uncertain == {1}
        assert (
            hass.states.get(
                entity_id(hass, entry, "binary_sensor", 1, "uncertain")
            ).state
            == "on"
        )
        with pytest.raises(HomeAssistantError, match="blocked"):
            await manager.control.execute(1, 0x83)
        assert gateway.requests.empty()
        gateway.responses.put_nowait((io_reply(1) + io_reply(2),))
        await manager.control.execute(2, 0x83)
        assert gateway.requests.get_nowait().node == 2
        assert manager.control.uncertain == {1}
        assert manager.coordinator.status[2].outcome == "data_response"


async def test_connection_failure_cooldown_and_explicit_retry(hass):
    gateway = SimulatedGateway()
    async with gateway.listen() as port:
        entry = await setup_gateway(hass, port=port, enable_control=True, stations="1")
        manager = entry.runtime_data
        with (
            patch("asyncio.open_connection", side_effect=ConnectionRefusedError),
            pytest.raises(HomeAssistantError, match="disconnected"),
        ):
            await manager.control.execute(1, 0x84)
        assert not manager.control.uncertain
        with pytest.raises(HomeAssistantError, match="cooldown"):
            await manager.control.execute(1, 0x84)
        await asyncio.sleep(1.01)
        gateway.responses.put_nowait((acknowledge(1),))
        await manager.control.execute(1, 0x84)
        assert gateway.requests.get_nowait().node == 1
        assert gateway.requests.empty()


async def test_disabled_storage_failure_and_unload_in_flight(hass):
    disabled = await setup_gateway(hass)
    with pytest.raises(HomeAssistantError, match="disabled"):
        await disabled.runtime_data.control.execute(1, 0x84)
    gateway = SimulatedGateway()
    async with gateway.listen() as port:
        entry = await setup_gateway(hass, port=port, enable_control=True)
        manager = entry.runtime_data
        with (
            patch.object(manager.control._store, "async_save", side_effect=OSError),
            pytest.raises(HomeAssistantError, match="save"),
        ):
            await manager.control.execute(1, 0x84)
        assert gateway.requests.empty()
        gateway.responses.put_nowait(None)
        active = asyncio.create_task(manager.control.execute(2, 0x84))
        assert (await asyncio.wait_for(gateway.requests.get(), 1)).node == 2
        assert await hass.config_entries.async_unload(entry.entry_id)
        with pytest.raises(asyncio.CancelledError):
            await active
        assert not manager.coordinator.connected


async def test_invalid_journal_blocks_all_stations(hass):
    with patch(
        "custom_components.soyal_access.control.Store.async_load",
        new=AsyncMock(return_value={"uncertain": ["invalid"]}),
    ):
        entry = await setup_gateway(hass, enable_control=True)
    manager = entry.runtime_data
    assert manager.control.uncertain == {1, 2}
    for status in manager.coordinator.status.values():
        assert status.outcome == "storage_error"
    with pytest.raises(HomeAssistantError, match="blocked"):
        await manager.control.execute(1, 0x84)


async def test_ack_with_failed_journal_clear_stays_blocked(hass):
    gateway = SimulatedGateway()
    async with gateway.listen() as port:
        entry = await setup_gateway(hass, port=port, enable_control=True)
        manager = entry.runtime_data
        save = manager.control._store.async_save

        async def fail_clear(data):
            if not data["uncertain"]:
                raise OSError
            await save(data)

        gateway.responses.put_nowait((acknowledge(1),))
        with (
            patch.object(manager.control._store, "async_save", side_effect=fail_clear),
            pytest.raises(HomeAssistantError, match="clear"),
        ):
            await manager.control.execute(1, 0x84)
        assert gateway.requests.get_nowait().node == 1
        assert manager.coordinator.status[1].outcome == "storage_error"
        assert manager.control.uncertain == {1}
        assert await hass.config_entries.async_reload(entry.entry_id)
        assert entry.runtime_data.control.uncertain == {1}


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
        assert entry.runtime_data.control.uncertain == {1}
        assert entry.runtime_data.coordinator.status[1].last_response is None


async def test_event_storage_lifecycle_and_backup_barrier(hass, tmp_path):
    from custom_components.soyal_access.backup import (
        async_post_backup,
        async_pre_backup,
    )
    from custom_components.soyal_access.diagnostics import (
        async_get_config_entry_diagnostics,
    )

    hass.config.config_dir = str(tmp_path)
    with patch("asyncio.open_connection", new_callable=AsyncMock) as connect:
        entry = await setup_gateway(hass, enable_control=True)
        for _ in range(2):
            manager = entry.runtime_data
            assert manager.events.path.parent == tmp_path / "soyal_access"
            assert not manager.preview.enabled
            await async_pre_backup(hass)
            diagnostic = asyncio.create_task(
                async_get_config_entry_diagnostics(hass, entry)
            )
            await asyncio.sleep(0)
            assert not diagnostic.done()
            await async_post_backup(hass)
            data = await diagnostic
            assert data["stations"][1]["saved"] == 0
            assert await hass.config_entries.async_reload(entry.entry_id)
            assert manager.events._db is None
        manager = entry.runtime_data
        assert await hass.config_entries.async_unload(entry.entry_id)
        assert manager.events._db is None
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
        assert entry.runtime_data is not manager
        assert not entry.runtime_data.preview.enabled
        connect.assert_not_called()


async def test_history_websocket_permissions_and_person_lifecycle(
    hass, hass_ws_client, hass_read_only_user, tmp_path
):
    hass.config.config_dir = str(tmp_path)
    entry = await setup_gateway(hass)
    admin = await hass_ws_client()
    token = await hass.auth.async_create_refresh_token(
        hass_read_only_user, "https://example.test"
    )
    reader = await hass_ws_client(
        access_token=hass.auth.async_create_access_token(token)
    )
    store = entry.runtime_data.events

    async def request(client, endpoint, **data):
        await client.send_json_auto_id(
            {"type": f"soyal_access/{endpoint}", "entry_id": entry.entry_id, **data}
        )
        return await client.receive_json()

    identity = {"station": 1, "user_address": 28}
    person = identity | {"name": "林小君", "employee_id": "A01"}
    for endpoint, data in (
        ("recover_station", {"station": 2, "step": "prepare"}),
        ("events", {}),
        ("people", {}),
        ("person/save", {"action": "create", **person}),
        ("person/delete", identity),
    ):
        assert (await request(reader, endpoint, **data))["error"][
            "code"
        ] == "unauthorized"

    assert (await request(admin, "person/save", action="create", **person))["success"]
    assert (await request(admin, "person/save", action="create", **person))["error"][
        "code"
    ] == "conflict"
    assert (
        await request(
            admin, "person/save", action="create", **(person | {"station": 2})
        )
    )["success"]
    assert (
        len(
            (await request(admin, "people", filters={"user_address": 28}))["result"][
                "rows"
            ]
        )
        == 2
    )

    await store.state(1)
    raw = encode(
        Frame(0, 0x0B, bytes([1, 5, 4, 3, 2, 1, 11, 26, 7, 0, 28]) + bytes(14))
    )
    await store.save_head(1, raw)
    filters = identity | {
        "name": "林",
        "start": "2026-11-01",
        "end": "2026-11-01",
        "event_type": 0x0B,
    }
    event = (await request(admin, "events", filters=filters))["result"]["rows"][0]
    assert event["name"] == "林小君" and event["origin"] == "backfill"
    assert event["controller_time"] == "2026-11-01T03:04:05" and event[
        "received_at"
    ].endswith("Z")
    assert {"raw", "digest"}.isdisjoint(event)
    assert (await request(admin, "events", filters={"name": "' OR 1=1 --"}))["result"][
        "rows"
    ] == []
    assert not (await request(admin, "events", limit=201))["success"]
    assert not (await request(admin, "events", filters={"start": "2026-02-30"}))[
        "success"
    ]

    assert (
        await request(
            admin, "person/save", action="update", **(person | {"name": "陳小美"})
        )
    )["success"]
    assert (await request(admin, "events"))["result"]["rows"][0]["name"] == "陳小美"
    assert (await request(admin, "person/delete", **identity))["success"]
    fallback = (await request(admin, "events"))["result"]["rows"][0]
    assert fallback["name"] is None and fallback["display_name"] == "28"

    assert await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done()
    state = hass.states.get(entity_id(hass, entry, "sensor", 1, "latest_stored_event"))
    assert state.state == "card"
    assert state.attributes["sync_phase"] == "uncertain"
    assert state.attributes["controller_time"] == "2026-11-01T03:04:05"
    assert {"name", "user_address", "employee_id"}.isdisjoint(state.attributes)
    assert await hass.config_entries.async_unload(entry.entry_id)
    assert (await request(admin, "events"))["error"]["code"] == "not_found"


async def test_frontend_config_flow_endpoints(hass, hass_client, tmp_path):
    """Exercise the HTTP endpoints used by Add integration and Reconfigure."""
    from homeassistant.setup import async_setup_component

    hass.config.config_dir = str(tmp_path)
    assert await async_setup_component(hass, "config", {})
    assert await async_setup_component(hass, "websocket_api", {})
    client = await hass_client()
    base = "/api/config/config_entries/flow"

    async def post(path, data):
        response = await client.post(path, json=data)
        assert response.status == 200, await response.text()
        return await response.json()

    with patch("asyncio.open_connection", new_callable=AsyncMock) as connect:
        response = await client.get(f"{base}_handlers")
        assert response.status == 200
        assert DOMAIN in await response.json()

        form = await post(base, {"handler": DOMAIN})
        assert form["type"] == "form" and form["step_id"] == "user"
        assert {"host", "port", "stations"} <= {
            field["name"] for field in form["data_schema"]
        }
        data = settings(host="Offline-Gateway.test", port=1502, stations="01, 02")
        created = await post(f"{base}/{form['flow_id']}", data)
        assert created["type"] == "create_entry"
        await hass.async_block_till_done()
        (entry,) = hass.config_entries.async_entries(DOMAIN)
        assert entry.state is ConfigEntryState.LOADED
        assert (entry.data["host"], entry.data["port"], entry.data["stations"]) == (
            "offline-gateway.test",
            1502,
            "1,2",
        )

        duplicate = await post(base, {"handler": DOMAIN})
        duplicate = await post(f"{base}/{duplicate['flow_id']}", data)
        assert duplicate["reason"] == "already_configured"

        form = await post(base, {"handler": DOMAIN, "entry_id": entry.entry_id})
        assert form["type"] == "form" and form["step_id"] == "reconfigure"
        updated = await post(f"{base}/{form['flow_id']}", data | {"stations": "2,3"})
        assert updated["reason"] == "reconfigure_successful"
        await hass.async_block_till_done()
        assert entry.runtime_data.nodes == (2, 3)
        previous = entry.runtime_data
        assert await hass.config_entries.async_unload(entry.entry_id)
        assert previous.events._db is None
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
        assert entry.runtime_data is not previous
        assert entry.runtime_data.nodes == (2, 3)
        connect.assert_not_awaited()


async def test_control_availability_after_frontend_reconfigure(
    hass, hass_client, tmp_path
):
    from homeassistant.setup import async_setup_component

    hass.config.config_dir = str(tmp_path)
    assert await async_setup_component(hass, "config", {})
    assert await async_setup_component(hass, "websocket_api", {})
    http = await hass_client()
    base = "/api/config/config_entries/flow"

    async def post(path, data):
        response = await http.post(path, json=data)
        assert response.status == 200, await response.text()
        return await response.json()

    gateway = SimulatedGateway()
    async with gateway.listen() as port:
        form = await post(base, {"handler": DOMAIN})
        control_field = next(
            field for field in form["data_schema"] if field["name"] == "enable_control"
        )
        assert control_field["selector"] == {"boolean": {}}
        assert control_field["default"] is False
        submitted = {"host": "127.0.0.1", "port": port}
        created = await post(f"{base}/{form['flow_id']}", submitted)
        assert created["type"] == "create_entry"
        await hass.async_block_till_done()
        (entry,) = hass.config_entries.async_entries(DOMAIN)

        controls = (
            ("button", "pulse_open", "press", 0x84),
            ("switch", "hold_open", "turn_on", 0x82),
            ("switch", "hold_open", "turn_off", 0x83),
        )
        ids = {
            (node, platform): entity_id(hass, entry, platform, node, key)
            for node in (1, 2)
            for platform, key, _, _ in controls
        }

        def assert_control_state(enabled, blocked=()):
            assert entry.data["enable_control"] is enabled
            assert entry.runtime_data.control.enabled is enabled
            assert entry.runtime_data.transport is not None
            for (node, platform), control_id in ids.items():
                state = hass.states.get(control_id)
                assert state is not None
                assert (state.state != "unavailable") is (
                    enabled and node not in blocked
                )
                if platform == "switch" and enabled and node not in blocked:
                    assert state.state == STATE_UNKNOWN
                assert er.async_get(hass).async_get(control_id).disabled_by is None
                outcome = hass.states.get(
                    entity_id(hass, entry, "sensor", node, "outcome")
                )
                assert outcome.attributes["control_block_reason"] == (
                    "disabled_in_settings"
                    if not enabled
                    else "uncertain"
                    if node in blocked
                    else None
                )

        async def reconfigure(enabled):
            old_manager = entry.runtime_data
            form = await post(base, {"handler": DOMAIN, "entry_id": entry.entry_id})
            change = await post(
                f"{base}/{form['flow_id']}", {"enable_control": enabled}
            )
            assert change["reason"] == "reconfigure_successful"
            await hass.async_block_till_done()
            assert entry.runtime_data is not old_manager
            assert old_manager.events._db is None
            assert_control_state(enabled)
            assert gateway.requests.empty()

        assert_control_state(False)
        await reconfigure(True)
        for node in (1, 2):
            for platform, key, action, code in controls:
                gateway.responses.put_nowait((io_reply(node),))
                await service(hass, entry, platform, node, key, action)
                assert gateway.requests.get_nowait() == Frame(node, 0x21, bytes([code]))
        await reconfigure(False)
        await reconfigure(True)
        gateway.responses.put_nowait((b"",))
        with pytest.raises(HomeAssistantError, match="uncertain"):
            await service(hass, entry, "button", 1, "pulse_open", "press")
        assert gateway.requests.get_nowait() == Frame(1, 0x21, b"\x84")
        assert_control_state(True, blocked=(1,))
        assert await hass.config_entries.async_unload(entry.entry_id)
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
        assert_control_state(True, blocked=(1,))
        assert gateway.requests.empty()


@pytest.mark.parametrize("saved_value", [None, "false"])
async def test_unconfirmed_legacy_enable_control_is_fail_closed(hass, saved_value):
    data = settings()
    if saved_value is None:
        del data["enable_control"]
    else:
        data["enable_control"] = saved_value
    entry = MockConfigEntry(domain=DOMAIN, data=data)
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    assert entry.runtime_data.transport is not None
    control_id = entity_id(hass, entry, "button", 1, "pulse_open")
    assert hass.states.get(control_id).state == "unavailable"
    from custom_components.soyal_access.diagnostics import (
        async_get_config_entry_diagnostics,
    )

    diagnostics = await async_get_config_entry_diagnostics(hass, entry)
    assert diagnostics["control_enabled"] is False
    assert diagnostics["stations"][1]["control_block_reason"] == "disabled_in_settings"


async def test_persistent_monitor_without_control(hass):
    from unittest.mock import patch

    from custom_components.soyal_access.transport import Transport

    connections = 0
    requests = asyncio.Queue()
    writers = []

    async def peer(reader, writer):
        nonlocal connections
        connections += 1
        writers.append(writer)
        try:
            while True:
                header = await reader.readexactly(2)
                body = await reader.readexactly(header[1])
                node = body[0]
                await requests.put((node, body[1:-2]))
                reply = io_reply(node)
                writer.write(reply[:3])
                await writer.drain()
                writer.write(reply[3:])
                await writer.drain()
        except (asyncio.IncompleteReadError, ConnectionResetError):
            pass
        finally:
            writer.close()
            try:
                await writer.wait_closed()
            except ConnectionResetError:
                pass

    server = await asyncio.start_server(peer, "127.0.0.1", 0)
    async with server:
        entry = await setup_gateway(hass, port=server.sockets[0].getsockname()[1])
        manager = entry.runtime_data
        # Advance the monitor by replacing only its sleep, preserving loopback I/O.
        original_sleep = asyncio.sleep

        async def fast_sleep(seconds):
            await original_sleep(0.02 if seconds == 15 else seconds)

        with patch(
            "custom_components.soyal_access.coordinator.asyncio.sleep", fast_sleep
        ):
            manager.coordinator.task.cancel()
            await asyncio.gather(manager.coordinator.task, return_exceptions=True)
            manager.start_monitoring()
            for _ in range(4):
                node, command = await asyncio.wait_for(requests.get(), 2)
                assert node == 2 and command == b"\x21\x00\x00"
            assert isinstance(manager.transport, Transport)
            assert connections == 1
            assert manager.coordinator.connected
            assert manager.coordinator.status[2].reachable
            assert manager.coordinator.status[1].reachable is None
            writers[0].close()
            await writers[0].wait_closed()
            for _ in range(2):
                await asyncio.wait_for(requests.get(), 2)
            assert connections == 2
            assert not manager.control.enabled
            assert await hass.config_entries.async_unload(entry.entry_id)
            assert not manager.coordinator.connected


async def test_session_monitor_control_interleaving_and_late_reply(hass):
    from custom_components.soyal_access.transport import IO_REPLY

    gateway = SimulatedGateway()
    async with gateway.listen() as port:
        entry = await setup_gateway(
            hass, port=port, enable_control=True, timeout=0.1, reconnect_delay=0
        )
        manager = entry.runtime_data
        client = manager.transport
        gateway.responses.put_nowait((io_reply(1),))
        reply = await client.transact(Frame(1, 0x21, b"\x00\x00"), IO_REPLY)
        assert reply.source == 1
        assert gateway.requests.get_nowait().node == 1
        gateway.responses.put_nowait(None)
        with pytest.raises(HomeAssistantError, match="uncertain"):
            await manager.control.execute(1, 0x84)
        assert gateway.requests.get_nowait().node == 1
        gateway.responses.put_nowait((io_reply(1) + io_reply(2),))
        await manager.control.execute(2, 0x82)
        assert gateway.requests.get_nowait().node == 2
        assert manager.control.uncertain == {1}
        with pytest.raises(HomeAssistantError, match="blocked"):
            await manager.control.execute(1, 0x83)
        assert gateway.requests.empty()


@pytest.fixture
def enable_simulated_preview(monkeypatch):
    from functools import partial

    from custom_components.soyal_access import hub

    monkeypatch.setattr(
        hub,
        "Hub",
        partial(hub.Hub, simulate_preview=True),
    )


async def test_node02_readonly_event_head_deduplication(hass, enable_simulated_preview):
    from custom_components.soyal_access.diagnostics import (
        async_get_config_entry_diagnostics,
    )

    gateway = SimulatedGateway()
    async with gateway.listen() as port:
        entry = await setup_gateway(
            hass,
            port=port,
            stations="2",
            node02_event_preview=True,
            node02_event_limit=5,
        )
        manager = entry.runtime_data
        raw = encode(
            Frame(0, 0x0B, bytes([2, 5, 4, 3, 2, 1, 11, 26, 7, 0, 20]) + bytes(14))
        )
        for _ in range(2):
            gateway.responses.put_nowait((raw[:8], raw[8:]))
            request = await asyncio.wait_for(gateway.requests.get(), 2)
            assert request == Frame(2, 0x25)
        await asyncio.sleep(0.05)
        page = await manager.events.history_page({}, None, 10)
        assert len(page["rows"]) == 1
        assert page["rows"][0]["user_address"] == 20
        assert page["rows"][0]["controller_time"] == "2026-11-01T03:04:05"
        assert manager.preview.duplicates == 1
        diagnostics = await async_get_config_entry_diagnostics(hass, entry)
        assert diagnostics["transactions"] == 2
        assert manager.preview.status == "duplicate_head"
        assert manager.preview.attempts == 2
        assert entry.data["node02_event_preview"] is False
        await asyncio.sleep(0.55)
        assert gateway.requests.empty()
        assert await hass.config_entries.async_reload(entry.entry_id)
        await hass.async_block_till_done()
        await asyncio.sleep(0.55)
        assert entry.runtime_data.preview.status == "disabled"
        assert gateway.requests.empty()


@pytest.mark.parametrize(
    ("response", "result"),
    [
        (acknowledge(2), "empty_ack_unverified"),
        (acknowledge(2, 5), "nack"),
        (encode(Frame(0, 0x7F, b"\x02")), "query_failed_ConnectionError"),
    ],
)
async def test_preview_reply_classification_and_control(
    hass, response, result, enable_simulated_preview
):
    gateway = SimulatedGateway()
    async with gateway.listen() as port:
        gateway.responses.put_nowait((response,))
        entry = await setup_gateway(
            hass,
            port=port,
            stations="2",
            enable_control=True,
            node02_event_preview=True,
            node02_event_limit=5,
        )
        manager = entry.runtime_data
        assert await asyncio.wait_for(gateway.requests.get(), 2) == Frame(2, 0x25)
        await asyncio.sleep(0.05)
        assert manager.preview.status == result
        assert manager.preview.attempts == 1
        if result.startswith("query_failed"):
            assert manager.control.uncertain == {2}
            with pytest.raises(HomeAssistantError, match="blocked"):
                await manager.control.execute(2, 0x84)
        else:
            assert not manager.control.uncertain
            gateway.responses.put_nowait((io_reply(2),))
            await manager.control.execute(2, 0x84)
            assert gateway.requests.get_nowait() == Frame(2, 0x21, b"\x84")
        await asyncio.sleep(0.55)
        assert gateway.requests.empty()


async def test_preview_disabled_by_default_and_one_read_limit(
    hass, enable_simulated_preview
):
    gateway = SimulatedGateway()
    async with gateway.listen() as port:
        entry = await setup_gateway(hass, port=port, stations="2")
        await asyncio.sleep(0.55)
        assert gateway.requests.empty()
        assert entry.runtime_data.preview.status == "disabled"
        assert await hass.config_entries.async_unload(entry.entry_id)
        hass.config_entries.async_update_entry(
            entry, data=dict(entry.data) | {"node02_event_preview": True}
        )
        raw = encode(
            Frame(0, 0x10, bytes([2, 5, 4, 3, 2, 1, 11, 26, 7, 255, 255]) + bytes(14))
        )
        gateway.responses.put_nowait((raw,))
        assert await hass.config_entries.async_setup(entry.entry_id)
        assert await asyncio.wait_for(gateway.requests.get(), 2) == Frame(2, 0x25)
        await asyncio.sleep(0.55)
        assert entry.runtime_data.preview.status == "oldest_event_saved"
        assert entry.runtime_data.preview.attempts == 1
        assert gateway.requests.empty()
        gateway.responses.put_nowait((io_reply(2),))
        # Reconfigure enabling control should preserve the saved queue head.
        hass.config_entries.async_update_entry(
            entry, data=dict(entry.data) | {"enable_control": True}
        )
        assert await hass.config_entries.async_reload(entry.entry_id)
        await entry.runtime_data.control.execute(2, 0x84)
        assert gateway.requests.get_nowait() == Frame(2, 0x21, b"\x84")


async def test_event_preview_preserves_control_quarantine(
    hass, enable_simulated_preview
):
    gateway = SimulatedGateway()
    async with gateway.listen() as port:
        with patch(
            "custom_components.soyal_access.control.Store.async_load",
            new=AsyncMock(return_value={"uncertain": [2]}),
        ):
            entry = await setup_gateway(
                hass,
                port=port,
                stations="2",
                enable_control=True,
                node02_event_preview=True,
                node02_event_limit=5,
            )
        await asyncio.sleep(0.55)
        assert entry.runtime_data.preview.status == "uncertain"
        assert entry.runtime_data.preview.attempts == 0
        with pytest.raises(HomeAssistantError, match="blocked"):
            await entry.runtime_data.control.execute(2, 0x84)
        assert gateway.requests.empty()


async def test_stale_preview_flag_is_ignored_in_production(hass):
    gateway = SimulatedGateway()
    async with gateway.listen() as port:
        entry = await setup_gateway(
            hass, port=port, stations="2", node02_event_preview=True
        )
        manager = entry.runtime_data
        assert not manager.preview.enabled
        assert manager.preview.task is None
        assert (0x25, b"") not in manager.transport._allowed


async def test_recovery_requires_preparation_and_clears_only_target(
    hass, hass_ws_client
):
    gateway = SimulatedGateway()
    async with gateway.listen() as port:
        entry = await setup_gateway(
            hass, port=port, stations="1,2", enable_control=True
        )
        manager = entry.runtime_data
        manager.control.uncertain.update({1, 2})
        await manager.control._save()
        with pytest.raises(HomeAssistantError, match="Prepare"):
            await manager.control.confirm_recovery(2)
        assert await manager.control.prepare_recovery(2) == "legacy_unknown"
        assert manager.coordinator.task.done()
        assert manager.control.control_block_reason(1) == "recovery_paused"
        with pytest.raises(HomeAssistantError, match="Prepare"):
            await manager.control.confirm_recovery(1)
        admin = await hass_ws_client()
        command = {
            "type": "soyal_access/recover_station",
            "entry_id": entry.entry_id,
            "station": 2,
            "step": "confirm",
        }
        await admin.send_json_auto_id(command)
        assert not (await admin.receive_json())["success"]
        await admin.send_json_auto_id(
            command | {"drain_evidence": "Simulated drain verification record"}
        )
        assert (await admin.receive_json())["success"]
        assert manager.control.uncertain == {1}
        assert gateway.requests.empty()
        assert (await manager.control._store.async_load())["uncertain"] == [1]


async def test_control_priority_over_health_poll(hass):
    gateway = SimulatedGateway(persistent=True)
    async with gateway.listen() as port:
        entry = await setup_gateway(hass, port=port, enable_control=True)
        hub = entry.runtime_data
        first = asyncio.create_task(hub.control.execute(1, 0x84))
        assert await asyncio.wait_for(gateway.requests.get(), 1) == Frame(
            1, 0x21, b"\x84"
        )
        second = asyncio.create_task(hub.control.execute(2, 0x82))
        await asyncio.sleep(0)
        assert hub.transport.waiting == 1
        await hub.coordinator.poll_once(hub.transport, hub.control)
        gateway.responses.put_nowait((io_reply(1),))
        assert await asyncio.wait_for(gateway.requests.get(), 1) == Frame(
            2, 0x21, b"\x82"
        )
        gateway.responses.put_nowait((io_reply(2),))
        await asyncio.gather(first, second)
        assert gateway.requests.empty()

        gateway.responses.put_nowait((io_reply(2),))
        await hub.coordinator.poll_once(hub.transport, hub.control)
        assert gateway.requests.get_nowait() == Frame(2, 0x21, b"\x00\x00")
        assert not hub.control.uncertain
