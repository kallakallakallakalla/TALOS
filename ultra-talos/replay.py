"""Replay past RFQs through the desk agent in isolation and compare with what was really sent.

  ../router/.venv/bin/python replay.py 24812 25270 ...

Per RFQ the agent sees only the request and the carrier replies that followed it (not the
other system's quote, not the customer's verdict). Dry run. Result: logs/replay.json
"""

import csv
import os
import json
import shutil
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT / "tools"))
from api import call  # noqa: E402


def mailbox(since: int) -> list[dict]:
    out = []
    while True:
        status, page = call("GET", f"/v1/mailbox/messages?since_id={since}&limit=200")
        if status != 200 or not page["messages"]:
            return out
        out += page["messages"]
        since = page["next_since_id"]
        time.sleep(5.2)  # one poll per 5 s


def main(ids: list[int]) -> None:
    mails = mailbox(min(ids) - 1)
    with open(ROOT / "data" / "carriers.csv", newline="") as f:
        carriers = {r["email"] for r in csv.DictReader(f)}
    roots = sorted(m["id"] for m in mails if not m["references"] and m["from"] not in carriers and "tracking@" not in m["from"])
    results = []
    for rfq in ids:
        nxt = min((r for r in roots if r > rfq), default=10**12)
        live = ROOT / "mails"
        shutil.rmtree(ROOT / "mails.live", ignore_errors=True)
        live.rename(ROOT / "mails.live")
        live.mkdir()
        for m in mails:  # only what a lone desk would have had: the carrier replies of this round
            if rfq < m["id"] < nxt and m["from"] in carriers and not m["subject"].startswith("Booking"):
                (live / f"{m['id']}.json").write_text(json.dumps(m, indent=1, ensure_ascii=False))
        shutil.rmtree(ROOT / "shipments" / str(rfq), ignore_errors=True)
        before = set((ROOT / "outbox").glob("*.json"))
        start = time.time()
        subprocess.run([sys.executable, "main.py", "--once", str(rfq)], cwd=ROOT, capture_output=True,
                       env=os.environ | {"ULTRA_LLM": "test"})  # replays never use the scored gateway
        took = round(time.time() - start)
        ours = [json.loads(p.read_text())["body"] for p in sorted(set((ROOT / "outbox").glob("*.json")) - before)]
        ours = [b for b in ours if isinstance(b, dict) and b.get("in_reply_to") == rfq]
        shutil.rmtree(live)
        (ROOT / "mails.live").rename(live)
        # the real quote: what the customer's first reply in the thread answered
        reply = next((m for m in mails if m["references"][:1] == [rfq] and m["from"] not in carriers), None)
        theirs = call("GET", f"/v1/mailbox/messages/{reply['in_reply_to']}")[1] if reply else {}
        request = next(m for m in mails if m["id"] == rfq)
        results.append({"rfq": rfq, "subject": request["subject"], "seconds": took,
                        "ours": [b["body"] for b in ours], "theirs": theirs.get("body"),
                        "customer_verdict": reply["body"] if reply else None})
        (ROOT / "logs" / "replay.json").write_text(json.dumps(results, indent=1, ensure_ascii=False))
        print(f"{rfq} done in {took}s, our mails: {len(ours)}", flush=True)


if __name__ == "__main__":
    main([int(a) for a in sys.argv[1:]])
