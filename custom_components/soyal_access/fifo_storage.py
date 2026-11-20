"""Small asynchronous SQLite store for observed FIFO events."""

import sqlite3
from contextlib import closing
from pathlib import Path


class FifoStorage:
    """Persist each FIFO observation in a dedicated integration database."""

    def __init__(self, path: str | Path):
        self.path = Path(path)

    def _connect(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        return sqlite3.connect(self.path)

    def initialize(self):
        with closing(self._connect()) as db, db:
            db.execute(
                """CREATE TABLE IF NOT EXISTS access_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    received_at TEXT NOT NULL,
                    node_id INTEGER NOT NULL,
                    event_code INTEGER NOT NULL,
                    event_type TEXT NOT NULL,
                    event_time_local TEXT,
                    port INTEGER NOT NULL,
                    user_address INTEGER NOT NULL,
                    raw_frame_hex TEXT NOT NULL
                )"""
            )

    def save_event(self, event: dict, received_at: str):
        with closing(self._connect()) as db, db:
            db.execute(
                """INSERT INTO access_events (
                    received_at, node_id, event_code, event_type,
                    event_time_local, port, user_address, raw_frame_hex
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    received_at,
                    event["node_id"],
                    event["event_code"],
                    event["event_type"],
                    event["event_time_local"],
                    event["port"],
                    event["user_address"],
                    event["raw_frame_hex"],
                ),
            )
