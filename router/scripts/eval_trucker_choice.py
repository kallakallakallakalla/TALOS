"""Offline check of the trucker chooser (ADR 55) against past scored rounds.

    .venv/bin/python scripts/eval_trucker_choice.py [--board data/replay/board-me.json]

No platform API call: rounds come from local copies of the router databases and a saved
`GET /v1/board/me` snapshot. LLM calls use EVALUATION_KEY directly at OpenAI (never the
gateway) and the local prompt files. Per round the same functions as in production run:
`extract_trucker_quotes` on the stored replies (with attachments), `trucker_choice_facts`,
`choose_trucker`, `check_trucker_choice`, then `pick_best` with the chosen trucker. The cut-off
rule (ADR 49) is scored next to it.

The expected all-in price of a round follows from its accuracy points,
45 x (1 - 10 x deviation): the candidate total (any airline + any trucker) closest to one of
the two prices that deviation allows. Rejected rounds have no points and are not scored.
"""

import argparse
import asyncio
import json
import shutil
import sys
import tempfile
from datetime import date, datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import pricing  # noqa: E402
from app.config import evaluation_settings  # noqa: E402
from app.extractors import Extractors  # noqa: E402
from app.llm import StructuredLLM  # noqa: E402
from app.schemas import AirOption  # noqa: E402
from app.storage import MessageStore  # noqa: E402
from app.workflow import check_trucker_choice, trucker_choice_facts  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
DATABASES = ["data/router.backup-pre-updates.db", "data/router.backup-2119.db", "data/router.db"]


def _when(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def _points(price: float, expected: float) -> float:
    return max(0.0, 45 * (1 - 10 * abs(price - expected) / expected))


def load_rounds(board: Path, tmp: Path) -> list[dict]:
    """Scored rounds with at least two priced trucker offers, matched to their shipment."""
    stores, shipments = [], {}
    for i, db in enumerate(DATABASES):
        if not (ROOT / db).exists():
            continue
        copy = tmp / f"{i}.db"  # a copy: opening a store may add columns
        shutil.copy(ROOT / db, copy)
        store = MessageStore(str(copy))
        stores.append(store)
        for s in store.list_shipments():
            if s.quote and s.created_at:
                shipments[s.id] = (s, store)
    rounds = []
    for entry in json.loads(board.read_text())["requests"]:
        accuracy = (entry.get("points") or {}).get("accuracy")
        if accuracy is None or not shipments:
            continue
        sent = _when(entry["sent_at"])
        shipment, store = min(shipments.values(), key=lambda p: abs((_when(p[0].created_at) - sent).total_seconds()))
        if abs((_when(shipment.created_at) - sent).total_seconds()) > 120:
            continue
        requests = [r for r in store.trucker_requests(shipment.id) if r.reply_message_id]
        if sum(r.price_eur is not None for r in requests) < 2:
            continue
        rounds.append({"round": entry["round_no"], "accuracy": accuracy, "shipment": shipment,
                       "store": store, "requests": requests})  # fmt: skip
    return sorted(rounds, key=lambda r: r["round"])


async def evaluate(r: dict, extractors: Extractors, cutoff: float) -> dict:
    shipment, store, requests = r["shipment"], r["store"], r["requests"]
    request = shipment.request
    air = pricing.by_source(store.air_options(shipment.id))
    stored_prices = {x.out_message_id: x.price_eur for x in requests}
    # Re-read the replies like production does, so `conditions` are filled.
    texts = []
    for x in requests:
        m = store.get_message(x.reply_message_id)
        files = "\n\n".join(f"ATTACHMENT {a.filename}:\n{a.text}" for a in m.attachments if a.readable and a.text)
        texts.append(f"{m.body.strip()}\n\n{files}" if files else m.body)
    read = {q.reply: q for q in (await extractors.extract_trucker_quotes(texts)).quotes}
    for n, x in enumerate(requests, start=1):
        if q := read.get(n):
            x.pickup_hours = q.pickup_hours if q.pickup_hours is not None else x.pickup_hours
            x.valid_until, x.conditions = q.valid_until, q.conditions
    offers = [x for x in requests if x.price_eur is not None]  # prices as quoted then
    # The order time: when we quoted (offers were valid then).
    now = _when(shipment.created_at) + timedelta(minutes=1)
    facts = trucker_choice_facts(shipment, air, offers, now, lambda t: t.trucker.split("@")[0])
    choice = await extractors.choose_trucker(facts)
    problem = check_trucker_choice(choice.offer, offers, air, shipment, now)
    llm_truck = None if problem else offers[choice.offer - 1]

    def pick(trucks):
        return pricing.pick_best(air, trucks, shipment.goal, request.ready_date, request.deliver_by, cutoff)

    rule_air, rule_truck = pick(offers)
    llm_air, llm_truck = pick([llm_truck]) if llm_truck else (rule_air, rule_truck)
    ours, dev = shipment.quote["all_in_eur"], (1 - r["accuracy"] / 45) / 10
    candidates = [o.total_eur + t.price_eur for o in air for t in offers]
    allowed = [ours / (1 + dev), ours / (1 - dev)] if dev > 1e-9 else [ours]
    expected = min(allowed, key=lambda e: min(abs(e - c) for c in candidates))
    exp_truck = min(offers, key=lambda t: min(abs(o.total_eur + t.price_eur - expected) for o in air))
    rule_total = rule_air.total_eur + rule_truck.price_eur
    llm_total = llm_air.total_eur + llm_truck.price_eur
    return {
        "round": r["round"], "shipment": shipment.id, "goal": shipment.goal,
        "ready": request.ready_date.isoformat() if request.ready_date else None,
        "offers": sorted((t.price_eur, t.pickup_hours) for t in offers),
        "expected": (exp_truck.price_eur, exp_truck.pickup_hours),
        "rule": (rule_truck.price_eur, rule_truck.pickup_hours),
        "llm": (llm_truck.price_eur, llm_truck.pickup_hours),
        "rule_points": round(_points(rule_total, expected), 2),
        "llm_points": round(_points(llm_total, expected), 2),
        "llm_reason": choice.reason, "llm_concerns": choice.concerns, "check_failed": problem,
        "conditions": {t.price_eur: t.conditions for t in offers},
        "prices_unchanged": all(stored_prices[t.out_message_id] == t.price_eur for t in offers),
    }  # fmt: skip


async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--board", type=Path, default=ROOT / "data/replay/board-me.json")
    parser.add_argument("--out", type=Path, default=ROOT / "data/replay/trucker-choice-eval.json")
    args = parser.parse_args()
    settings = evaluation_settings().model_copy(update={"langfuse_enabled": False})  # local prompts
    assert "api.openai.com" in settings.decision_api_base_url, "eval must go straight to OpenAI"
    extractors = Extractors(settings, StructuredLLM(settings))
    with tempfile.TemporaryDirectory() as tmp:
        rounds = load_rounds(args.board, Path(tmp))
        results = await asyncio.gather(*(evaluate(r, extractors, settings.trucker_pickup_cutoff_hours) for r in rounds))
    print(f"{'rnd':>3} {'goal':8} {'expected':>11} {'rule 14h':>11} {'llm':>11}  rule  llm")
    for x in results:
        f = lambda t: f"{t[0]:.2f}@{t[1]:g}"  # noqa: E731
        mark = "" if x["llm"] == x["expected"] else "  <-"
        print(f"{x['round']:>3} {x['goal'] or '-':8} {f(x['expected']):>11} {f(x['rule']):>11} {f(x['llm']):>11}"
              f"  {x['rule_points']:4.1f} {x['llm_points']:4.1f}{mark}")  # fmt: skip
    n = len(results)
    rule_hits = sum(x["rule"] == x["expected"] for x in results)
    llm_hits = sum(x["llm"] == x["expected"] for x in results)
    print(f"\nrounds {n}: trucker as expected - rule {rule_hits}/{n}, llm {llm_hits}/{n}")
    print(f"accuracy points - rule {sum(x['rule_points'] for x in results):.1f}, "
          f"llm {sum(x['llm_points'] for x in results):.1f} (max {45 * n})")  # fmt: skip
    print(f"checks failed: {sum(bool(x['check_failed']) for x in results)}; "
          f"prices changed by re-reading: {sum(not x['prices_unchanged'] for x in results)}")
    args.out.write_text(json.dumps(results, indent=1, default=str))
    print(f"details: {args.out}")


if __name__ == "__main__":
    asyncio.run(main())
