"""SQLite history shared by SOYAL FIFO observations and accepted HA controls."""

import sqlite3
from contextlib import closing
from pathlib import Path


class FifoStorage:
    """Persist events in the existing entry-local integration database."""

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
                    raw_frame_hex TEXT NOT NULL,
                    source TEXT NOT NULL DEFAULT 'soyal',
                    ha_user_id TEXT DEFAULT NULL,
                    ha_user_name TEXT DEFAULT NULL
                )"""
            )

            columns = {row[1] for row in db.execute("PRAGMA table_info(access_events)")}
            for name, definition in (
                ("source", "TEXT NOT NULL DEFAULT 'soyal'"),
                ("ha_user_id", "TEXT DEFAULT NULL"),
                ("ha_user_name", "TEXT DEFAULT NULL"),
            ):
                if name not in columns:
                    db.execute(
                        f"ALTER TABLE access_events ADD COLUMN {name} {definition}"
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

    def save_ha_event(
        self,
        node_id,
        event_type,
        received_at,
        event_time_local,
        ha_user_id,
        ha_user_name,
    ):
        """Append one accepted HA command; -1/empty packet are non-SOYAL sentinels."""
        with closing(self._connect()) as db, db:
            db.execute(
                """INSERT INTO access_events (
                    received_at, node_id, event_code, event_type,
                    event_time_local, port, user_address, raw_frame_hex,
                    source, ha_user_id, ha_user_name
                ) VALUES (?, ?, -1, ?, ?, -1, -1, '', 'ha', ?, ?)""",
                (
                    received_at,
                    node_id,
                    event_type,
                    event_time_local,
                    ha_user_id,
                    ha_user_name,
                ),
            )

    def read_history(self, node_id):
        """Read a consistent count and latest rows without creating a database."""
        if not self.path.exists():
            return 0, []
        with closing(
            sqlite3.connect(f"{self.path.resolve().as_uri()}?mode=ro", uri=True)
        ) as db:
            db.row_factory = sqlite3.Row
            db.execute("BEGIN")
            count = db.execute(
                "SELECT COUNT(*) FROM access_events WHERE node_id = ?", (node_id,)
            ).fetchone()[0]
            rows = db.execute(
                """SELECT id, node_id, received_at, event_code, event_time_local, event_type,
                          port, user_address, source, ha_user_id, ha_user_name
                   FROM access_events WHERE node_id = ? ORDER BY id DESC LIMIT 100""",
                (node_id,),
            ).fetchall()
            events = [dict(row) for row in rows]
            for event in events:
                event_code = event.pop("event_code")
                if event_code == 0x01 and event["event_type"] == "unknown_event":
                    event["event_type"] = "invalid_user_pin"
            return count, events
