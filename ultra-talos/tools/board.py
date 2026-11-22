#!/usr/bin/env python3
"""The black board: one SQLite table with everything that happened, across all sessions.

  tools/board.py show [SHIPMENT]        history of one shipment (or the last 40 events of all)
  tools/board.py find TEXT              search every event (customer, BK-..., airport, mail id)
  tools/board.py note SHIPMENT KIND TEXT   add an entry (kinds: quote, booking, cancel, answer, update, decision, ...)
  tools/board.py sql "SELECT ..."       anything else (table events: ts, shipment, mail_id, kind, summary, data)

The router writes every mail, classification and session; tools/api.py writes every mail
sent and every booking. You add what only you know: the decision and why.
"""

import json
import os
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

DB = Path(__file__).resolve().parent.parent / "state" / "board.db"


def db() -> sqlite3.Connection:
    DB.parent.mkdir(exist_ok=True)
    con = sqlite3.connect(DB, timeout=10)
    con.execute("CREATE TABLE IF NOT EXISTS events (id INTEGER PRIMARY KEY, ts TEXT, shipment TEXT, "
                "mail_id INTEGER, kind TEXT, summary TEXT, data TEXT)")
    con.execute("CREATE INDEX IF NOT EXISTS events_shipment ON events (shipment)")
    return con


def log(kind: str, summary: str, shipment: str | None = None, mail_id: int | None = None, data: object = None) -> None:
    """Add one event. Never raises: the board must not break the work it records."""
    try:
        with db() as con:
            con.execute("INSERT INTO events (ts, shipment, mail_id, kind, summary, data) VALUES (?,?,?,?,?,?)",
                        (datetime.now(timezone.utc).isoformat(timespec="seconds"),
                         shipment or os.environ.get("TALOS_SHIPMENT"), mail_id, kind, summary,
                         None if data is None else json.dumps(data, ensure_ascii=False)))
    except sqlite3.Error as exc:
        print(f"board: {exc}", file=sys.stderr)


def show(rows: list[tuple]) -> None:
    for ts, shipment, mail_id, kind, summary in rows:
        print(f"{ts[5:19]} [{shipment or '-'}] {kind}{f' mail {mail_id}' if mail_id else ''}: {summary}")


if __name__ == "__main__":
    args = sys.argv[1:]
    cols = "ts, shipment, mail_id, kind, summary"
    if args[:1] == ["show"]:
        if len(args) > 1:
            show(db().execute(f"SELECT {cols} FROM events WHERE shipment=? ORDER BY id", (args[1],)).fetchall())
        else:
            show(db().execute(f"SELECT {cols} FROM (SELECT * FROM events ORDER BY id DESC LIMIT 40) ORDER BY id").fetchall())
    elif args[:1] == ["find"] and len(args) == 2:
        like = f"%{args[1]}%"
        show(db().execute(f"SELECT {cols} FROM events WHERE summary LIKE ? OR data LIKE ? OR shipment=? ORDER BY id",
                          (like, like, args[1])).fetchall())
    elif args[:1] == ["note"] and len(args) == 4:
        log(args[2], args[3], shipment=args[1])
        print("noted")
    elif args[:1] == ["sql"] and len(args) == 2:
        for row in db().execute(args[1]).fetchall():
            print(" | ".join("" if v is None else str(v) for v in row))
    else:
        print(__doc__)
        sys.exit(2)
