"""Stage 8B wire and durable-journal tests against a synthetic controller."""

import asyncio
import sqlite3

import pytest
from homeassistant.helpers import entity_registry as er

from custom_components.soyal_access.const import DOMAIN
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
        await hub.fifo_storage.set_pause(None)
        await toggle(hass, entry, True)
        assert hub.fifo_sync.enabled
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
        await hub.fifo_storage.set_pause(None)
        await toggle(hass, entry, True)
        await read_head(writer, requests, raw)
        if phase == "ack_commit":
            await read_head(writer, requests, raw)
            assert await next_command(writer, requests) == POP
            await send(writer, FIELD_POP_ACK)
            expected = "paused"
        elif phase == "changed_head":
            await read_head(writer, requests, synthetic_event(0x10))
            expected = "paused"
        else:
            expected = "paused"
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
        await hub.fifo_storage.set_pause(None)
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
        await eventually(lambda: hub.fifo_sync.state == "paused")
        assert hub.fifo_sync.enabled and hub.can_pulse
        assert (await hub.fifo_storage.progress())[:2] == (event_id, "pop_uncertain")
        await no_commands(writer, requests)
        await toggle(hass, entry, False)
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
        await hub.fifo_storage.set_pause(None)
        await toggle(hass, entry, True)
        if durable_state == "saved":
            await read_head(writer, requests, raw)
            await read_head(writer, requests, raw)
            assert await next_command(writer, requests) == POP
            await send(writer, FIELD_POP_ACK)
            await eventually(lambda: hub.fifo_sync.state == "idle")
        else:
            await eventually(lambda: hub.fifo_sync.state == "paused")
            await no_commands(writer, requests)
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
        await hub.fifo_storage.set_pause(None)
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
        await hub.fifo_storage.set_pause(None)
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
        await eventually(lambda: entry.runtime_data.fifo_sync.state == "paused")
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
        await hub.fifo_storage.set_pause(None)
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


@pytest.mark.parametrize(
    "mode",
    ["empty", "success", "short_ack", "timeout", "mismatch", "save", "intent", "auto"],
)
async def test_single_fifo_press(hass, tmp_path, mode, monkeypatch):
    from homeassistant.exceptions import HomeAssistantError

    hass.config.config_dir = str(tmp_path)
    async with gateway(allow_fifo=True, allow_pop=True) as (port, connections):
        entry = await setup_entry(hass, port, timeout=0.35)
        hub = entry.runtime_data
        writer, requests, _ = await next_item(connections)
        await validate(hub, writer, requests)
        entity = er.async_get(hass).async_get_entity_id(
            "button", DOMAIN, f"{entry.entry_id}:2:sync_one_fifo_event"
        )

        async def press():
            await hass.services.async_call(
                "button", "press", {"entity_id": entity}, blocking=True
            )

        if mode == "auto":
            await toggle(hass, entry, True)
            with pytest.raises(HomeAssistantError, match="must be disabled"):
                await press()
            return

        async def fail(*args):
            raise sqlite3.OperationalError("simulated failure")

        if mode in ("save", "intent"):
            monkeypatch.setattr(
                hub.fifo_storage, "save_head" if mode == "save" else "transition", fail
            )
        raw = synthetic_event()
        for _ in range(2 if mode == "success" else 1):
            task = asyncio.create_task(press())
            assert await next_command(writer, requests) == FIFO
            with pytest.raises(HomeAssistantError, match="already running"):
                await press()
            await send(writer, ACK if mode == "empty" else raw)
            if mode == "mismatch":
                await read_head(writer, requests, synthetic_event(0x10))
            elif mode not in ("empty", "save", "intent"):
                await read_head(writer, requests, raw)
                assert await next_command(writer, requests) == POP
                if mode != "timeout":
                    await send(writer, ACK if mode == "short_ack" else FIELD_POP_ACK)
            if mode in ("short_ack", "timeout", "mismatch", "save", "intent"):
                with pytest.raises(HomeAssistantError):
                    await task
            else:
                await task
            assert not hub.fifo_sync.enabled
            if mode in ("short_ack", "timeout"):
                assert (await hub.fifo_storage.progress())[1] == "pop_uncertain"
                writer, requests, _ = await next_item(connections)
                await validate(hub, writer, requests)
                with pytest.raises(HomeAssistantError, match="pop_uncertain"):
                    await press()
            await no_commands(writer, requests)
        if mode == "success":
            assert (
                len(rows(hub.fifo_storage.path, "SELECT id FROM fifo_access_events"))
                == 2
            )


@pytest.mark.parametrize("delayed_pop", [False, True])
async def test_extended_fifo_ack_has_no_command_identity(hass, tmp_path, delayed_pop):
    hass.config.config_dir = str(tmp_path)
    async with gateway(allow_fifo=True, allow_pop=True) as (port, connections):
        entry = await setup_entry(hass, port, timeout=0.35)
        hub = entry.runtime_data
        writer, requests, closed = await next_item(connections)
        await validate(hub, writer, requests)
        if delayed_pop:
            await hub.fifo_storage.set_pause(None)
            await toggle(hass, entry, True)
            event_id = await expect_saved_pop(hub, writer, requests, synthetic_event())
            await asyncio.wait_for(closed.wait(), 2)
            await toggle(hass, entry, False)
            writer, requests, closed = await next_item(connections)
            await validate(hub, writer, requests)

        read = asyncio.create_task(hub.read_oldest_event())
        # A genuine empty reply and a delayed removal reply look identical here.
        await read_head(writer, requests, FIELD_POP_ACK)
        await read
        assert hub.fifo_result == "invalid_response"
        await asyncio.wait_for(closed.wait(), 2)
        progress = await hub.fifo_storage.progress()
        if delayed_pop:
            assert progress[:2] == (event_id, "pop_uncertain")
        else:
            assert progress is None
        writer, requests, _ = await next_item(connections)
        await validate(hub, writer, requests)
        assert hub.can_pulse
        await no_commands(writer, requests)


async def test_auto_fifo_protocol_pause_survives_reload(hass, tmp_path):
    hass.config.config_dir = str(tmp_path)
    async with gateway(allow_fifo=True, allow_pop=True) as (port, connections):
        entry = await setup_entry(hass, port, timeout=0.35)
        hub = entry.runtime_data
        writer, requests, closed = await next_item(connections)
        await validate(hub, writer, requests)
        await hub.fifo_storage.set_pause(None)
        await toggle(hass, entry, True)
        await read_head(writer, requests, FIELD_POP_ACK)
        await asyncio.wait_for(closed.wait(), 2)
        await eventually(lambda: hub.fifo_sync.state == "paused")
        writer, requests, _ = await next_item(connections)
        await validate(hub, writer, requests)
        action = asyncio.create_task(hub.pulse())
        assert await next_command(writer, requests) == bytes.fromhex(
            "7E 05 02 21 84 58 FF"
        )
        await send(writer, IO)
        await action
        await no_commands(writer, requests, duration=0.65)
        await toggle(hass, entry, False)
        assert hub.fifo_sync.attributes["pause_reason"] == "fifo_read_protocol_error"
        await toggle(hass, entry, True)
        await no_commands(writer, requests)
        assert await hass.config_entries.async_reload(entry.entry_id)
        hub = entry.runtime_data
        writer, requests, _ = await next_item(connections)
        await validate(hub, writer, requests)
        await eventually(lambda: hub.fifo_sync.state == "paused")
        await no_commands(writer, requests)
        await toggle(hass, entry, False)
        await hass.services.async_call(
            DOMAIN,
            "clear_fifo_pause",
            {"entry_id": entry.entry_id, "verified": True},
            blocking=True,
        )
        assert await hub.fifo_storage.pause_reason() is None
        progress = await hub.fifo_storage.progress()
        assert progress is None
        await no_commands(writer, requests)
        await toggle(hass, entry, True)
        await read_head(writer, requests, ACK)
        await read_head(writer, requests, ACK)


@pytest.mark.parametrize("restart", [False, True])
async def test_pause_commit_failure_requires_fresh_enable(
    hass, tmp_path, monkeypatch, restart
):
    hass.config.config_dir = str(tmp_path)
    async with gateway(allow_fifo=True, allow_pop=True) as (port, connections):
        entry = await setup_entry(hass, port, timeout=0.35)
        # Simulate a persisted ON setting from an older version.
        hass.config_entries.async_update_entry(
            entry, data={**entry.data, "fifo_auto_sync": True}
        )
        hub = entry.runtime_data
        writer, requests, _ = await next_item(connections)
        await validate(hub, writer, requests)
        await hub.fifo_storage.set_pause(None)
        await toggle(hass, entry, True)

        async def fail_pause(reason):
            raise sqlite3.OperationalError("simulated pause commit failure")

        monkeypatch.setattr(hub.fifo_storage, "set_pause", fail_pause)
        await read_head(writer, requests, FIELD_POP_ACK)
        await eventually(
            lambda: (
                hub.fifo_sync.attributes.get("last_error_type") == "pause_storage_error"
            )
        )
        assert await hub.fifo_storage.pause_reason() is None
        if restart:
            # Cold entry startup with a new Hub, using only on-disk state.
            assert await hass.config_entries.async_unload(entry.entry_id)
            assert await hass.config_entries.async_setup(entry.entry_id)
        else:
            assert await hass.config_entries.async_reload(entry.entry_id)

        hub = entry.runtime_data
        writer, requests, _ = await next_item(connections)
        await validate(hub, writer, requests)
        assert hub.fifo_sync.enabled
        await eventually(lambda: hub.fifo_sync.state == "paused")
        assert hub.fifo_sync.attributes["pause_reason"] == "in_flight"
        assert hub.can_pulse
        await no_commands(writer, requests)
        await toggle(hass, entry, False)
        await hass.services.async_call(
            DOMAIN,
            "clear_fifo_pause",
            {"entry_id": entry.entry_id, "verified": True},
            blocking=True,
        )
        await no_commands(writer, requests)
        await toggle(hass, entry, True)
        await read_head(writer, requests, ACK)
        await read_head(writer, requests, ACK)


@pytest.mark.parametrize("startup", ["reload", "restart", "off", "interrupted_read"])
async def test_auto_sync_startup_safety(hass, tmp_path, startup):
    hass.config.config_dir = str(tmp_path)
    async with gateway(allow_fifo=True, allow_pop=True) as (port, connections):
        entry = await setup_entry(hass, port, timeout=0.35)
        hub = entry.runtime_data
        writer, requests, _ = await next_item(connections)
        await validate(hub, writer, requests)
        await hub.fifo_storage.set_pause(None)
        await toggle(hass, entry, True)
        assert await next_command(writer, requests) == FIFO
        if startup != "interrupted_read":
            await send(writer, ACK)
            await eventually(lambda: not hub.fifo_operation.locked())
        if startup == "off":
            await toggle(hass, entry, False)

        if startup == "restart":
            assert await hass.config_entries.async_unload(entry.entry_id)
            assert await hass.config_entries.async_setup(entry.entry_id)
        else:
            assert await hass.config_entries.async_reload(entry.entry_id)
        hub = entry.runtime_data
        writer, requests, _ = await next_item(connections)
        await validate(hub, writer, requests)
        assert hub.fifo_sync.enabled == (startup != "off")
        if startup in ("off", "interrupted_read"):
            await no_commands(writer, requests, duration=0.65)
            if startup == "interrupted_read":
                assert hub.fifo_sync.state == "paused"
                assert hub.fifo_sync.attributes["pause_reason"] == "in_flight"
        else:
            await read_head(writer, requests, ACK)
            await eventually(lambda: hub.fifo_sync.state == "idle")


@pytest.mark.parametrize("mode", ["on", "off", "paused", "pop_uncertain"])
async def test_fifo_sync_sensor_is_live_after_reload(hass, tmp_path, mode):
    hass.config.config_dir = str(tmp_path)
    async with gateway(allow_fifo=True, allow_pop=True) as (port, connections):
        entry = await setup_entry(hass, port, timeout=1)
        hub = entry.runtime_data
        await next_item(connections)
        entity_id = er.async_get(hass).async_get_entity_id(
            "sensor", DOMAIN, f"{entry.entry_id}:2:fifo_sync_status"
        )
        await hub.fifo_storage.set_pause(
            "fifo_read_protocol_error" if mode == "paused" else None
        )
        if mode == "pop_uncertain":
            event = observation()
            event_id = await hub.fifo_storage.save_head(event)
            await hub.fifo_storage.transition(
                event_id, "saved", "pop_uncertain", event["read_at"]
            )

        desired = mode != "off"
        hass.config_entries.async_update_entry(
            entry, data={**entry.data, "desired_auto_sync": desired}
        )
        old_worker = hub.fifo_sync.task
        assert await hass.config_entries.async_reload(entry.entry_id)
        await hass.async_block_till_done()
        assert old_worker.done()
        hub = entry.runtime_data
        writer, requests, _ = await next_item(connections)

        def assert_live(expected):
            state = hass.states.get(entity_id)
            assert state is not None
            assert state.state == expected
            assert not state.attributes.get("restored")
            assert state.attributes["desired_auto_sync"] is desired
            assert not hub.fifo_sync.task.done()
            return state

        assert not hub.can_query
        if mode == "paused":
            await eventually(lambda: hub.fifo_sync.state == "paused")
            assert assert_live("paused").attributes["pause_reason"] == (
                "fifo_read_protocol_error"
            )
        else:
            assert_live("waiting_for_gateway" if desired else "disabled")

        await validate(hub, writer, requests)
        if mode == "on":
            await read_head(writer, requests, ACK)
            await eventually(lambda: hub.fifo_sync.state == "idle")
            assert_live("idle")
        elif mode == "pop_uncertain":
            await eventually(lambda: hub.fifo_sync.state == "paused")
            assert assert_live("paused").attributes["pause_reason"] == "pop_uncertain"
            assert (await hub.fifo_storage.progress())[:2] == (
                event_id,
                "pop_uncertain",
            )
            await no_commands(writer, requests)
        else:
            await no_commands(writer, requests)
            assert_live("paused" if mode == "paused" else "disabled")
