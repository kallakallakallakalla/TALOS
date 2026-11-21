import json
import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from app.schemas import (
    ClassificationDecision,
    EmailCategory,
    MailboxAttachment,
    MailboxMessage,
    StoredMessageRecord,
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
