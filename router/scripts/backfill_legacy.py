"""Mark everything already in the mailbox as handled. Run once, at cutover, before the start.

    .venv/bin/python scripts/backfill_legacy.py [--db data/router.db]

After it ran, the router starts with the next new mail and never acts on an older thread:

- every mail in the mailbox is stored as processed (mails this router never classified get
  the category `other`; no LLM call) and the poll cursor moves to the newest mail
- every customer thread gets a closed shipment (`FAILED`, note "legacy: ..."), so a late
  reply in an old thread (thanks, accept, more details) finds a closed shipment and
  triggers nothing: no second quote, no booking

Read-only on the platform, idempotent, and it never touches a shipment that already exists.
"""

import argparse
import sys
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.config import get_settings  # noqa: E402
from app.mailbox_client import parse_mailbox_message  # noqa: E402
from app.reference import Reference  # noqa: E402
from app.schemas import (  # noqa: E402
    ClassificationDecision,
    EmailCategory,
    MailboxMessage,
    Shipment,
    ShipmentState,
)
from app.storage import MessageStore  # noqa: E402

LEGACY_NOTE = "legacy: handled by the old process before cutover"


def backfill(store: MessageStore, mails: list[MailboxMessage], reference: Reference) -> tuple[int, int]:
    """Store unseen mails as processed and close every customer thread.

    Returns `(mails_added, shipments_closed)`.
    """
    mails_added = shipments_closed = 0
    for msg in sorted(mails, key=lambda m: m.id):
        if not store.is_processed(msg.id):
            store.save_processed_message(
                msg,
                ClassificationDecision(
                    category=EmailCategory.OTHER, confidence=0.0, reasoning=LEGACY_NOTE
                ),
                model="none",
            )
            mails_added += 1
        store.set_since_id(msg.id)

        if reference.role_for_email(msg.sender) != "customer":
            continue
        # references[0] is the customer's first mail, which is the shipment id.
        root = msg.references[0] if msg.references else msg.in_reply_to or msg.id
        if store.get_shipment(root) is None:
            store.save_shipment(
                Shipment(
                    id=root,
                    customer=msg.sender or "",
                    subject=msg.subject,
                    state=ShipmentState.FAILED,
                    error=LEGACY_NOTE,
                )
            )
            shipments_closed += 1
    return mails_added, shipments_closed


def fetch_all_mails(settings) -> list[MailboxMessage]:
    client = httpx.Client(
        base_url=settings.hackloop_base_url,
        headers={"X-API-Key": settings.hackloop_api_key},
        timeout=20,
    )
    mails: list[MailboxMessage] = []
    since_id = 0
    while True:
        response = client.get("/v1/mailbox/messages", params={"since_id": since_id, "limit": 200})
        response.raise_for_status()
        page = response.json()
        mails += [parse_mailbox_message(m) for m in page["messages"]]
        if len(page["messages"]) < 200:
            return mails
        since_id = page["next_since_id"]


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--db", default=None, help="SQLite file (default: SQLITE_DB_PATH)")
    args = parser.parse_args()
    settings = get_settings()
    store = MessageStore(args.db or settings.sqlite_db_path)
    mails = fetch_all_mails(settings)
    added, closed = backfill(store, mails, Reference(settings.start_kit_dir))
    print(
        f"{len(mails)} mails in the mailbox: {added} newly stored, {closed} threads closed; "
        f"cursor at {store.get_since_id()}, {len(store.list_shipments())} shipments in total"
    )


if __name__ == "__main__":
    main()
