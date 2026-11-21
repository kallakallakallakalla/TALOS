"""Append every incoming and outgoing mail to two text files (YYMMDD_mail_incoming.txt / _outgoing.txt)."""

from __future__ import annotations

import os
import threading
from datetime import datetime
from pathlib import Path

_lock = threading.Lock()
SEP = "=" * 78


def _path(kind: str) -> Path:
    folder = Path(os.getenv("MAIL_LOG_DIR", "mail_logs"))
    folder.mkdir(parents=True, exist_ok=True)
    return folder / f"{datetime.now():%y%m%d}_mail_{kind}.txt"


def _write(kind: str, header: list[str], body: str) -> None:
    text = "\n".join([SEP, *header, "-" * 78, body.rstrip(), ""]) + "\n"
    with _lock, _path(kind).open("a", encoding="utf-8") as fh:
        fh.write(text)


def log_incoming(mail) -> None:
    _write(
        "incoming",
        [
            f"logged:   {datetime.now():%Y-%m-%d %H:%M:%S}",
            f"id:       {mail.id}",
            f"received: {mail.received_at.isoformat() if mail.received_at else '?'}",
            f"from:     {mail.sender}",
            f"subject:  {mail.subject}",
            f"reply:    {mail.is_reply} | attachments: {len(mail.attachments)}",
        ],
        mail.body,
    )


def log_outgoing(to: str, subject: str, body: str, in_reply_to: int | None, result: str) -> None:
    _write(
        "outgoing",
        [
            f"sent:        {datetime.now():%Y-%m-%d %H:%M:%S}",
            f"to:          {to}",
            f"subject:     {subject}",
            f"in_reply_to: {in_reply_to}",
            f"result:      {result}",
        ],
        body,
    )
