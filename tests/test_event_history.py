"""Access history read-only queries, entity refresh and Recorder exclusion."""

import asyncio
import json
import sqlite3
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from zoneinfo import ZoneInfo

import pytest
from homeassistant.components.recorder.db_schema import StateAttributes
from homeassistant.core import Context, Event
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from homeassistant.setup import async_setup_component
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.soyal_access.const import DOMAIN
from custom_components.soyal_access.fifo import parse_event
from custom_components.soyal_access.fifo_storage import FifoStorage
from custom_components.soyal_access.hub import Hub
from custom_components.soyal_access.protocol import decode
from custom_components.soyal_access.transport import NotSentError
from tests.test_fifo_events import event_frame
from tests.test_ha import gateway, setup_entry
from tests.test_stage5 import eventually

pytestmark = pytest.mark.usefixtures("enable_custom_integrations", "socket_enabled")


def test_history_normalizes_only_legacy_invalid_user_pin(tmp_path):
    storage = FifoStorage(tmp_path / "events.sqlite3")
    storage.initialize()
    event = {
        "node_id": 2,
        "event_code": 1,
        "event_type": "unknown_event",
        "event_time_local": None,
        "port": 1,
        "user_address": 42,
        "raw_frame_hex": "01",
    }
    storage.save_event(event, "legacy-1")
    storage.save_event({**event, "event_code": 2}, "legacy-2")
    storage.save_event({**event, "event_type": "pin_access"}, "classified")

    count, events = storage.read_history(2)

    assert count == 3
    assert [row["event_type"] for row in events] == [
        "pin_access",
        "unknown_event",
        "invalid_user_pin",
    ]
    with sqlite3.connect(storage.path) as db:
        assert (
            db.execute(
                "SELECT event_type FROM access_events WHERE event_code = 1"
            ).fetchone()[0]
            == "unknown_event"
        )


@pytest.fixture(autouse=True)
def skip_network_polling(monkeypatch):
    monkeypatch.setattr(
        "custom_components.soyal_access.hub.Hub.start_polling", lambda self: None
    )


async def test_history_refresh_reload_and_recorder(hass):
    assert await async_setup_component(hass, "homeassistant", {})
    async with gateway() as (port, _, _):
        entry = await setup_entry(hass, port)
        hub = entry.runtime_data
        entity_id = er.async_get(hass).async_get_entity_id(
            "sensor", DOMAIN, f"{entry.entry_id}:2:access_event_history"
        )
        gateway_device = dr.async_get(hass).async_get_device_by_identifier(
            (DOMAIN, entry.entry_id), entry.entry_id
        )
        sensor_device = dr.async_get(hass).async_get_device_by_identifier(
            (DOMAIN, f"{entry.entry_id}:2"), entry.entry_id
        )
        sensor_entity = er.async_get(hass).async_get(entity_id)
        assert gateway_device is not None
        assert sensor_device is not None
        assert sensor_device.id != gateway_device.id
        assert sensor_device.via_device_id == gateway_device.id
        assert sensor_entity.device_id == sensor_device.id
        assert hass.states.get(entity_id).state == "0"
        assert hass.states.get(entity_id).attributes["events"] == []
        event = parse_event(decode(event_frame()), 2)

        def populate():
            for i in range(105):
                hub.fifo_storage.save_event(event, f"received-{i}")

        await hass.async_add_executor_job(populate)
        await hass.services.async_call(
            "homeassistant", "update_entity", {"entity_id": entity_id}, blocking=True
        )
        before = hub.fifo_storage.path.read_bytes()
        state = hass.states.get(entity_id)
        assert state.state == "105"
        rows = state.attributes["events"]
        assert len(rows) == 100
        assert [row["id"] for row in rows] == list(range(105, 5, -1))
        assert set(rows[0]) == {
            "id",
            "node_id",
            "received_at",
            "event_time_local",
            "event_type",
            "port",
            "user_address",
            "source",
            "ha_user_id",
            "ha_user_name",
        }
        assert rows[0]["source"] == "soyal"
        assert rows[0]["ha_user_id"] is None
        assert rows[0]["ha_user_name"] is None
        encoded = StateAttributes.shared_attrs_bytes_from_event(
            Event("state_changed", {"new_state": state}), None
        )
        assert "events" not in json.loads(encoded)
        assert hub.fifo_storage.path.read_bytes() == before

        with patch.object(
            hub.transport, "read_fifo_event", return_value=decode(event_frame())
        ):
            hub._validation_polls = 2
            await hub._read_fifo_event()
        await eventually(lambda: hass.states.get(entity_id).state == "106")
        assert await hass.config_entries.async_reload(entry.entry_id)
        await hass.async_block_till_done()
        assert hass.states.get(entity_id).state == "106"
        assert len(hass.states.get(entity_id).attributes["events"]) == 100
        assert await hass.config_entries.async_unload(entry.entry_id)


async def test_missing_and_unreadable_history(hass):
    assert await async_setup_component(hass, "homeassistant", {})
    async with gateway() as (port, _, _):
        entry = await setup_entry(hass, port)
        hub = entry.runtime_data
        entity_id = er.async_get(hass).async_get_entity_id(
            "sensor", DOMAIN, f"{entry.entry_id}:2:access_event_history"
        )
        hub.fifo_storage.path.unlink()
        await hass.services.async_call(
            "homeassistant", "update_entity", {"entity_id": entity_id}, blocking=True
        )
        assert hass.states.get(entity_id).state == "0"
        assert not hub.fifo_storage.path.exists()
        with patch.object(
            hub.fifo_storage, "read_history", side_effect=sqlite3.OperationalError
        ):
            await hass.services.async_call(
                "homeassistant",
                "update_entity",
                {"entity_id": entity_id},
                blocking=True,
            )
        assert hass.states.get(entity_id).state == "unavailable"
        assert hub.transport.connected
        await hass.services.async_call(
            "homeassistant", "update_entity", {"entity_id": entity_id}, blocking=True
        )
        assert hass.states.get(entity_id).state == "0"
        assert await hass.config_entries.async_unload(entry.entry_id)


def test_schema_upgrade_preserves_rows_ids_and_legacy_queries(tmp_path):
    storage = FifoStorage(tmp_path / "events.sqlite3")
    with sqlite3.connect(storage.path) as db:
        db.execute("""CREATE TABLE access_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT, received_at TEXT NOT NULL,
            node_id INTEGER NOT NULL, event_code INTEGER NOT NULL,
            event_type TEXT NOT NULL, event_time_local TEXT,
            port INTEGER NOT NULL, user_address INTEGER NOT NULL,
            raw_frame_hex TEXT NOT NULL)""")
        db.execute("""INSERT INTO access_events VALUES
            (42, 'legacy', 2, 1, 'unknown_event', NULL, 1, 123, '01')""")
    storage.initialize()
    storage.initialize()
    fresh = FifoStorage(tmp_path / "fresh.sqlite3")
    fresh.initialize()
    with sqlite3.connect(storage.path) as db, sqlite3.connect(fresh.path) as new_db:
        assert (
            db.execute("PRAGMA table_info(access_events)").fetchall()
            == new_db.execute("PRAGMA table_info(access_events)").fetchall()
        )
        assert db.execute(
            "SELECT id, node_id, event_code, user_address, raw_frame_hex FROM access_events"
        ).fetchall() == [(42, 2, 1, 123, "01")]
        assert db.execute(
            "SELECT source, ha_user_id, ha_user_name FROM access_events"
        ).fetchone() == ("soyal", None, None)
    storage.save_ha_event(2, "ha_pulse_open", "utc", "local", "user-1", "測試者")
    storage.save_event(parse_event(decode(event_frame()), 2), "fifo")
    count, rows = storage.read_history(2)
    assert count == 3
    assert [row["id"] for row in rows] == [44, 43, 42]
    assert [row["source"] for row in rows] == ["soyal", "ha", "soyal"]
    assert rows[1]["ha_user_name"] == "測試者"
    assert rows[2]["event_type"] == "invalid_user_pin"
    with sqlite3.connect(storage.path) as db:
        assert db.execute(
            "SELECT node_id, event_code, port, user_address, raw_frame_hex FROM access_events WHERE source = 'ha'"
        ).fetchone() == (2, -1, -1, -1, "")


@pytest.fixture
async def control_hub(hass):
    entry = MockConfigEntry(domain=DOMAIN, data={"host": "127.0.0.1", "port": 502})
    hub = Hub(hass, entry, "gateway-device-id")
    hub.transport = SimpleNamespace(
        connected=True,
        tainted=False,
        control_node=AsyncMock(return_value=4),
        close=AsyncMock(),
    )
    hub._validation_polls = 2
    await hub.initialize_fifo_storage()
    await hub.hold_open_timeout.async_load()
    yield hub
    await hub.stop()


async def test_success_history_context_and_timeout_classification(hass, control_hub):
    from custom_components.soyal_access.button import PulseButton
    from custom_components.soyal_access.switch import HoldOpenSwitch

    hub = control_hub
    button, switch = PulseButton(hub), HoldOpenSwitch(hub)
    button.async_set_context(Context(user_id="user-1"))
    switch.async_set_context(Context(user_id="user-1"))
    actor = SimpleNamespace(name="操作時姓名")

    async def lookup(user_id):
        assert not hub.busy and not hub._transaction.locked()
        assert user_id == "user-1"
        # Entity contexts can change while this operation awaits history I/O.
        button.async_set_context(Context(user_id="other-user"))
        return actor

    with (
        patch.object(dt_util, "DEFAULT_TIME_ZONE", ZoneInfo("Asia/Taipei")),
        patch.object(hass.auth, "async_get_user", side_effect=lookup) as auth,
    ):
        await button.async_press()
        await switch.async_turn_on()
        await switch.async_turn_off()
        await hub.set_hold_open(True)
        # Use the real timeout callback and a due deadline without waiting.
        manager = hub.hold_open_timeout
        manager.started_at = dt_util.utcnow() - timedelta(minutes=30)
        manager.deadline = manager.started_at + timedelta(minutes=30)
        manager._reschedule()
        await manager._task
        assert auth.call_count == 3
    actor.name = "已變更姓名"
    count, rows = await hass.async_add_executor_job(
        hub.fifo_storage.read_history, hub.node_id
    )
    assert count == 5
    assert [row["event_type"] for row in reversed(rows)] == [
        "ha_pulse_open",
        "ha_hold_open",
        "ha_release_open",
        "ha_hold_open",
        "ha_hold_open_timeout",
    ]
    assert rows[0]["ha_user_id"] is None and rows[0]["ha_user_name"] == "系統"
    assert rows[1]["ha_user_id"] is None and rows[1]["ha_user_name"] is None
    assert [(row["ha_user_id"], row["ha_user_name"]) for row in rows[2:]] == [
        ("user-1", "操作時姓名")
    ] * 3
    assert all(row["source"] == "ha" for row in rows)
    assert all(row["event_time_local"].endswith("+08:00") for row in rows)
    assert all(row["received_at"].endswith("+00:00") for row in rows)
    assert not hub._fifo_delete_pending


@pytest.mark.parametrize(
    "failure", [5, NotSentError(), TimeoutError(), asyncio.CancelledError()]
)
async def test_failed_controls_do_not_write_history(control_hub, failure):
    hub = control_hub
    if failure == 5:
        hub.transport.control_node.return_value = 5
    else:
        hub.transport.control_node.side_effect = failure
    with pytest.raises((HomeAssistantError, asyncio.CancelledError)):
        await hub.pulse(user_id="not-looked-up")
    assert await hub._hass.async_add_executor_job(
        hub.fifo_storage.read_history, hub.node_id
    ) == (
        0,
        [],
    )


async def test_history_failure_and_user_lookup_do_not_block_next_control(
    hass, control_hub, caplog
):
    hub = control_hub
    looking_up, finish_lookup = asyncio.Event(), asyncio.Event()

    async def lookup(_user_id):
        assert not hub.busy and not hub._transaction.locked()
        looking_up.set()
        await finish_lookup.wait()

    with patch.object(hass.auth, "async_get_user", side_effect=lookup):
        first = asyncio.create_task(hub.pulse(user_id="missing-user"))
        await looking_up.wait()
        await hub.pulse()
        finish_lookup.set()
        await first
    _, rows = await hass.async_add_executor_job(
        hub.fifo_storage.read_history, hub.node_id
    )
    assert len(rows) == 2
    assert all(
        row["ha_user_id"] is None and row["ha_user_name"] is None for row in rows
    )

    def fail_save(*_args):
        assert not hub.busy and not hub._transaction.locked()
        raise sqlite3.OperationalError("disk full")

    with patch.object(hub.fifo_storage, "save_ha_event", side_effect=fail_save):
        await hub.set_hold_open(True)
    assert hub.hold_open is True
    assert hub.hold_open_timeout.status == "active"
    assert hub.transport.control_node.call_count == 3
    assert "history save failed (control succeeded)" in caplog.text


async def test_ha_save_notifies_existing_sensor(hass):
    async with gateway() as (port, _, _):
        entry = await setup_entry(hass, port)
        hub = entry.runtime_data
        entity_id = er.async_get(hass).async_get_entity_id(
            "sensor", DOMAIN, f"{entry.entry_id}:2:access_event_history"
        )
        hub._validation_polls = 2
        with patch.object(hub.transport, "control_node", return_value=4):
            await hub.pulse()
        await eventually(lambda: hass.states.get(entity_id).state == "1")
        row = hass.states.get(entity_id).attributes["events"][0]
        assert row["event_type"] == "ha_pulse_open" and row["source"] == "ha"
        assert row["ha_user_id"] is None and row["ha_user_name"] is None
        assert not hub._fifo_delete_pending
        assert await hass.config_entries.async_unload(entry.entry_id)
