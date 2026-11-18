"""Migration, backup and large history pagination tests."""

import sqlite3
from contextlib import closing

import pytest

from custom_components.soyal_access.event_store import (
    _SCHEMA,
    EventStore,
    restore_for_review,
)


@pytest.mark.asyncio
async def test_migrate_page_and_restore(tmp_path):
    path = tmp_path / "original.db"
    with closing(sqlite3.connect(path)) as db, db:
        for statement in _SCHEMA:
            db.execute(statement)
        db.executemany(
            """INSERT INTO access_events
            (gateway,station,controller_time,event_type,user_address,port,raw,digest,status)
            VALUES ('entry',1,'2026-11-01T12:00:00',?,28,7,X'0102','same','saved')""",
            [(code,) for code in [0xEE] + [0x0B] * 10020],
        )
    store = EventStore(path, "entry")
    await store.open()
    try:
        received = []
        cursor = None
        while True:
            page = await store.history_page(
                {"station": 1, "user_address": 28}, cursor, 137
            )
            received.extend(row["id"] for row in page["rows"])
            cursor = page["next_cursor"]
            if cursor is None:
                break
        assert received == list(range(10021, 0, -1))
        unknown = await store.history_page({"event_type": 0xEE}, None, 1)
        assert unknown["rows"][0]["event_type"] == 0xEE
        await store.save_person("create", 1, 28, "張小明", None)
        snapshot = tmp_path / "snapshot.db"
        await store.backup(snapshot)
        restored = tmp_path / "restored.db"
        await restore_for_review(snapshot, restored)
        with closing(sqlite3.connect(restored)) as db:
            assert db.execute(
                "SELECT max(version) FROM schema_migrations"
            ).fetchone() == (2,)
            assert db.execute(
                "SELECT count(*),min(raw),max(raw) FROM access_events"
            ).fetchone() == (10021, b"\x01\x02", b"\x01\x02")
            assert db.execute("SELECT name FROM people").fetchone() == ("張小明",)
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_migration_rolls_back(tmp_path):
    path = tmp_path / "conflicting.db"
    with closing(sqlite3.connect(path)) as db, db:
        for statement in _SCHEMA:
            db.execute(statement)
        db.execute("CREATE INDEX events_time ON access_events(id)")
    store = EventStore(path, "entry")
    with pytest.raises(sqlite3.OperationalError):
        await store.open()
    with closing(sqlite3.connect(path)) as db:
        assert db.execute("SELECT max(version) FROM schema_migrations").fetchone() == (
            1,
        )
        assert (
            db.execute("SELECT name FROM sqlite_master WHERE name='people'").fetchone()
            is None
        )
