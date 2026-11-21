"""Apply update mails that were classified `tariff_update` but never handled (ADR 50).

    .venv/bin/python scripts/apply_pending_updates.py            # live DB, as configured
    .venv/bin/python scripts/apply_pending_updates.py --dry-run  # list them only

Safety net for mails that arrived before the update handler was deployed. Each mail runs
through the same handler as a new mail (read, flag, check, put, confirm). A mail that already
has a `data_updates` row is skipped, so running this twice changes nothing.
"""

import argparse
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.config import configure_langfuse_env, get_settings  # noqa: E402
from app.mailbox_client import MailboxClient  # noqa: E402
from app.main import build_workflow  # noqa: E402
from app.schemas import MailboxMessage  # noqa: E402
from app.storage import MessageStore  # noqa: E402


async def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--dry-run", action="store_true", help="only list the pending mails")
    args = parser.parse_args()

    settings = get_settings()
    configure_langfuse_env(settings)
    store = MessageStore(settings.sqlite_db_path)
    pending = [u for u in store.data_updates() if u.status == "not_handled"]
    print(f"{len(pending)} update mail(s) not handled yet")
    if args.dry_run or not pending:
        for u in pending:
            print(f"  #{u.message_id} {u.sender}: {u.subject}")
        return
    workflow = build_workflow(settings, store, MailboxClient(settings))
    for u in reversed(pending):  # oldest first, like the mailbox
        record = store.get_message(u.message_id)
        msg = MailboxMessage(
            id=record.message_id, sender=record.sender, to=record.to, subject=record.subject,
            body=record.body, in_reply_to=record.in_reply_to, references=record.references,
            attachments=record.attachments, created_at=record.received_at,
        )  # fmt: skip
        for att in msg.attachments:  # stored text was cut at the customer limit: read again
            att.text, att.vision_tried = "", False
        await workflow.read_attachments(msg)
        await workflow.handle(msg, record.decision, await workflow.context(msg))
        print(f"  #{u.message_id} {u.sender}: done")


if __name__ == "__main__":
    asyncio.run(main())
