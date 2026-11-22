"""How fast we answered, measured on the real mails in archive/mails.json.

    .venv/bin/python scripts/reply_times.py

Both times of a pair are the platform's own (when it received the customer's mail, when it
received ours), so the difference is what the customer experienced. Two measures:

- request -> first answer: each customer request, and our first mail to that customer in the
  same thread (a quote, or a question back);
- every direct reply: each mail of ours that answers one specific inbound mail.

A mail of ours with a database row was written by this router; one without was sent by the
earlier process or by hand. Refresh the archive first with scripts/export_all_mails.py.
"""

import csv
import json
import statistics
from collections import defaultdict
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
ROUND_WINDOW_S = 360  # a round closes 6 minutes after it is sent


def when(mail: dict) -> datetime:
    return datetime.fromisoformat(mail["raw"]["received_at"])


def line(label: str, seconds: list[float]) -> None:
    if not seconds:
        print(f"{label:<46} none")
        return
    ordered = sorted(seconds)
    p90 = ordered[min(len(ordered) - 1, int(0.9 * len(ordered)))]
    print(
        f"{label:<46} n={len(ordered):<4} median {statistics.median(ordered):>4.0f} s   mean {statistics.fmean(ordered):>5.0f} s   "
        f"90% within {p90:>4.0f} s   within 60 s: {sum(s <= 60 for s in ordered)}   later than 6 min: {sum(s > ROUND_WINDOW_S for s in ordered)}"
    )


def main() -> None:
    mails = json.loads((ROOT / "archive" / "mails.json").read_text())["mails"]
    with (ROOT.parent / "data" / "start-kit" / "carrier-list.csv").open() as handle:
        carriers = {row["email"].strip().lower() for row in csv.DictReader(handle)}
    by_id = {m["id"]: m for m in mails}
    threads: dict[int, list[dict]] = defaultdict(list)
    for mail in mails:
        threads[mail["thread_id"]].append(mail)
    ours = [m for m in mails if m["direction"] == "outbound"]
    theirs = [m for m in mails if m["direction"] == "inbound"]
    print(f"{len(theirs)} mails received, {len(ours)} sent, {min(map(when, mails)):%d %b %H:%M} to {max(map(when, mails)):%d %b %H:%M} UTC\n")

    def customer(address: str) -> bool:
        return address not in carriers and address.split("@")[0] not in ("tracking", "ratedesk", "talos")

    requests = [m for m in theirs if (m["db"] or {}).get("category", "").lower() == "rfq" and customer(m["raw"]["from"].lower())]
    firsts = []
    for request in requests:
        answers = [o for o in threads[request["thread_id"]]
                   if o["direction"] == "outbound" and o["raw"]["to"].lower() == request["raw"]["from"].lower() and when(o) > when(request)]  # fmt: skip
        if answers:
            first = min(answers, key=when)
            pickup = any((o["db"] or {}).get("kind") == "trucker_request" for o in ours
                         if o["db"] and o["db"].get("shipment_id") in request["shipment_ids"])  # fmt: skip
            firsts.append(((when(first) - when(request)).total_seconds(), bool(first["db"]), pickup))
    print(f"Request -> first answer: {len(firsts)} of {len(requests)} requests answered")
    line("  everything", [s for s, _, _ in firsts])
    line("  this router", [s for s, router, _ in firsts if router])
    line("    with pickup (truckers asked)", [s for s, router, pickup in firsts if router and pickup])
    line("    without pickup", [s for s, router, pickup in firsts if router and not pickup])
    line("  earlier process or by hand", [s for s, router, _ in firsts if not router])

    direct = [((when(o) - when(by_id[o["raw"]["in_reply_to"]])).total_seconds(), o) for o in ours
              if o["raw"].get("in_reply_to") in by_id and by_id[o["raw"]["in_reply_to"]]["direction"] == "inbound"]  # fmt: skip
    direct = [(seconds, o) for seconds, o in direct if seconds > 0]
    print("\nEvery direct reply to one inbound mail")
    line("  everything", [s for s, _ in direct])
    line("  this router", [s for s, o in direct if o["db"]])
    for kind in ("quote", "clarification", "booking_note", "tracking_answer"):
        line(f"    {kind.replace('_', ' ')}", [s for s, o in direct if (o["db"] or {}).get("kind") == kind])


if __name__ == "__main__":
    main()
