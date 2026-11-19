"""Stage 8A SQLite persistence and isolation against synthetic FIFO captures."""

import asyncio
import sqlite3
from contextlib import closing
from threading import Event

import pytest
from homeassistant.exceptions import HomeAssistantError

from custom_components.soyal_access.fifo import parse_event
from custom_components.soyal_access.protocol import decode
from custom_components.soyal_access.storage import FifoStorage
from tests.test_fifo import FIFO, synthetic_event
from tests.test_ha import next_item, setup_entry
from tests.test_stage5 import IO, POLL, gateway, send, validate

pytestmark = pytest.mark.usefixtures("enable_custom_integrations", "socket_enabled")


def observation(code=0xFA, seen="2026-11-19T00:00:00+00:00"):
    return parse_event(decode(synthetic_event(code))) | {"read_at": seen}


def rows(path, query):
    with closing(sqlite3.connect(path)) as db:
        return db.execute(query).fetchall()


async def test_storage_reopen_duplicate_and_entry_isolation(hass, tmp_path):
    hass.config.config_dir = str(tmp_path)
    first = FifoStorage(hass, "first")
    result = await first.save(observation())
    assert result == {
        "storage_status": "saved",
        "database_record_id": 1,
        "first_seen_utc": "2026-11-19T00:00:00+00:00",
        "read_count": 1,
    }
    await first.close()
    reopened = FifoStorage(hass, "first")
    other = FifoStorage(hass, "second")
    later = "2026-11-19T00:01:00+00:00"
    duplicate = await reopened.save(observation(seen=later))
    assert duplicate == result | {"storage_status": "already_saved", "read_count": 2}
    assert (await reopened.save(observation(0x1C)))["database_record_id"] == 2
    assert (await other.save(observation()))["storage_status"] == "saved"
    assert rows(
        reopened.path,
        """
        SELECT entry_id, node_id, event_code, event_type, event_time_local,
               raw_frame, last_seen_utc, read_count FROM fifo_observations ORDER BY id
        """,
    )[0] == (
        "first",
        2,
        0xFA,
        "unknown_event",
        "2026-11-19T05:06:07",
        synthetic_event(0xFA),
        later,
        2,
    )
    assert rows(other.path, "SELECT entry_id, read_count FROM fifo_observations") == [
        ("second", 1)
    ]
    assert rows(reopened.path, "SELECT version FROM fifo_observation_schema") == [(1,)]
    await reopened.close()
    await other.close()


async def test_commit_failure_rolls_back_observation(hass, tmp_path, monkeypatch):
    hass.config.config_dir = str(tmp_path)
    storage = FifoStorage(hass, "rollback")
    await storage.save(observation())
    connect = sqlite3.connect

    def deny_commit(*args, **kwargs):
        db = connect(*args, **kwargs)
        db.set_authorizer(
            lambda action, arg, *_: (
                sqlite3.SQLITE_DENY
                if action == sqlite3.SQLITE_TRANSACTION and arg == "COMMIT"
                else sqlite3.SQLITE_OK
            )
        )
        return db

    with monkeypatch.context() as patch:
        patch.setattr(sqlite3, "connect", deny_commit)
        with pytest.raises(sqlite3.DatabaseError):
            await storage.save(observation())
    assert rows(storage.path, "SELECT read_count FROM fifo_observations") == [(1,)]
    assert (await storage.save(observation()))["read_count"] == 2
    await storage.close()


async def test_storage_failure_and_slow_write_preserve_poll_and_control(
    hass, tmp_path, monkeypatch, caplog
):
    hass.config.config_dir = str(tmp_path)
    async with gateway(allow_fifo=True) as (port, connections):
        entry = await setup_entry(hass, port, timeout=1)
        hub = entry.runtime_data
        writer, requests, _ = await next_item(connections)
        await validate(hub, writer, requests)
        storage = hub.fifo_storage
        started = asyncio.Event()
        release = Event()
        loop = asyncio.get_running_loop()

        def slow_failure(_event):
            loop.call_soon_threadsafe(started.set)
            assert release.wait(5)
            raise sqlite3.OperationalError("SENSITIVE_TEST_PAYLOAD")

        with monkeypatch.context() as patch:
            patch.setattr(storage, "_save", slow_failure)
            read = asyncio.create_task(hub.read_oldest_event())
            try:
                assert (await next_item(requests))[1] == FIFO
                await send(writer, synthetic_event(0x1C))
                await asyncio.wait_for(started.wait(), 2)
                with pytest.raises(HomeAssistantError, match="not sent"):
                    await hub.read_oldest_event()
                assert (await next_item(requests))[1] == POLL
                await send(writer, IO)
                pulse = asyncio.create_task(hub.pulse())
                assert (await next_item(requests))[1] == bytes.fromhex(
                    "7E 05 02 21 84 58 FF"
                )
                await send(writer, IO)
                await pulse
                assert not read.done()
            finally:
                release.set()
            await read

        assert hub.fifo_result == "event"
        assert hub.fifo_attributes["storage_status"] == "error"
        assert "raw_frame_hex" not in hub.fifo_attributes
        assert "SENSITIVE_TEST_PAYLOAD" not in caplog.text
        assert hub.node_online and hub.transport.connected
        assert connections.empty()
        # Make lazy initialization fail and retry after correcting the path.
        storage.path.mkdir(parents=True)
        for expected in ("error", "saved"):
            task = asyncio.create_task(hub.read_oldest_event())
            assert (await next_item(requests))[1] == FIFO
            await send(writer, synthetic_event(0x1C))
            await task
            assert hub.fifo_result == "event"
            assert hub.fifo_attributes["storage_status"] == expected
            assert "raw_frame_hex" not in hub.fifo_attributes
            assert hub.node_online and hub.can_pulse and connections.empty()
            if expected == "error":
                storage.path.rmdir()
        assert rows(storage.path, "SELECT raw_frame FROM fifo_observations") == [
            (synthetic_event(0x1C),)
        ]
        assert await hass.config_entries.async_unload(entry.entry_id)


async def test_unrecognized_schema_is_not_overwritten(hass, tmp_path):
    hass.config.config_dir = str(tmp_path)
    storage = FifoStorage(hass, "future")
    storage.path.parent.mkdir()
    with closing(sqlite3.connect(storage.path)) as db, db:
        db.execute("CREATE TABLE fifo_observation_schema (singleton, version)")
        db.execute("INSERT INTO fifo_observation_schema VALUES (1, 99)")
        db.execute("CREATE TABLE access_events (id INTEGER PRIMARY KEY)")
        db.execute("INSERT INTO access_events VALUES (123)")
    before = storage.path.read_bytes()
    with pytest.raises(sqlite3.DatabaseError, match="Unsupported"):
        await storage.save(observation())
    assert storage.path.read_bytes() == before
    await storage.close()
