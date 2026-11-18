"""Durable per-gateway FIFO journal. Each operation owns a SQLite transaction."""

import asyncio
import hashlib
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from datetime import datetime
from pathlib import Path

from .protocol import ProtocolError, decode

_SCHEMA = (
    "CREATE TABLE schema_migrations(version INTEGER PRIMARY KEY, applied_at TEXT DEFAULT CURRENT_TIMESTAMP)",
    """CREATE TABLE access_events(
       id INTEGER PRIMARY KEY, gateway TEXT NOT NULL, station INTEGER NOT NULL,
       controller_time TEXT NOT NULL, received_at TEXT DEFAULT CURRENT_TIMESTAMP,
       event_type INTEGER NOT NULL, user_address INTEGER NOT NULL, port INTEGER NOT NULL,
       raw BLOB NOT NULL, digest TEXT NOT NULL, origin TEXT NOT NULL DEFAULT 'backfill',
       status TEXT NOT NULL, created_at TEXT DEFAULT CURRENT_TIMESTAMP,
       updated_at TEXT DEFAULT CURRENT_TIMESTAMP)""",
    """CREATE TABLE sync_state(station INTEGER PRIMARY KEY, phase TEXT NOT NULL DEFAULT 'idle',
       event_id INTEGER REFERENCES access_events(id), head BLOB, intent INTEGER DEFAULT 0,
       possibly_sent INTEGER DEFAULT 0, last_success TEXT, processed INTEGER DEFAULT 0,
       saved INTEGER DEFAULT 0, error TEXT)""",
    "INSERT INTO schema_migrations(version) VALUES (1)",
)


class EventStore:
    def __init__(self, path: Path, gateway: str, gate=None):
        self._gate = gate or asyncio.Lock()
        self.path = path
        self.gateway = gateway
        self._worker = ThreadPoolExecutor(max_workers=1)
        self._db = None

    async def _run(self, operation, *args):
        async with self._gate:
            future = asyncio.get_running_loop().run_in_executor(
                self._worker, operation, *args
            )
            try:
                return await asyncio.shield(future)
            except asyncio.CancelledError:
                await future
                raise

    async def open(self):
        try:
            await self._run(self._open)
        except BaseException:
            await self.close()
            raise

    def _open(self):
        if self._db is not None:
            return
        self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        db = sqlite3.connect(self.path, isolation_level=None, timeout=5)
        try:
            db.row_factory = sqlite3.Row
            db.execute("PRAGMA foreign_keys=ON")
            db.execute("PRAGMA journal_mode=DELETE")
            db.execute("PRAGMA synchronous=FULL")
            db.execute("PRAGMA busy_timeout=5000")
            db.execute("BEGIN IMMEDIATE")
            if not db.execute(
                "SELECT 1 FROM sqlite_master WHERE name='schema_migrations'"
            ).fetchone():
                for sql in _SCHEMA:
                    db.execute(sql)
            if (
                db.execute("SELECT max(version) FROM schema_migrations").fetchone()[0]
                != 1
            ):
                raise ValueError("Unsupported SOYAL event database version")
            # An old snapshot or a restart cannot prove what the controller did.
            db.execute("""UPDATE sync_state SET phase='uncertain',error='interrupted'
                       WHERE phase IN ('ready_to_delete','delete_in_flight')""")
            db.commit()
        except BaseException:
            db.rollback()
            db.close()
            raise
        self._db = db

    def _transaction(self, operation, *args):
        db = self._db
        db.execute("BEGIN IMMEDIATE")
        try:
            result = operation(db, *args)
            db.commit()
            return result
        except BaseException:
            db.rollback()
            raise

    async def state(self, station):
        def read(db):
            db.execute(
                "INSERT OR IGNORE INTO sync_state(station) VALUES (?)", (station,)
            )
            return dict(
                db.execute(
                    "SELECT * FROM sync_state WHERE station=?", (station,)
                ).fetchone()
            )

        return await self._run(self._transaction, read)

    async def save_head(self, station, raw):
        frame = decode(raw)
        if (
            frame.node != 0
            or frame.code not in (0x0B, 0x10, 0x1C)
            or len(raw) < 31
            or frame.payload[0] != station
        ):
            raise ProtocolError("Unexpected FIFO event")
        second, minute, hour, _, day, month, year = raw[5:12]
        if year > 99:
            raise ProtocolError("Invalid controller year")
        local_time = datetime(2000 + year, month, day, hour, minute, second).isoformat()

        def save(db):
            event_id = db.execute(
                """INSERT INTO access_events
                (gateway,station,controller_time,event_type,user_address,port,raw,digest,status)
                VALUES (?,?,?,?,?,?,?,?, 'saved')""",
                (
                    self.gateway,
                    station,
                    local_time,
                    raw[3],
                    int.from_bytes(raw[13:15], "big"),
                    raw[12],
                    raw,
                    hashlib.sha256(raw).hexdigest(),
                ),
            ).lastrowid
            changed = db.execute(
                """UPDATE sync_state SET phase='ready_to_delete',event_id=?,
                head=?,saved=saved+1,error=NULL WHERE station=? AND phase='idle'
                AND (head IS NULL OR head=?)""",
                (event_id, raw, station, raw),
            )
            if changed.rowcount != 1:
                raise ValueError("FIFO checkpoint mismatch")

        await self._run(self._transaction, save)

    async def mark_sent(self, station, head):
        def mark(db):
            changed = db.execute(
                """UPDATE sync_state SET intent=1, possibly_sent=1,
                phase='delete_in_flight' WHERE station=? AND head=? AND event_id IS NOT NULL
                AND phase='ready_to_delete'""",
                (station, head),
            )
            if changed.rowcount != 1:
                raise ValueError("Delete precondition failed")

        await self._run(self._transaction, mark)

    async def advance(self, station, head, following):
        def finish(db):
            row = db.execute(
                """SELECT event_id FROM sync_state WHERE station=? AND head=?
                AND phase='delete_in_flight' AND possibly_sent=1""",
                (station, head),
            ).fetchone()
            if row is None or head == following:
                raise ValueError("FIFO advancement unconfirmed")
            db.execute(
                "UPDATE access_events SET status='confirmed',updated_at=CURRENT_TIMESTAMP WHERE id=?",
                (row[0],),
            )
            db.execute(
                """UPDATE sync_state SET phase='idle',head=?,intent=0,possibly_sent=0,
                event_id=NULL,processed=processed+1,last_success=CURRENT_TIMESTAMP,error=NULL WHERE station=?""",
                (following, station),
            )

        await self._run(self._transaction, finish)

    async def stop(self, station, reason):
        def stop(db):
            db.execute(
                "UPDATE sync_state SET phase='uncertain',error=? WHERE station=?",
                (reason, station),
            )

        await self._run(self._transaction, stop)

    async def backup(self, destination):
        def copy():
            destination.touch(mode=0o600, exist_ok=False)
            try:
                with closing(sqlite3.connect(destination)) as target:
                    self._db.backup(target)
            except BaseException:
                destination.unlink(missing_ok=True)
                raise

        await self._run(copy)

    async def close(self):
        def close():
            if self._db is not None:
                self._db.close()
                self._db = None

        try:
            await self._run(close)
        finally:
            self._worker.shutdown(wait=False)


async def restore_for_review(snapshot: Path, destination: Path):
    """Restore to a new file only, with all known FIFO positions quarantined."""

    def restore():
        with closing(
            sqlite3.connect(snapshot.resolve().as_uri() + "?mode=ro", uri=True)
        ) as source:
            if source.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                raise ValueError("Invalid SQLite snapshot")
            if (
                source.execute("SELECT max(version) FROM schema_migrations").fetchone()[
                    0
                ]
                != 1
            ):
                raise ValueError("Unsupported snapshot")
            # Exclusive creation prevents replacing the running HA database.
            destination.touch(mode=0o600, exist_ok=False)
            try:
                with closing(sqlite3.connect(destination)) as target:
                    source.backup(target)
                    target.execute("BEGIN IMMEDIATE")
                    try:
                        target.execute(
                            "UPDATE sync_state SET phase='paused',error='restored_snapshot'"
                        )
                        target.commit()
                    except BaseException:
                        target.rollback()
                        raise
            except BaseException:
                destination.unlink(missing_ok=True)
                raise

    await asyncio.to_thread(restore)
