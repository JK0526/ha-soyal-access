"""Bounded, parameterized reads and local person management."""

_SCHEMA_V2 = (
    """CREATE TABLE people(
        station INTEGER NOT NULL CHECK(station BETWEEN 1 AND 255),
        user_address INTEGER NOT NULL CHECK(user_address BETWEEN 0 AND 65535),
        name TEXT NOT NULL, employee_id TEXT,
        created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
        updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
        PRIMARY KEY(station,user_address)) WITHOUT ROWID""",
    "CREATE INDEX events_time ON access_events(controller_time DESC,id DESC)",
    "CREATE INDEX events_station_time ON access_events(station,controller_time DESC,id DESC)",
    "CREATE INDEX events_person_time ON access_events(station,user_address,controller_time DESC,id DESC)",
    "INSERT INTO schema_migrations(version) VALUES (2)",
)

_PERSON = "station,user_address,name,employee_id,created_at,updated_at"
EVENT_TYPES = {0x0B: "card", 0x10: "egress", 0x1C: "pin"}


def save_person(db, action, station, user_address, name, employee_id):
    if action == "create":
        db.execute(
            "INSERT INTO people(station,user_address,name,employee_id) VALUES (?,?,?,?)",
            (station, user_address, name, employee_id),
        )
    else:
        changed = db.execute(
            """UPDATE people SET name=?,employee_id=?,updated_at=CURRENT_TIMESTAMP
               WHERE station=? AND user_address=?""",
            (name, employee_id, station, user_address),
        )
        if changed.rowcount != 1:
            raise LookupError("Person not found")
    return dict(
        db.execute(
            f"SELECT {_PERSON} FROM people WHERE station=? AND user_address=?",
            (station, user_address),
        ).fetchone()
    )


def people_page(db, filters, after, limit):
    sql = " WHERE 1=1"
    params = []
    for field in ("station", "user_address"):
        if field in filters:
            sql += f" AND {field}=?"
            params.append(filters[field])
    if "name" in filters:
        sql += " AND instr(name,?)>0"
        params.append(filters["name"])
    if after is not None:
        sql += " AND (station,user_address)>(?,?)"
        params.extend(after)
    rows = db.execute(
        f"SELECT {_PERSON} FROM people{sql} ORDER BY station,user_address LIMIT ?",
        (*params, limit + 1),
    ).fetchall()
    return _page(rows, limit, ("station", "user_address"))


def events_page(db, filters, after, limit):
    sql = """SELECT e.id,e.controller_time,e.received_at,e.station,e.event_type,e.user_address,
              p.name,p.employee_id,e.origin,e.status,
              COALESCE(p.name, CAST(e.user_address AS TEXT)) AS display_name
              FROM access_events e LEFT JOIN people p
              ON (p.station,p.user_address)=(e.station,e.user_address) WHERE 1=1"""
    params = []
    for field in ("station", "user_address", "event_type", "status", "origin"):
        if field in filters:
            sql += f" AND e.{field}=?"
            params.append(filters[field])
    if "name" in filters:
        sql += " AND instr(p.name,?)>0"
        params.append(filters["name"])
    if "start" in filters:
        sql += " AND e.controller_time>=?"
        params.append(filters["start"] + "T00:00:00")
    if "end" in filters:
        sql += " AND e.controller_time<=?"
        params.append(filters["end"] + "T23:59:59")
    if after is not None:
        sql += " AND (e.controller_time,e.id)<(?,?)"
        params.extend(after)
    rows = db.execute(
        sql + " ORDER BY e.controller_time DESC,e.id DESC LIMIT ?", (*params, limit + 1)
    ).fetchall()
    page = _page(rows, limit, ("controller_time", "id"))
    for row in page["rows"]:
        row["received_at"] = row["received_at"].replace(" ", "T") + "Z"
    return page


def _page(rows, limit, key):
    page = [dict(row) for row in rows[:limit]]
    cursor = [page[-1][field] for field in key] if len(rows) > limit else None
    return {"rows": page, "next_cursor": cursor}


def delete_person(db, station, user_address):
    if (
        db.execute(
            "DELETE FROM people WHERE station=? AND user_address=?",
            (station, user_address),
        ).rowcount
        != 1
    ):
        raise LookupError("Person not found")


def station_summary(db, station):
    event = db.execute(
        """SELECT controller_time,received_at,event_type,origin
        FROM access_events WHERE station=? ORDER BY controller_time DESC,id DESC LIMIT 1""",
        (station,),
    ).fetchone()
    sync = db.execute(
        "SELECT phase FROM sync_state WHERE station=?", (station,)
    ).fetchone()
    return (dict(event) if event else None, sync["phase"] if sync else "idle")
