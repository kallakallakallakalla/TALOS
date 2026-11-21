"""The last dry-run replay as a database the read-only views can open.

`scripts/replay.py` writes `data/replay/replay.db` (not committed) and `result.json`
(committed). On a fresh checkout only the JSON exists, so the database is rebuilt from it
and from the recorded mails in `tests/fixtures/mails.json`. Nothing here calls an LLM or the
platform.
"""

import json
from pathlib import Path

from app.mailbox_client import parse_mailbox_message
from app.schemas import (
    AirOption,
    ClassificationDecision,
    EmailCategory,
    OutboundMail,
    Shipment,
    TruckerRequest,
)
from app.storage import MessageStore

_ROOT = Path(__file__).resolve().parents[1]
REPLAY_DIR = _ROOT / "data/replay"
FIXTURES_DIR = _ROOT / "tests/fixtures"


def replay_labels(replay_dir: Path = REPLAY_DIR) -> list[dict]:
    """Per mail: the classifier's category and the hand label, from the last replay."""
    path = replay_dir / "result.json"
    return json.loads(path.read_text())["classified"] if path.exists() else []


def ensure_replay_db(replay_dir: Path = REPLAY_DIR, fixtures_dir: Path = FIXTURES_DIR) -> Path | None:
    """Path of the replay database, rebuilt from `result.json` if it is missing.

    Returns None when there is no replay to show.
    """
    db_path, result_path = replay_dir / "replay.db", replay_dir / "result.json"
    if db_path.exists():
        return db_path
    if not result_path.exists():
        return None
    result = json.loads(result_path.read_text())
    building = replay_dir / "replay.db.building"
    for leftover in replay_dir.glob("replay.db.building*"):
        leftover.unlink(missing_ok=True)
    store = MessageStore(str(building))

    for entry in result["shipments"]:
        store.save_shipment(Shipment.model_validate(entry["shipment"]), touch=False)
        for request in entry["trucker_requests"]:
            store.save_trucker_request(TruckerRequest.model_validate(request))
        store.replace_air_options(
            entry["shipment"]["id"], [AirOption.model_validate(o) for o in entry["air_options"]]
        )
        for mail in entry["outbound"]:
            store.import_outbound(OutboundMail.model_validate(mail))

    mails_path = fixtures_dir / "mails.json"
    recorded = json.loads(mails_path.read_text())["inbound"] if mails_path.exists() else []
    mails = {m["id"]: parse_mailbox_message(m) for m in recorded}
    for row in result["classified"]:
        if row["id"] in mails:
            decision = ClassificationDecision(
                category=EmailCategory(row["category"]), confidence=row["confidence"], reasoning=""
            )
            store.save_processed_message(mails[row["id"]], decision, model="replay")

    # Fold the write-ahead log into the one file and close it before it gets its final name.
    conn = store._connect()  # noqa: SLF001 - same package, one-off maintenance call
    try:
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    finally:
        conn.close()
    building.rename(db_path)
    for leftover in replay_dir.glob("replay.db.building*"):
        leftover.unlink(missing_ok=True)
    return db_path
