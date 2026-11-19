"""Durable FIFO consumption journal, separate from manual observations."""

import sqlite3
from contextlib import closing
from hashlib import sha256

from .storage import FifoStorage

_SCHEMA_VERSION = 1


class SyncStorage(FifoStorage):
    """Store FIFO instances with a local sequence and a single active head."""

    def __init__(self, hass, entry):
        super().__init__(hass, entry.entry_id)
        self._endpoint = f"[{entry.data['host']}]:{entry.data['port']}"

    async def progress(self):
        return await self._run(self._progress)

    async def save_head(self, event):
        return await self._run(self._save_head, event)

    async def transition(self, event_id, previous, status, now, evidence=None):
        await self._run(self._transition, event_id, previous, status, now, evidence)

    async def begin_auto_cycle(self):
        return await self._run(self._begin_auto_cycle)

    async def finish_auto_cycle(self):
        await self._run(self._finish_auto_cycle)

    def _begin_auto_cycle(self):
        with closing(self._connect()) as db, db:
            pending = self._pending(db)
            if pending and pending[1] == "pop_uncertain":
                return pending[1]
            if db.execute("SELECT 1 FROM fifo_sync_pause").fetchone():
                return "fifo_protocol_pause"
            state = db.execute(
                "SELECT state FROM fifo_auto_safety WHERE singleton = 1"
            ).fetchone()[0]
            if state != "safe":
                return state
            db.execute("UPDATE fifo_auto_safety SET state = 'in_flight'")
        return None

    def _finish_auto_cycle(self):
        with closing(self._connect()) as db, db:
            db.execute("UPDATE fifo_auto_safety SET state = 'safe'")

    async def pause_reason(self):
        return await self._run(self._pause_reason)

    async def set_pause(self, reason):
        await self._run(self._set_pause, reason)

    def _pause_reason(self):
        if not self.path.exists():
            return None
        with closing(sqlite3.connect(f"{self.path.as_uri()}?mode=ro", uri=True)) as db:
            table = db.execute(
                "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'fifo_sync_pause'"
            ).fetchone()
            if not table:
                return None
            row = db.execute("SELECT reason FROM fifo_sync_pause").fetchone()
            return row[0] if row else None

    def _set_pause(self, reason):
        with closing(self._connect()) as db, db:
            if reason is None:
                db.execute("DELETE FROM fifo_sync_pause")
                pending = self._pending(db)
                if pending is None or pending[1] == "saved":
                    db.execute("UPDATE fifo_auto_safety SET state = 'safe'")
            else:
                db.execute("UPDATE fifo_auto_safety SET state = 'paused'")
                db.execute(
                    "INSERT OR REPLACE INTO fifo_sync_pause VALUES (1, ?)", (reason,)
                )

    def _connect(self):
        self._prepare_file()
        db = sqlite3.connect(self.path)
        try:
            db.execute("PRAGMA foreign_keys = ON")
            db.execute("PRAGMA synchronous = FULL")
            db.execute("BEGIN IMMEDIATE")
            self._ensure_sync_schema(db)
        except BaseException:
            db.close()
            raise
        return db

    def _ensure_sync_schema(self, db):
        db.execute("""
            CREATE TABLE IF NOT EXISTS fifo_sync_schema (
                singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
                version INTEGER NOT NULL,
                endpoint TEXT NOT NULL
            )
        """)
        db.execute("""
            CREATE TABLE IF NOT EXISTS fifo_sync_pause (
                singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
                reason TEXT NOT NULL
            )
        """)
        db.execute("""
            CREATE TABLE IF NOT EXISTS fifo_auto_safety (
                singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
                state TEXT NOT NULL CHECK (state IN ('safe', 'in_flight', 'paused'))
            )
        """)
        db.execute("INSERT OR IGNORE INTO fifo_auto_safety VALUES (1, 'paused')")
        row = db.execute(
            "SELECT version, endpoint FROM fifo_sync_schema WHERE singleton = 1"
        ).fetchone()
        if row is not None:
            if row[0] != _SCHEMA_VERSION:
                raise sqlite3.DatabaseError("Unsupported FIFO synchronization schema")
            if row[1] != self._endpoint:
                raise sqlite3.DatabaseError("FIFO synchronization endpoint changed")
            return
        db.execute("""
            CREATE TABLE fifo_access_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                entry_id TEXT NOT NULL,
                node_id INTEGER NOT NULL,
                sequence_number INTEGER NOT NULL UNIQUE,
                event_code INTEGER NOT NULL,
                event_type TEXT NOT NULL,
                event_time_local TEXT,
                port INTEGER NOT NULL,
                user_address INTEGER NOT NULL,
                raw_frame BLOB NOT NULL,
                raw_sha256 TEXT NOT NULL,
                first_seen_utc TEXT NOT NULL,
                consumption_status TEXT NOT NULL,
                created_at_utc TEXT NOT NULL,
                pop_intent_utc TEXT,
                resolved_at_utc TEXT,
                observed_next_sha256 TEXT
            )
        """)
        db.execute("""
            CREATE TABLE fifo_sync_progress (
                singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
                pending_event_id INTEGER REFERENCES fifo_access_events(id),
                last_sequence INTEGER NOT NULL
            )
        """)
        db.execute("INSERT INTO fifo_sync_progress VALUES (1, NULL, 0)")
        db.execute(
            "INSERT INTO fifo_sync_schema VALUES (1, ?, ?)",
            (_SCHEMA_VERSION, self._endpoint),
        )

    @staticmethod
    def _pending(db):
        row = db.execute("""
            SELECT p.pending_event_id, e.consumption_status, e.raw_frame
            FROM fifo_sync_progress p LEFT JOIN fifo_access_events e
            ON e.id = p.pending_event_id WHERE p.singleton = 1
        """).fetchone()
        if row is None or (
            row[0] is not None and row[1] not in ("saved", "pop_uncertain")
        ):
            raise sqlite3.DatabaseError("Invalid FIFO synchronization journal")
        return row if row[0] is not None else None

    def _progress(self):
        with closing(self._connect()) as db, db:
            return self._pending(db)

    def _save_head(self, event):
        raw = bytes.fromhex(event["raw_frame_hex"])
        now = event["read_at"]
        with closing(self._connect()) as db, db:
            pending = self._pending(db)
            if pending is not None:
                if pending[1:] != ("saved", raw):
                    raise sqlite3.DatabaseError(
                        "FIFO head does not match saved progress"
                    )
                return pending[0]
            sequence = db.execute(
                "SELECT last_sequence + 1 FROM fifo_sync_progress WHERE singleton = 1"
            ).fetchone()[0]
            event_id = db.execute(
                """
                INSERT INTO fifo_access_events (
                    entry_id, node_id, sequence_number, event_code, event_type,
                    event_time_local, port, user_address, raw_frame, raw_sha256,
                    first_seen_utc, consumption_status, created_at_utc
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'saved', ?)
            """,
                (
                    self._entry_id,
                    event["source_node"],
                    sequence,
                    event["event_code"],
                    event["event_type"],
                    event["event_time_local"],
                    event["port"],
                    event["user_address"],
                    raw,
                    sha256(raw).hexdigest(),
                    now,
                    now,
                ),
            ).lastrowid
            db.execute(
                "UPDATE fifo_sync_progress SET pending_event_id = ?, last_sequence = ? WHERE singleton = 1",
                (event_id, sequence),
            )
            return event_id

    def _transition(self, event_id, previous, status, now, evidence):
        with closing(self._connect()) as db, db:
            pending = self._pending(db)
            if pending is None or pending[:2] != (event_id, previous):
                raise sqlite3.DatabaseError("FIFO synchronization state changed")
            db.execute(
                """
                UPDATE fifo_access_events SET
                    consumption_status = ?, pop_intent_utc = CASE
                        WHEN ? = 'pop_uncertain' THEN ? ELSE pop_intent_utc END,
                    resolved_at_utc = ?, observed_next_sha256 = ?
                WHERE id = ?
            """,
                (
                    status,
                    status,
                    now,
                    now
                    if status in ("popped_confirmed", "advanced_observed")
                    else None,
                    evidence,
                    event_id,
                ),
            )
            if status in ("popped_confirmed", "advanced_observed"):
                db.execute(
                    "UPDATE fifo_sync_progress SET pending_event_id = NULL WHERE singleton = 1"
                )
