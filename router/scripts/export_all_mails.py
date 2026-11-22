"""Export every mail we received and sent into archive/mails.json (read-only on the platform).

    .venv/bin/python scripts/export_all_mails.py

Each mail keeps the platform's JSON untouched in `raw` (enough to replay it) and the columns
only our database knows in `db` (category, shipment, kind, ...). `thread_id` groups mails that
reply to or reference each other; `shipment_ids` says which shipment(s) that thread is about.
Attachments land in archive/attachments/<mail id>/<attachment id>-<filename>.
"""

import json
import sqlite3
import sys
import time
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.config import get_settings  # noqa: E402

ARCHIVE = ROOT / "archive"
INBOUND_DB_ONLY = ("category", "confidence", "reasoning", "model", "trace_id", "processed_at")


def rows(db: Path, sql: str) -> list[dict]:
    try:
        con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
        con.row_factory = sqlite3.Row
        return [dict(r) for r in con.execute(sql)]
    except sqlite3.Error:  # backup without that table, or unreadable
        return []


def get(client: httpx.Client, path: str, **kwargs) -> httpx.Response:
    """GET that waits out the platform's rate limit (429) instead of failing."""
    for attempt in range(30):
        response = client.get(path, **kwargs)
        if response.status_code != 429:
            return response
        retry_after = response.headers.get("retry-after", "")
        time.sleep(float(retry_after) if retry_after.replace(".", "", 1).isdigit() else min(2 + attempt * 2, 30))
    response.raise_for_status()
    return response


def refs(raw: dict) -> list[int]:
    return [i for i in [raw.get("in_reply_to"), *(raw.get("references") or [])] if i]


def main() -> None:
    settings = get_settings()
    client = httpx.Client(
        base_url=settings.hackloop_base_url,
        headers={"X-API-Key": settings.hackloop_api_key},
        timeout=30,
    )
    # Live database first, so its rows win over the older backups.
    dbs = [Path(settings.sqlite_db_path), *sorted((ROOT / "data").glob("router*.db"), reverse=True)]

    # --- inbound: the platform's list, plus anything only a database still has
    inbound: dict[int, dict] = {}
    since_id = 0
    while True:
        page = get(client, "/v1/mailbox/messages", params={"since_id": since_id, "limit": 200})
        page.raise_for_status()
        page = page.json()
        inbound.update({m["id"]: m for m in page["messages"]})
        if len(page["messages"]) < 200:
            break
        since_id = page["next_since_id"]
    inbound_db: dict[int, dict] = {}
    for db in dbs:
        for r in rows(db, "SELECT * FROM processed_messages"):
            inbound_db.setdefault(r["message_id"], r)
            inbound.setdefault(r["message_id"], json.loads(r["raw_json"]))

    # --- outbound: every id a database says we sent, plus every id an inbound mail answers
    outbound_db: dict[int, dict] = {}
    drafts: list[dict] = []
    shipment_of: dict[int, int] = {}  # mail id -> shipment id
    for db in dbs:
        for r in rows(db, "SELECT * FROM outbound_mails"):
            if r["sent_message_id"] is None:
                if db == dbs[0]:
                    drafts.append(r)  # composed in a dry run, never sent
                continue
            outbound_db.setdefault(r["sent_message_id"], r)
        for table in ("trucker_requests", "airline_requests"):
            for r in rows(db, f"SELECT out_message_id, shipment_id FROM {table}"):
                shipment_of.setdefault(r["out_message_id"], r["shipment_id"])
    fixture = json.loads((ROOT / "tests/fixtures/mails.json").read_text())
    fallback = {m["id"]: m for m in fixture["outbound"]}
    wanted = (set(outbound_db) | set(shipment_of) | set(fallback) | {i for m in inbound.values() for i in refs(m)}) - set(inbound)
    outbound: dict[int, dict | None] = {}
    for message_id in sorted(wanted):
        response = get(client, f"/v1/mailbox/messages/{message_id}")
        if response.status_code == 200:
            outbound[message_id] = response.json()
        elif message_id in fallback or message_id in outbound_db:
            outbound[message_id] = fallback.get(message_id)  # platform lost it; db row still has the text
    for message_id, r in outbound_db.items():
        if r["shipment_id"]:
            shipment_of.setdefault(message_id, r["shipment_id"])

    shipments = {r["id"]: r for db in reversed(dbs) for r in rows(db, "SELECT * FROM shipments")}
    for shipment_id in shipments:  # a shipment's id is the customer's first mail
        if shipment_id in inbound:
            shipment_of.setdefault(shipment_id, shipment_id)

    # --- threads: union-find over in_reply_to / references
    parent: dict[int, int] = {}

    def find(i: int) -> int:
        while parent.setdefault(i, i) != i:
            parent[i] = i = parent[parent[i]]
        return i

    def link(mail_id: int, raw: dict | None, db_row: dict | None) -> None:
        for other in refs(raw or {}) + ([db_row["in_reply_to"]] if db_row and db_row.get("in_reply_to") else []):
            a, b = find(mail_id), find(other)
            parent[max(a, b)] = min(a, b)  # thread id = oldest mail in it

    for i, raw in inbound.items():
        link(i, raw, None)
    for i, raw in outbound.items():
        link(i, raw, outbound_db.get(i))

    thread_shipments: dict[int, set[int]] = {}
    for mail_id, shipment_id in shipment_of.items():
        thread_shipments.setdefault(find(mail_id), set()).add(shipment_id)

    mails = []
    for i, raw in inbound.items():
        db_row = inbound_db.get(i)
        mails.append({"id": i, "direction": "inbound", "raw": raw,
                      "db": {k: db_row[k] for k in INBOUND_DB_ONLY} if db_row else None})
    for i, raw in outbound.items():
        mails.append({"id": i, "direction": "outbound", "raw": raw, "db": outbound_db.get(i)})
    mails.sort(key=lambda m: m["id"])
    for m in mails:
        m["thread_id"] = find(m["id"])
        m["shipment_ids"] = sorted(thread_shipments.get(m["thread_id"], ()))
    threads: dict[int, list[int]] = {}
    for m in mails:
        threads.setdefault(m["thread_id"], []).append(m["id"])

    # --- attachments
    saved = 0
    for m in mails:
        for a in (m["raw"] or {}).get("attachments") or []:
            target = ARCHIVE / "attachments" / str(m["id"]) / f"{a['id']}-{Path(a['filename']).name}"
            if not target.exists():
                response = get(client, f"/v1/mailbox/messages/{m['id']}/attachments/{a['id']}")
                if response.status_code != 200:
                    print(f"attachment {a['id']} of mail {m['id']}: HTTP {response.status_code}")
                    continue
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(response.content)
            saved += 1

    ARCHIVE.mkdir(exist_ok=True)
    (ARCHIVE / "mails.json").write_text(json.dumps({
        "mails": mails,
        "threads": [{"thread_id": t, "shipment_ids": sorted(thread_shipments.get(t, ())), "mail_ids": ids}
                    for t, ids in sorted(threads.items())],
        "shipments": list(shipments.values()),
        "unsent_drafts": drafts,
    }, indent=1, ensure_ascii=False))

    # Self-check: nothing lost, every mail sits in exactly one thread.
    assert len({m["id"] for m in mails}) == len(mails) == sum(len(ids) for ids in threads.values())
    assert set(inbound_db) <= set(inbound) and set(outbound_db) <= set(outbound)
    n_in = sum(m["direction"] == "inbound" for m in mails)
    print(f"{n_in} inbound, {len(mails) - n_in} outbound "
          f"({sum(m['raw'] is None for m in mails)} without platform copy), {len(threads)} threads, "
          f"{sum(bool(m['shipment_ids']) for m in mails)} mails tied to a shipment, "
          f"{saved} attachments, {len(drafts)} unsent drafts -> {ARCHIVE / 'mails.json'}")


if __name__ == "__main__":
    main()
