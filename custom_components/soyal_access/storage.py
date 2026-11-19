"""Local SQLite storage for observations of the oldest FIFO event."""

import asyncio
import sqlite3
from contextlib import closing, suppress
from hashlib import sha256
from pathlib import Path

_SCHEMA_VERSION = 1


class FifoStorage:
    """Serialize executor writes and close each SQLite connection in its worker."""

    def __init__(self, hass, entry_id):
        self._hass = hass
        self._entry_id = entry_id
        self.path = Path(hass.config.path("soyal_access", f"{entry_id}.sqlite3"))
        self._lock = asyncio.Lock()
        self._closed = False

    async def save(self, event):
        async with self._lock:
            if self._closed:
                raise sqlite3.ProgrammingError("FIFO storage is closed")
            job = self._hass.async_add_executor_job(self._save, event)
            try:
                return await asyncio.shield(job)
            except asyncio.CancelledError:
                # Executor I/O cannot be cancelled. Keep the lock until it finishes.
                await asyncio.gather(job, return_exceptions=True)
                raise

    async def close(self):
        self._closed = True
        async with self._lock:
            pass

    def _save(self, event):
        self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        with suppress(FileExistsError):
            self.path.touch(mode=0o600, exist_ok=False)
        raw = bytes.fromhex(event["raw_frame_hex"])
        seen = event["read_at"]

        with closing(sqlite3.connect(self.path)) as connection, connection:
            connection.execute("BEGIN IMMEDIATE")
            self._ensure_schema(connection)
            with closing(
                connection.execute(
                    """
                    INSERT INTO fifo_observations (
                        entry_id, node_id, event_code, event_type, event_time_local,
                        port, user_address, raw_frame, raw_sha256, first_seen_utc,
                        last_seen_utc, read_count
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1)
                    ON CONFLICT (entry_id, node_id, raw_sha256) DO UPDATE SET
                        last_seen_utc = excluded.last_seen_utc,
                        read_count = fifo_observations.read_count + 1
                    RETURNING id, first_seen_utc, read_count
                    """,
                    (
                        self._entry_id,
                        event["source_node"],
                        event["event_code"],
                        event["event_type"],
                        event["event_time_local"],
                        event["port"],
                        event["user_address"],
                        raw,
                        sha256(raw).hexdigest(),
                        seen,
                        seen,
                    ),
                )
            ) as cursor:
                record_id, first_seen, count = cursor.fetchone()

        return {
            "storage_status": "saved" if count == 1 else "already_saved",
            "database_record_id": record_id,
            "first_seen_utc": first_seen,
            "read_count": count,
        }

    @staticmethod
    def _ensure_schema(connection):
        # Use dedicated metadata so a database from an older stage can coexist.
        connection.execute("""
            CREATE TABLE IF NOT EXISTS fifo_observation_schema (
                singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
                version INTEGER NOT NULL
            )
        """)
        row = connection.execute(
            "SELECT version FROM fifo_observation_schema WHERE singleton = 1"
        ).fetchone()
        if row is not None:
            if row[0] != _SCHEMA_VERSION:
                raise sqlite3.DatabaseError("Unsupported FIFO observation schema")
            return

        connection.execute("""
            CREATE TABLE fifo_observations (
                id INTEGER PRIMARY KEY,
                entry_id TEXT NOT NULL,
                node_id INTEGER NOT NULL,
                event_code INTEGER NOT NULL,
                event_type TEXT NOT NULL,
                event_time_local TEXT,
                port INTEGER NOT NULL,
                user_address INTEGER NOT NULL,
                raw_frame BLOB NOT NULL,
                raw_sha256 TEXT NOT NULL,
                first_seen_utc TEXT NOT NULL,
                last_seen_utc TEXT NOT NULL,
                read_count INTEGER NOT NULL,
                UNIQUE (entry_id, node_id, raw_sha256)
            )
        """)
        connection.execute(
            "INSERT INTO fifo_observation_schema VALUES (1, ?)", (_SCHEMA_VERSION,)
        )
