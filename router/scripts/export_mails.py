"""Export the team mailbox into tests/fixtures/mails.json (read-only on the platform).

    .venv/bin/python scripts/export_mails.py

`inbound` is every mail sent to us; `outbound` is every mail of ours that an inbound mail
replies to or references. New mails then need a label in tests/fixtures/labels.json before
`build_datasets.py` picks them up.
"""

import json
import sys
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.config import get_settings  # noqa: E402

FIXTURE = Path(__file__).resolve().parents[1] / "tests/fixtures/mails.json"


def main() -> None:
    settings = get_settings()
    client = httpx.Client(
        base_url=settings.hackloop_base_url,
        headers={"X-API-Key": settings.hackloop_api_key},
        timeout=20,
    )
    inbound: list[dict] = []
    since_id = 0
    while True:
        page = client.get("/v1/mailbox/messages", params={"since_id": since_id, "limit": 200}).json()
        inbound += page["messages"]
        if len(page["messages"]) < 200:
            break
        since_id = page["next_since_id"]

    received = {m["id"] for m in inbound}
    ours = {i for m in inbound for i in [m["in_reply_to"], *m["references"]] if i} - received
    outbound = []
    for message_id in sorted(ours):
        response = client.get(f"/v1/mailbox/messages/{message_id}")
        if response.status_code == 200:
            outbound.append(response.json())

    FIXTURE.write_text(json.dumps({"inbound": inbound, "outbound": outbound}, indent=1, ensure_ascii=False))
    print(f"{len(inbound)} inbound, {len(outbound)} outbound -> {FIXTURE}")


if __name__ == "__main__":
    main()
