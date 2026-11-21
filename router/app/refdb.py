"""The reference data store (ADR 50): carriers, pickup zones and tariff lanes in SQLite.

`Reference` and `Tariffs` read it on every lookup, and update mails write it, all through
ONE connection per database file, so one update mail is one transaction across all tables.
The start-kit files only seed the tables while they are empty.
"""

import sqlite3

SCHEMA = """
CREATE TABLE IF NOT EXISTS carriers (
    name TEXT PRIMARY KEY, kind TEXT NOT NULL, email TEXT NOT NULL,
    serves TEXT NOT NULL DEFAULT '',      -- airline: IATA codes; trucker: 2-digit zones
    aliases TEXT NOT NULL DEFAULT '',     -- earlier emails, still recognised (risk 4)
    active INTEGER NOT NULL DEFAULT 1,
    source TEXT NOT NULL DEFAULT 'start_kit', source_message_id INTEGER
);
CREATE TABLE IF NOT EXISTS pickup_zones (
    zone TEXT PRIMARY KEY, area TEXT NOT NULL DEFAULT '', airport TEXT NOT NULL,
    source TEXT NOT NULL DEFAULT 'start_kit', source_message_id INTEGER
);
CREATE TABLE IF NOT EXISTS tariff_lanes (
    airline TEXT NOT NULL, origin TEXT NOT NULL, destination TEXT NOT NULL,
    valid_from TEXT NOT NULL, valid_to TEXT NOT NULL,
    lane_json TEXT NOT NULL,              -- the whole TariffLane
    source TEXT NOT NULL DEFAULT 'start_kit', source_message_id INTEGER,
    PRIMARY KEY (airline, origin, destination, valid_from)
);
CREATE TABLE IF NOT EXISTS data_updates (
    message_id INTEGER PRIMARY KEY, sender TEXT, subject TEXT,
    status TEXT NOT NULL,                 -- applied | partly_applied | needs_review | nothing_found
    confirmation_message_id INTEGER, created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS data_changes (
    id INTEGER PRIMARY KEY AUTOINCREMENT, message_id INTEGER NOT NULL, change_no INTEGER NOT NULL,
    target TEXT NOT NULL, record_key TEXT NOT NULL, field TEXT NOT NULL,
    before TEXT, after TEXT,
    status TEXT NOT NULL,                 -- applied | held
    flag TEXT NOT NULL, reason TEXT NOT NULL DEFAULT '', created_at TEXT NOT NULL
);
"""

_CONNECTIONS: dict[str, sqlite3.Connection] = {}


def connect(db_path: str | None) -> sqlite3.Connection:
    """The shared connection for `db_path`; None gives a private in-memory database."""
    if db_path and db_path in _CONNECTIONS:
        return _CONNECTIONS[db_path]
    # ponytail: one connection for the whole process; the app runs one event loop
    conn = sqlite3.connect(db_path or ":memory:", timeout=5.0, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    if db_path:
        conn.execute("PRAGMA journal_mode=WAL;")
        _CONNECTIONS[db_path] = conn
    conn.executescript(SCHEMA)
    return conn


def is_empty(conn: sqlite3.Connection, table: str) -> bool:
    return conn.execute(f"SELECT 1 FROM {table} LIMIT 1").fetchone() is None  # noqa: S608 - fixed names
