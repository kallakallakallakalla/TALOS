import json
import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from app.schemas import (
    AirOption,
    ClassificationDecision,
    EmailCategory,
    MailboxAttachment,
    MailboxMessage,
    OutboundMail,
    Shipment,
    ShipmentRequest,
    ShipmentState,
    StoredMessageRecord,
    TruckerRequest,
)

_SINCE_ID_KEY = "mailbox_since_id"


def _utc_now_iso() -> str:
    return datetime.now(UTC).isoformat()


def _category_where(category: EmailCategory | None) -> tuple[str, tuple[Any, ...]]:
    if category is None:
        return "", ()
    return "WHERE category = ?", (category.value,)


class MessageStore:
    """SQLite-backed store for polling cursor, deduplication, and classified mails."""

    def __init__(self, db_path: str = "data/router.db") -> None:
        self._db_path = db_path
        if db_path != ":memory:":
            Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        self._init_schema()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self._db_path, timeout=5.0)
        conn.row_factory = sqlite3.Row
        if self._db_path != ":memory:":
            conn.execute("PRAGMA journal_mode=WAL;")
        return conn

    def _init_schema(self) -> None:
        with self._connect() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS poller_state (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS processed_messages (
                    message_id INTEGER PRIMARY KEY,
                    sender TEXT,
                    to_json TEXT NOT NULL,
                    subject TEXT NOT NULL,
                    body TEXT NOT NULL,
                    in_reply_to INTEGER,
                    references_json TEXT NOT NULL,
                    attachments_json TEXT NOT NULL,
                    raw_json TEXT NOT NULL,
                    category TEXT NOT NULL,
                    confidence REAL NOT NULL,
                    reasoning TEXT NOT NULL,
                    model TEXT NOT NULL,
                    trace_id TEXT,
                    received_at TEXT,
                    processed_at TEXT NOT NULL
                );

                CREATE INDEX IF NOT EXISTS idx_processed_messages_category
                    ON processed_messages (category, message_id DESC);

                CREATE TABLE IF NOT EXISTS message_failures (
                    message_id INTEGER PRIMARY KEY,
                    attempts INTEGER NOT NULL,
                    last_error TEXT
                );

                -- Quote workflow (docs/workflow.md section 8)
                CREATE TABLE IF NOT EXISTS shipments (
                    id INTEGER PRIMARY KEY,           -- customer's first mail id = team_reference
                    customer TEXT NOT NULL, subject TEXT NOT NULL, language TEXT,
                    state TEXT NOT NULL, request_json TEXT NOT NULL,
                    goal TEXT, origin_airport TEXT, zone TEXT,
                    clarifications INTEGER NOT NULL DEFAULT 0,
                    answers_sent INTEGER NOT NULL DEFAULT 0,
                    pricing_started_at TEXT,
                    quote_json TEXT, quote_message_id INTEGER, chosen_option_json TEXT,
                    booking_ref TEXT, booking_attempts INTEGER NOT NULL DEFAULT 0,
                    last_booking_attempt_at TEXT,
                    error TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS trucker_requests (
                    out_message_id INTEGER PRIMARY KEY, shipment_id INTEGER NOT NULL,
                    trucker TEXT NOT NULL, sent_at TEXT NOT NULL,
                    answered INTEGER NOT NULL DEFAULT 0,
                    price_eur REAL, pickup_hours REAL, valid_until TEXT, ref TEXT,
                    reply_message_id INTEGER
                );
                CREATE TABLE IF NOT EXISTS air_options (
                    shipment_id INTEGER NOT NULL, source TEXT NOT NULL,  -- portal | tariff
                    airline TEXT NOT NULL, airline_code TEXT, origin TEXT, destination TEXT,
                    billed_kg REAL, total_eur REAL NOT NULL, transit_days INTEGER NOT NULL,
                    rate_id TEXT, valid_until TEXT, weight_kg REAL, volume_cbm REAL
                );
                -- Every mail the workflow composed. In a dry run sent_message_id stays NULL.
                CREATE TABLE IF NOT EXISTS outbound_mails (
                    id INTEGER PRIMARY KEY AUTOINCREMENT, shipment_id INTEGER,
                    kind TEXT NOT NULL, recipient TEXT NOT NULL, subject TEXT NOT NULL,
                    body TEXT NOT NULL, in_reply_to INTEGER, sent_message_id INTEGER,
                    used_template INTEGER NOT NULL DEFAULT 0, created_at TEXT NOT NULL
                );
                """
            )

    def _read_since_id(self, conn: sqlite3.Connection) -> int:
        row = conn.execute(
            "SELECT value FROM poller_state WHERE key = ?",
            (_SINCE_ID_KEY,),
        ).fetchone()
        return int(row["value"]) if row else 0

    def get_since_id(self) -> int:
        """Return the last persisted since_id cursor (0 if none stored yet)."""
        with self._connect() as conn:
            return self._read_since_id(conn)

    def set_since_id(self, since_id: int) -> int:
        """Persist since_id monotonically (never decreases an existing cursor)."""
        with self._connect() as conn:
            return self._upsert_since_id(conn, since_id)

    def _upsert_since_id(self, conn: sqlite3.Connection, candidate: int) -> int:
        current = self._read_since_id(conn)
        target = max(current, candidate)
        conn.execute(
            """
            INSERT INTO poller_state (key, value, updated_at)
            VALUES (?, ?, ?)
            ON CONFLICT(key) DO UPDATE SET
                value = excluded.value,
                updated_at = excluded.updated_at
            """,
            (_SINCE_ID_KEY, str(target), _utc_now_iso()),
        )
        return target

    def is_processed(self, message_id: int) -> bool:
        """Check whether a message_id has already been processed and stored."""
        with self._connect() as conn:
            row = conn.execute(
                "SELECT 1 FROM processed_messages WHERE message_id = ?",
                (message_id,),
            ).fetchone()
        return row is not None

    def save_processed_message(
        self,
        message: MailboxMessage,
        decision: ClassificationDecision,
        model: str,
        trace_id: str | None = None,
        advance_cursor: bool = True,
    ) -> bool:
        """Atomically persist a classified mail and advance since_id. Returns False if duplicate."""
        now_iso = _utc_now_iso()
        with self._connect() as conn:
            cursor = conn.execute(
                """
                INSERT OR IGNORE INTO processed_messages (
                    message_id,
                    sender,
                    to_json,
                    subject,
                    body,
                    in_reply_to,
                    references_json,
                    attachments_json,
                    raw_json,
                    category,
                    confidence,
                    reasoning,
                    model,
                    trace_id,
                    received_at,
                    processed_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    message.id,
                    message.sender,
                    json.dumps(message.to),
                    message.subject,
                    message.body,
                    message.in_reply_to,
                    json.dumps(message.references),
                    json.dumps([att.model_dump() for att in message.attachments]),
                    json.dumps(message.raw),
                    decision.category.value,
                    decision.confidence,
                    decision.reasoning,
                    model,
                    trace_id,
                    message.created_at,
                    now_iso,
                ),
            )
            inserted = cursor.rowcount > 0
            if advance_cursor:
                self._upsert_since_id(conn, message.id)
        return inserted

    def record_failure(self, message_id: int, error: str) -> int:
        """Count one failed processing attempt of a mail and return the new total (ADR 20)."""
        with self._connect() as conn:
            row = conn.execute(
                """
                INSERT INTO message_failures (message_id, attempts, last_error) VALUES (?, 1, ?)
                ON CONFLICT(message_id) DO UPDATE SET
                    attempts = attempts + 1, last_error = excluded.last_error
                RETURNING attempts
                """,
                (message_id, error),
            ).fetchone()
        return int(row["attempts"])

    def count_messages(self, category: EmailCategory | None = None) -> int:
        """Return total number of stored messages, optionally filtered by category."""
        where_sql, params = _category_where(category)
        with self._connect() as conn:
            row = conn.execute(
                f"SELECT COUNT(*) AS cnt FROM processed_messages {where_sql}", params
            ).fetchone()
        return int(row["cnt"]) if row else 0

    def list_messages(
        self,
        category: EmailCategory | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> tuple[list[StoredMessageRecord], int]:
        """Return stored messages (newest first) along with total matching count."""
        total = self.count_messages(category=category)
        where_sql, params = _category_where(category)
        with self._connect() as conn:
            rows = conn.execute(
                f"""
                SELECT * FROM processed_messages
                {where_sql}
                ORDER BY message_id DESC
                LIMIT ? OFFSET ?
                """,
                (*params, limit, offset),
            ).fetchall()
        return [self._row_to_record(row) for row in rows], total

    def get_message(self, message_id: int) -> StoredMessageRecord | None:
        """Return a single stored message by ID, or None if not found."""
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM processed_messages WHERE message_id = ?",
                (message_id,),
            ).fetchone()
        return self._row_to_record(row) if row else None

    @staticmethod
    def _row_to_record(row: sqlite3.Row) -> StoredMessageRecord:
        attachments_data: list[dict[str, Any]] = json.loads(row["attachments_json"])
        return StoredMessageRecord(
            message_id=row["message_id"],
            sender=row["sender"],
            to=json.loads(row["to_json"]),
            subject=row["subject"],
            body=row["body"],
            in_reply_to=row["in_reply_to"],
            references=json.loads(row["references_json"]),
            attachments=[MailboxAttachment.model_validate(item) for item in attachments_data],
            decision=ClassificationDecision(
                category=EmailCategory(row["category"]),
                confidence=row["confidence"],
                reasoning=row["reasoning"],
            ),
            model=row["model"],
            trace_id=row["trace_id"],
            received_at=row["received_at"],
            processed_at=row["processed_at"],
            raw=json.loads(row["raw_json"]),
        )

    # --- Quote workflow -------------------------------------------------------------------

    def save_shipment(self, shipment: Shipment) -> Shipment:
        """Insert or fully replace a shipment row; stamps created_at / updated_at."""
        now = _utc_now_iso()
        shipment.created_at = shipment.created_at or now
        shipment.updated_at = now
        with self._connect() as conn:
            conn.execute(
                """
                INSERT OR REPLACE INTO shipments (
                    id, customer, subject, language, state, request_json, goal, origin_airport,
                    zone, clarifications, answers_sent, pricing_started_at, quote_json,
                    quote_message_id, chosen_option_json, booking_ref, booking_attempts,
                    last_booking_attempt_at, error, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    shipment.id,
                    shipment.customer,
                    shipment.subject,
                    shipment.language,
                    shipment.state.value,
                    shipment.request.model_dump_json(),
                    shipment.goal,
                    shipment.origin_airport,
                    shipment.zone,
                    shipment.clarifications,
                    shipment.answers_sent,
                    shipment.pricing_started_at,
                    json.dumps(shipment.quote) if shipment.quote is not None else None,
                    shipment.quote_message_id,
                    shipment.chosen_option.model_dump_json() if shipment.chosen_option else None,
                    shipment.booking_ref,
                    shipment.booking_attempts,
                    shipment.last_booking_attempt_at,
                    shipment.error,
                    shipment.created_at,
                    shipment.updated_at,
                ),
            )
        return shipment

    @staticmethod
    def _row_to_shipment(row: sqlite3.Row) -> Shipment:
        data = dict(row)
        return Shipment(
            **{k: v for k, v in data.items() if not k.endswith("_json")},
            request=ShipmentRequest.model_validate_json(data["request_json"]),
            quote=json.loads(data["quote_json"]) if data["quote_json"] else None,
            chosen_option=(
                AirOption.model_validate_json(data["chosen_option_json"])
                if data["chosen_option_json"]
                else None
            ),
        )

    def get_shipment(self, shipment_id: int) -> Shipment | None:
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM shipments WHERE id = ?", (shipment_id,)).fetchone()
        return self._row_to_shipment(row) if row else None

    def list_shipments(self, state: ShipmentState | None = None) -> list[Shipment]:
        """All shipments, newest first, optionally only those in one state."""
        where, params = ("WHERE state = ?", (state.value,)) if state else ("", ())
        with self._connect() as conn:
            rows = conn.execute(
                f"SELECT * FROM shipments {where} ORDER BY id DESC", params
            ).fetchall()
        return [self._row_to_shipment(row) for row in rows]

    def save_trucker_request(self, request: TruckerRequest) -> None:
        """Insert a pickup request we sent, or update it with the trucker's answer."""
        with self._connect() as conn:
            conn.execute(
                """
                INSERT OR REPLACE INTO trucker_requests (
                    out_message_id, shipment_id, trucker, sent_at, answered, price_eur,
                    pickup_hours, valid_until, ref, reply_message_id
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    request.out_message_id,
                    request.shipment_id,
                    request.trucker,
                    request.sent_at,
                    int(request.answered),
                    request.price_eur,
                    request.pickup_hours,
                    request.valid_until,
                    request.ref,
                    request.reply_message_id,
                ),
            )

    def trucker_requests(self, shipment_id: int) -> list[TruckerRequest]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM trucker_requests WHERE shipment_id = ? ORDER BY out_message_id",
                (shipment_id,),
            ).fetchall()
        return [TruckerRequest(**dict(row)) for row in rows]

    def find_trucker_request(
        self, reply_to_id: int | None, trucker: str, match_by_sender: bool = False
    ) -> TruckerRequest | None:
        """The pickup request a trucker's mail answers: the one we sent as `reply_to_id`.

        `match_by_sender` is for dry runs, where our requests are never sent and the reply
        answers someone else's mail: then the trucker's newest unanswered request is used.
        """
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM trucker_requests WHERE out_message_id = ?", (reply_to_id,)
            ).fetchone()
            if row is None and match_by_sender:
                row = conn.execute(
                    """
                    SELECT * FROM trucker_requests WHERE trucker = ? AND answered = 0
                    ORDER BY sent_at DESC LIMIT 1
                    """,
                    (trucker,),
                ).fetchone()
        return TruckerRequest(**dict(row)) if row else None

    def trucker_quote_history(self) -> list[tuple[str | None, float, float]]:
        """Every stored trucker price as `(zone, price_eur, gross_kg)`, for the fallback price."""
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT s.zone, t.price_eur, json_extract(s.request_json, '$.gross_kg') AS kg
                FROM trucker_requests t JOIN shipments s ON s.id = t.shipment_id
                WHERE t.price_eur IS NOT NULL AND kg IS NOT NULL
                """
            ).fetchall()
        return [(row["zone"], row["price_eur"], row["kg"]) for row in rows]

    def replace_air_options(self, shipment_id: int, options: list[AirOption]) -> None:
        with self._connect() as conn:
            conn.execute("DELETE FROM air_options WHERE shipment_id = ?", (shipment_id,))
            conn.executemany(
                """
                INSERT INTO air_options (
                    shipment_id, source, airline, airline_code, origin, destination, billed_kg,
                    total_eur, transit_days, rate_id, valid_until, weight_kg, volume_cbm
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                [
                    (
                        shipment_id, o.source, o.airline, o.airline_code, o.origin, o.destination,
                        o.billed_kg, o.total_eur, o.transit_days, o.rate_id, o.valid_until,
                        o.weight_kg, o.volume_cbm,
                    )
                    for o in options
                ],
            )  # fmt: skip

    def air_options(self, shipment_id: int) -> list[AirOption]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM air_options WHERE shipment_id = ? ORDER BY total_eur",
                (shipment_id,),
            ).fetchall()
        return [AirOption(**{k: v for k, v in dict(row).items() if k != "shipment_id"}) for row in rows]

    @staticmethod
    def _row_to_outbound(row: sqlite3.Row) -> OutboundMail:
        data = dict(row)
        return OutboundMail(to=data.pop("recipient"), **data)

    def add_outbound(
        self,
        *,
        kind: str,
        to: str,
        subject: str,
        body: str,
        shipment_id: int | None = None,
        in_reply_to: int | None = None,
        used_template: bool = False,
    ) -> OutboundMail:
        """Record a composed mail before it is sent (or instead of sending, in a dry run)."""
        with self._connect() as conn:
            row = conn.execute(
                """
                INSERT INTO outbound_mails (
                    shipment_id, kind, recipient, subject, body, in_reply_to, used_template,
                    created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?) RETURNING *
                """,
                (shipment_id, kind, to, subject, body, in_reply_to, int(used_template), _utc_now_iso()),
            ).fetchone()
        return self._row_to_outbound(row)

    def mark_outbound_sent(self, outbound_id: int, sent_message_id: int) -> None:
        with self._connect() as conn:
            conn.execute(
                "UPDATE outbound_mails SET sent_message_id = ? WHERE id = ?",
                (sent_message_id, outbound_id),
            )

    def outbound_mails(self, shipment_id: int | None = None) -> list[OutboundMail]:
        """Composed mails, oldest first, of one shipment or of all."""
        where, params = ("WHERE shipment_id = ?", (shipment_id,)) if shipment_id else ("", ())
        with self._connect() as conn:
            rows = conn.execute(
                f"SELECT * FROM outbound_mails {where} ORDER BY id", params
            ).fetchall()
        return [self._row_to_outbound(row) for row in rows]

    def shipment_inbound(self, shipment_id: int) -> list[StoredMessageRecord]:
        """Mails we received for a shipment: the customer thread plus replies to our mails."""
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT * FROM processed_messages
                WHERE message_id = :id OR json_extract(references_json, '$[0]') = :id
                   OR in_reply_to IN (  -- a dry-run mail has the id -outbound.id
                       SELECT COALESCE(sent_message_id, -id) FROM outbound_mails
                       WHERE shipment_id = :id)
                   OR message_id IN (
                       SELECT reply_message_id FROM trucker_requests WHERE shipment_id = :id)
                ORDER BY message_id
                """,
                {"id": shipment_id},
            ).fetchall()
        return [self._row_to_record(row) for row in rows]

    def find_outbound(self, message_id: int) -> OutboundMail | None:
        """Our mail with this mailbox id. A dry-run mail has the id `-outbound.id`."""
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM outbound_mails WHERE sent_message_id = ? OR id = ?",
                (message_id, -message_id),
            ).fetchone()
        return self._row_to_outbound(row) if row else None
