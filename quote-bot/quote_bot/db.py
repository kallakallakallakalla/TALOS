"""SQLite state: since_id, processed messages, quotes (grows into the CRM)."""

from __future__ import annotations

import json
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

SCHEMA = """
CREATE TABLE IF NOT EXISTS state (
    key   TEXT PRIMARY KEY,
    value TEXT
);
CREATE TABLE IF NOT EXISTS messages (
    id           INTEGER PRIMARY KEY,
    received_at  TEXT,
    sender       TEXT,
    subject      TEXT,
    category     TEXT,
    method       TEXT,
    status       TEXT,
    flags        TEXT,
    llm_tokens   INTEGER,
    processed_at TEXT
);
CREATE TABLE IF NOT EXISTS quotes (
    id                   INTEGER PRIMARY KEY AUTOINCREMENT,
    message_id           INTEGER,
    lot                  INTEGER,
    origin               TEXT,
    destination          TEXT,
    weight_kg            REAL,
    volume_cbm           REAL,
    pieces               INTEGER,
    airline              TEXT,
    airline_code         TEXT,
    rate_id              TEXT,
    total                TEXT,
    currency             TEXT,
    quoted_weight_kg     TEXT,
    chargeable_weight_kg TEXT,
    transit_days         INTEGER,
    valid_until          TEXT,
    break_applied        INTEGER,
    created_at           TEXT,
    raw                  TEXT
);
CREATE TABLE IF NOT EXISTS sent (
    id          INTEGER PRIMARY KEY,
    to_addr     TEXT,
    subject     TEXT,
    in_reply_to INTEGER,
    created_at  TEXT
);
CREATE TABLE IF NOT EXISTS bookings (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    rfq_id            INTEGER,
    lot               INTEGER,
    customer          TEXT,
    booking_reference TEXT,
    team_reference    TEXT,
    rate_id           TEXT,
    airline           TEXT,
    origin            TEXT,
    destination       TEXT,
    price             TEXT,
    currency          TEXT,
    status            TEXT,
    pickup            TEXT,
    transit_days      INTEGER,
    created_at        TEXT,
    raw               TEXT
);
CREATE INDEX IF NOT EXISTS idx_quotes_msg ON quotes(message_id);
CREATE INDEX IF NOT EXISTS idx_quotes_rate ON quotes(rate_id);
"""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class DB:
    def __init__(self, path: Path | str):
        self.conn = sqlite3.connect(str(path), check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.lock = threading.Lock()
        with self.lock, self.conn:
            self.conn.executescript(SCHEMA)

    # -- state ----------------------------------------------------------------
    def get_since_id(self) -> int:
        row = self.conn.execute("SELECT value FROM state WHERE key='since_id'").fetchone()
        return int(row["value"]) if row else 0

    def set_since_id(self, value: int) -> None:
        with self.lock, self.conn:
            self.conn.execute(
                "INSERT INTO state(key, value) VALUES('since_id', ?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (str(int(value)),),
            )

    # -- messages ---------------------------------------------------------------
    def is_processed(self, message_id: int) -> bool:
        row = self.conn.execute("SELECT 1 FROM messages WHERE id=?", (message_id,)).fetchone()
        return row is not None

    def save_message(
        self,
        mail_id: int,
        *,
        received_at: str | None,
        sender: str,
        subject: str,
        category: str,
        method: str,
        status: str,
        flags: list[str],
        llm_tokens: int = 0,
    ) -> None:
        with self.lock, self.conn:
            self.conn.execute(
                "INSERT OR REPLACE INTO messages(id, received_at, sender, subject, category, method,"
                " status, flags, llm_tokens, processed_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
                (
                    mail_id, received_at, sender, subject, category, method, status,
                    json.dumps(flags, ensure_ascii=False), llm_tokens, _now(),
                ),
            )

    # -- sent mails / threads ---------------------------------------------------
    def save_sent(self, sent_id: int, to: str, subject: str, in_reply_to: int | None) -> None:
        with self.lock, self.conn:
            self.conn.execute(
                "INSERT OR REPLACE INTO sent(id, to_addr, subject, in_reply_to, created_at) VALUES(?,?,?,?,?)",
                (sent_id, to, subject, in_reply_to, _now()),
            )

    def sent_parent(self, sent_id: int) -> int | None:
        row = self.conn.execute("SELECT in_reply_to FROM sent WHERE id=?", (sent_id,)).fetchone()
        return row["in_reply_to"] if row else None

    def quotes_for(self, rfq_id: int) -> list[sqlite3.Row]:
        return self.conn.execute(
            "SELECT * FROM quotes WHERE message_id=? AND rate_id IS NOT NULL ORDER BY lot, id DESC", (rfq_id,)
        ).fetchall()

    def message(self, mid: int) -> sqlite3.Row | None:
        return self.conn.execute("SELECT * FROM messages WHERE id=?", (mid,)).fetchone()

    def latest_rfq_for(self, email: str, subject: str | None = None) -> int | None:
        q = ("SELECT m.id FROM messages m WHERE m.category='rfq' AND lower(m.sender) LIKE ? "
             "AND EXISTS (SELECT 1 FROM quotes q WHERE q.message_id=m.id AND q.rate_id IS NOT NULL)")
        args: list[Any] = [f"%{email.lower()}%"]
        if subject:
            row = self.conn.execute(q + " AND m.subject=? ORDER BY m.id DESC LIMIT 1", args + [subject]).fetchone()
            if row:
                return row["id"]
        row = self.conn.execute(q + " ORDER BY m.id DESC LIMIT 1", args).fetchone()
        return row["id"] if row else None

    # -- bookings ---------------------------------------------------------------
    def save_booking(self, **kw: Any) -> None:
        kw.setdefault("created_at", _now())
        cols = ",".join(kw)
        with self.lock, self.conn:
            self.conn.execute(f"INSERT INTO bookings({cols}) VALUES({','.join('?' * len(kw))})", tuple(kw.values()))

    def bookings_for_rfq(self, rfq_id: int) -> list[sqlite3.Row]:
        return self.conn.execute("SELECT * FROM bookings WHERE rfq_id=? ORDER BY lot", (rfq_id,)).fetchall()

    def find_bookings(self, email: str, text: str) -> list[sqlite3.Row]:
        rows = self.conn.execute("SELECT * FROM bookings ORDER BY id DESC").fetchall()
        hits = [r for r in rows if any(v and str(v) in text for v in
                                       (r["booking_reference"], r["team_reference"], r["rate_id"]))]
        if hits:
            return hits
        mine = [r for r in rows if email and email.lower() in (r["customer"] or "").lower()]
        if not mine:
            return []
        last = mine[0]["rfq_id"]
        return [r for r in mine if r["rfq_id"] == last]

    # -- quotes -----------------------------------------------------------------
    def save_quote(self, message_id: int, lot: int, shipment: dict, rate: dict | None,
                   airline: str, break_applied: bool) -> None:
        rate = rate or {}
        with self.lock, self.conn:
            self.conn.execute(
                "INSERT INTO quotes(message_id, lot, origin, destination, weight_kg, volume_cbm, pieces,"
                " airline, airline_code, rate_id, total, currency, quoted_weight_kg, chargeable_weight_kg,"
                " transit_days, valid_until, break_applied, created_at, raw)"
                " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    message_id, lot, shipment.get("origin"), shipment.get("destination"),
                    shipment.get("weight_kg"), shipment.get("volume_cbm"), shipment.get("pieces"),
                    rate.get("airline", airline), rate.get("airline_code"), rate.get("rate_id"),
                    _s(rate.get("total")), rate.get("currency"), _s(rate.get("weight_kg")),
                    _s(rate.get("chargeable_weight_kg")), rate.get("transit_days"),
                    rate.get("valid_until"), int(break_applied), _now(),
                    json.dumps(rate, ensure_ascii=False, default=str),
                ),
            )


def _s(v: Any) -> str | None:
    return None if v is None else str(v)
