"""Stage 8B wire and durable-journal tests against a synthetic controller."""

import asyncio
import sqlite3

import pytest
from homeassistant.helpers import entity_registry as er

from custom_components.soyal_access.const import DOMAIN
from custom_components.soyal_access.fifo_sync import ENABLE_KEY
from tests.test_fifo import ACK, FIFO, synthetic_event
from tests.test_ha import next_item, setup_entry
from tests.test_stage5 import IO, NACK, POLL, eventually, gateway, send, validate
from tests.test_storage import observation, rows

pytestmark = pytest.mark.usefixtures("enable_custom_integrations", "socket_enabled")
POP = bytes.fromhex("7E 04 02 37 CA 03")
FIELD_POP_ACK = bytes.fromhex("7E 0A 00 04 02 34 10 00 18 73 B6 8B")


@pytest.fixture(autouse=True)
def faster_sync(monkeypatch):
    # Keep production poll spacing, shorten the one-second idle/reconnect delays.
    monkeypatch.setattr("custom_components.soyal_access.fifo_sync.IDLE_DELAY", 0.28)
    monkeypatch.setattr("custom_components.soyal_access.hub.RECONNECT_DELAYS", (0.1,))


async def next_command(writer, requests):
    async with asyncio.timeout(2):
        while True:
            _, packet = await requests.get()
            if packet != POLL:
                return packet
            await send(writer, IO)


async def no_commands(writer, requests, duration=0.35):
    with pytest.raises(TimeoutError):
        async with asyncio.timeout(duration):
            await next_command(writer, requests)


async def read_head(writer, requests, reply):
    assert await next_command(writer, requests) == FIFO
    await send(writer, reply)


async def expect_saved_pop(hub, writer, requests, raw):
    await read_head(writer, requests, raw)
    # The second read verifies the saved head immediately before destructive I/O.
    await read_head(writer, requests, raw)
    assert await next_command(writer, requests) == POP
    pending = await hub.fifo_storage.progress()
    assert pending[1:] == ("pop_uncertain", raw)
    return pending[0]


async def toggle(hass, entry, enabled):
    entity = er.async_get(hass).async_get_entity_id(
        "switch", DOMAIN, f"{entry.entry_id}:2:fifo_auto_sync"
    )
    await hass.services.async_call(
        "switch",
        "turn_on" if enabled else "turn_off",
        {"entity_id": entity},
        blocking=True,
    )


async def test_sync_disabled_empty_nack_identical_heads_and_legacy_migration(
    hass, tmp_path
):
    hass.config.config_dir = str(tmp_path)
    async with gateway(allow_fifo=True, allow_pop=True) as (port, connections):
        entry = await setup_entry(hass, port, timeout=0.35)
        hub = entry.runtime_data
        writer, requests, _ = await next_item(connections)
        await validate(hub, writer, requests)
        assert not hub.fifo_sync.enabled
        assert not hub.fifo_storage.path.exists()
        await hub.fifo_storage.save(observation())
        await no_commands(writer, requests)
        await toggle(hass, entry, True)
        assert entry.data[ENABLE_KEY] is True
        await read_head(writer, requests, ACK)
        await read_head(writer, requests, NACK)
        await eventually(lambda: hub.fifo_sync.state == "paused")
        assert rows(
            hub.fifo_storage.path, "SELECT COUNT(*) FROM fifo_access_events"
        ) == [(0,)]

        same = synthetic_event(0x1C)
        first = await expect_saved_pop(hub, writer, requests, same)
        await send(writer, FIELD_POP_ACK)
        second = await expect_saved_pop(hub, writer, requests, same)
        assert first != second
        await toggle(hass, entry, False)  # Already sent: still finish the ACK commit.
        await send(writer, FIELD_POP_ACK)
        await eventually(lambda: not hub.fifo_operation.locked())
        assert rows(
            hub.fifo_storage.path,
            """
            SELECT sequence_number, consumption_status, raw_frame FROM fifo_access_events ORDER BY id
        """,
        ) == [(1, "popped_confirmed", same), (2, "popped_confirmed", same)]
        assert rows(
            hub.fifo_storage.path, "SELECT read_count FROM fifo_observations"
        ) == [(1,)]
        assert hub.fifo_result == "not_queried"
        await no_commands(writer, requests)
        task = hub.fifo_sync.task
        assert await hass.config_entries.async_unload(entry.entry_id)
        assert task.done()


@pytest.mark.parametrize(
    "phase", ["insert", "commit", "intent", "ack_commit", "changed_head"]
)
async def test_failures_do_not_delete_unsaved_heads(hass, tmp_path, monkeypatch, phase):
    hass.config.config_dir = str(tmp_path)
    async with gateway(allow_fifo=True, allow_pop=True) as (port, connections):
        entry = await setup_entry(hass, port, timeout=0.35)
        hub = entry.runtime_data
        writer, requests, _ = await next_item(connections)
        await validate(hub, writer, requests)
        storage = hub.fifo_storage
        await storage.progress()
        if phase in {"insert", "commit"}:
            connect = sqlite3.connect

            def failing_connect(*args, **kwargs):
                db = connect(*args, **kwargs)
                # Fail only the save transaction, not schema/progress reads.
                wrote_event = False

                def authorize(action, arg, *_):
                    nonlocal wrote_event
                    if action == sqlite3.SQLITE_INSERT and arg == "fifo_access_events":
                        wrote_event = True
                        if phase == "insert":
                            return sqlite3.SQLITE_DENY
                    if (
                        phase == "commit"
                        and wrote_event
                        and action == sqlite3.SQLITE_TRANSACTION
                        and arg == "COMMIT"
                    ):
                        return sqlite3.SQLITE_DENY
                    return sqlite3.SQLITE_OK

                db.set_authorizer(authorize)
                return db

            monkeypatch.setattr(sqlite3, "connect", failing_connect)
        elif phase in {"intent", "ack_commit"}:
            transition = storage.transition

            async def fail_transition(event_id, previous, status, now, evidence=None):
                target = "pop_uncertain" if phase == "intent" else "popped_confirmed"
                if status == target:
                    raise sqlite3.OperationalError("injected transition failure")
                await transition(event_id, previous, status, now, evidence)

            monkeypatch.setattr(storage, "transition", fail_transition)

        raw = synthetic_event()
        await toggle(hass, entry, True)
        await read_head(writer, requests, raw)
        if phase == "ack_commit":
            await read_head(writer, requests, raw)
            assert await next_command(writer, requests) == POP
            await send(writer, FIELD_POP_ACK)
            expected = "pop_uncertain"
        elif phase == "changed_head":
            await read_head(writer, requests, synthetic_event(0x10))
            expected = "paused"
        else:
            expected = "storage_error"
        await eventually(lambda: hub.fifo_sync.state == expected)
        await toggle(hass, entry, False)
        assert hub.can_pulse and hub.node_online
        await no_commands(writer, requests)
        pending = await storage.progress()
        if phase == "ack_commit":
            assert pending[1] == "pop_uncertain"
        elif phase in {"intent", "changed_head"}:
            assert pending[1] == "saved"
        else:
            assert pending is None
            assert rows(storage.path, "SELECT COUNT(*) FROM fifo_access_events") == [
                (0,)
            ]
        assert await hass.config_entries.async_unload(entry.entry_id)


@pytest.mark.parametrize(
    "loss", ["timeout", "disconnect", "nack", "reload_after_send", "late_read_ack"]
)
async def test_unconfirmed_removal_is_never_replayed(hass, tmp_path, loss):
    hass.config.config_dir = str(tmp_path)
    async with gateway(allow_fifo=True, allow_pop=True) as (port, connections):
        entry = await setup_entry(hass, port, timeout=0.35)
        hub = entry.runtime_data
        writer, requests, closed = await next_item(connections)
        await validate(hub, writer, requests)
        await toggle(hass, entry, True)
        raw = synthetic_event()
        event_id = await expect_saved_pop(hub, writer, requests, raw)
        if loss == "disconnect":
            writer.close()
        elif loss == "nack":
            await send(writer, NACK)
        elif loss == "late_read_ack":
            await send(writer, ACK)
        elif loss == "reload_after_send":
            old_task = hub.fifo_sync.task
            assert await hass.config_entries.async_reload(entry.entry_id)
            await hass.async_block_till_done()
            assert old_task.done()

        if loss != "nack":
            await asyncio.wait_for(closed.wait(), 2)
            writer, requests, _ = await next_item(connections)
            hub = entry.runtime_data
            await validate(hub, writer, requests)
        else:
            await eventually(lambda: hub.fifo_sync.state == "pop_uncertain")

        await read_head(writer, requests, raw)
        await read_head(writer, requests, ACK)
        assert (await hub.fifo_storage.progress())[:2] == (event_id, "pop_uncertain")
        assert hub.can_pulse and hub.fifo_sync.enabled
        # A one-off different frame, followed by the old head, cannot resolve it.
        different = synthetic_event(0x10)
        await read_head(writer, requests, different)
        await read_head(writer, requests, raw)
        assert (await hub.fifo_storage.progress())[:2] == (event_id, "pop_uncertain")
        await read_head(writer, requests, different)
        assert (await hub.fifo_storage.progress())[:2] == (event_id, "pop_uncertain")
        await read_head(writer, requests, different)
        await eventually(
            lambda: (
                hub.fifo_sync.attributes.get("last_resolution") == "advanced_observed"
            )
        )
        await toggle(hass, entry, False)
        assert await hub.fifo_storage.progress() is None
        assert rows(
            hub.fifo_storage.path, "SELECT consumption_status FROM fifo_access_events"
        ) == [("advanced_observed",)]
        await no_commands(writer, requests)
        assert await hass.config_entries.async_unload(entry.entry_id)


@pytest.mark.parametrize("durable_state", ["saved", "pop_uncertain"])
async def test_persisted_progress_is_read_before_first_removal(
    hass, tmp_path, durable_state
):
    hass.config.config_dir = str(tmp_path)
    async with gateway(allow_fifo=True, allow_pop=True) as (port, connections):
        entry = await setup_entry(hass, port, timeout=0.35)
        hub = entry.runtime_data
        writer, requests, _ = await next_item(connections)
        await validate(hub, writer, requests)
        event = observation()
        event_id = await hub.fifo_storage.save_head(event)
        raw = bytes.fromhex(event["raw_frame_hex"])
        if durable_state == "pop_uncertain":
            # Crash after the durable intent, before reaching the socket.
            await hub.fifo_storage.transition(
                event_id, "saved", durable_state, event["read_at"]
            )
        assert await hass.config_entries.async_reload(entry.entry_id)
        await hass.async_block_till_done()
        hub = entry.runtime_data
        writer, requests, _ = await next_item(connections)
        await validate(hub, writer, requests)
        await toggle(hass, entry, True)
        await read_head(writer, requests, raw)
        if durable_state == "saved":
            await read_head(writer, requests, raw)
            assert await next_command(writer, requests) == POP
            await send(writer, FIELD_POP_ACK)
            await eventually(lambda: hub.fifo_sync.state == "idle")
        else:
            await read_head(writer, requests, raw)
            assert (await hub.fifo_storage.progress())[:2] == (event_id, durable_state)
        await toggle(hass, entry, False)
        assert rows(hub.fifo_storage.path, "SELECT id FROM fifo_access_events") == [
            (event_id,)
        ]
        assert await hass.config_entries.async_unload(entry.entry_id)


async def test_manual_and_control_take_priority_over_unsent_pop(hass, tmp_path):
    hass.config.config_dir = str(tmp_path)
    async with gateway(allow_fifo=True, allow_pop=True) as (port, connections):
        entry = await setup_entry(hass, port, timeout=1)
        hub = entry.runtime_data
        writer, requests, _ = await next_item(connections)
        await validate(hub, writer, requests)
        raw = synthetic_event()
        await toggle(hass, entry, True)
        await read_head(writer, requests, raw)
        assert await next_command(writer, requests) == FIFO

        manual = asyncio.create_task(hub.read_oldest_event())
        await eventually(lambda: hub.fifo_pending)
        control = asyncio.create_task(hub.pulse())
        await eventually(lambda: hub.busy)
        await send(writer, raw)
        assert await next_command(writer, requests) == bytes.fromhex(
            "7E 05 02 21 84 58 FF"
        )
        await send(writer, ACK)
        await control
        assert await next_command(writer, requests) == FIFO
        await send(writer, raw)
        await manual
        assert hub.fifo_attributes["storage_status"] == "saved"
        assert (await hub.fifo_storage.progress())[1] == "saved"
        await toggle(hass, entry, False)
        await no_commands(writer, requests)
        assert await hass.config_entries.async_unload(entry.entry_id)


async def test_disabling_during_recheck_prevents_pop(hass, tmp_path):
    hass.config.config_dir = str(tmp_path)
    async with gateway(allow_fifo=True, allow_pop=True) as (port, connections):
        entry = await setup_entry(hass, port, timeout=0.35)
        hub = entry.runtime_data
        writer, requests, _ = await next_item(connections)
        await validate(hub, writer, requests)
        raw = synthetic_event()
        await toggle(hass, entry, True)
        await read_head(writer, requests, raw)
        assert await next_command(writer, requests) == FIFO
        await toggle(hass, entry, False)
        await send(writer, raw)
        await eventually(lambda: not hub.fifo_operation.locked())
        assert (await hub.fifo_storage.progress())[1:] == ("saved", raw)
        assert hub.fifo_sync.state == "disabled"
        await no_commands(writer, requests)
        assert await hass.config_entries.async_unload(entry.entry_id)


async def test_endpoint_change_disables_sync_and_preserves_journal(hass, tmp_path):
    from homeassistant.config_entries import SOURCE_RECONFIGURE

    hass.config.config_dir = str(tmp_path)
    async with (
        gateway(allow_fifo=True, allow_pop=True) as (port, connections),
        gateway(allow_fifo=True, allow_pop=True) as (other_port, other),
    ):
        entry = await setup_entry(hass, port, timeout=0.35)
        hub = entry.runtime_data
        writer, requests, _ = await next_item(connections)
        await validate(hub, writer, requests)
        event = observation()
        event_id = await hub.fifo_storage.save_head(event)
        await hub.fifo_storage.transition(
            event_id, "saved", "pop_uncertain", event["read_at"]
        )
        await toggle(hass, entry, True)
        flow = await hass.config_entries.flow.async_init(
            DOMAIN, context={"source": SOURCE_RECONFIGURE, "entry_id": entry.entry_id}
        )
        result = await hass.config_entries.flow.async_configure(
            flow["flow_id"], {"host": "127.0.0.1", "port": other_port}
        )
        assert result["reason"] == "reconfigure_successful"
        await hass.async_block_till_done()
        assert not entry.runtime_data.fifo_sync.enabled
        writer, requests, _ = await next_item(other)
        await validate(entry.runtime_data, writer, requests)
        await toggle(hass, entry, True)
        await eventually(lambda: entry.runtime_data.fifo_sync.state == "storage_error")
        await no_commands(writer, requests)
        assert rows(
            hub.fifo_storage.path, "SELECT consumption_status FROM fifo_access_events"
        ) == [("pop_uncertain",)]
        assert await hass.config_entries.async_unload(entry.entry_id)


async def test_empty_after_one_event_does_not_send_another_removal(hass, tmp_path):
    hass.config.config_dir = str(tmp_path)
    async with gateway(allow_fifo=True, allow_pop=True) as (port, connections):
        entry = await setup_entry(hass, port, timeout=0.35)
        hub = entry.runtime_data
        writer, requests, _ = await next_item(connections)
        await validate(hub, writer, requests)
        await toggle(hass, entry, True)
        await expect_saved_pop(hub, writer, requests, synthetic_event())
        await send(writer, FIELD_POP_ACK)
        # Simulate a controlled test FIFO that contained exactly one event.
        await read_head(writer, requests, ACK)
        await read_head(writer, requests, ACK)
        await toggle(hass, entry, False)
        await no_commands(writer, requests)
        assert rows(
            hub.fifo_storage.path, "SELECT consumption_status FROM fifo_access_events"
        ) == [("popped_confirmed",)]
        assert await hass.config_entries.async_unload(entry.entry_id)
