"""Access history read-only queries, entity refresh and Recorder exclusion."""

import json
import sqlite3
from unittest.mock import patch

import pytest
from homeassistant.components.recorder.db_schema import StateAttributes
from homeassistant.core import Event
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from homeassistant.setup import async_setup_component

from custom_components.soyal_access.const import DOMAIN
from custom_components.soyal_access.fifo import parse_event
from custom_components.soyal_access.protocol import decode
from tests.test_fifo_events import event_frame
from tests.test_ha import gateway, setup_entry
from tests.test_stage5 import eventually

pytestmark = pytest.mark.usefixtures("enable_custom_integrations", "socket_enabled")


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
        event = parse_event(decode(event_frame()))

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
            "received_at",
            "event_time_local",
            "event_type",
            "port",
            "user_address",
        }
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
