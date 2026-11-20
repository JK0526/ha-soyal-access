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

    def read_history(self):
        """Read a consistent count and latest rows without creating a database."""
        if not self.path.exists():
            return 0, []
        with closing(
            sqlite3.connect(f"{self.path.resolve().as_uri()}?mode=ro", uri=True)
        ) as db:
            db.row_factory = sqlite3.Row
            db.execute("BEGIN")
            count = db.execute("SELECT COUNT(*) FROM access_events").fetchone()[0]
            rows = db.execute(
                """SELECT id, received_at, event_time_local, event_type,
                          port, user_address
                   FROM access_events ORDER BY id DESC LIMIT 100"""
            ).fetchall()
            return count, [dict(row) for row in rows]
