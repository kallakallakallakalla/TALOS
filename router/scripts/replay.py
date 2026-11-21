"""Replay the recorded mailbox through the whole pipeline, as a dry run.

    .venv/bin/python scripts/replay.py [--rate-reads-per-minute 3] [--only 947 1056 ...]

Every recorded inbound mail (tests/fixtures/mails.json) goes through context lookup ->
classifier -> extractor -> pricing -> composer, exactly as in production, with:

- real LLM calls (gateway, the Langfuse prompts with the configured label)
- real portal rate reads, throttled and cached on disk (data/replay/rates.json), so a
  second run costs no reads
- a virtual clock set to each mail's received time, so trucker deadlines behave as they did
- SEND_ENABLED forced to false and a mailbox stub that cannot send: nothing leaves this machine

Results: data/replay/result.json (shipments, composed mails, classification per mail).
The recorded customer verdicts answered the OLD process's quotes; here they only drive the
state machine (accept -> dry-run booking).
"""

import argparse
import asyncio
import json
import sys
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.classifier import DecisionClassifier  # noqa: E402
from app.composer import Composer  # noqa: E402
from app.config import evaluation_settings  # noqa: E402
from app.extractors import Extractors  # noqa: E402
from app.llm import StructuredLLM  # noqa: E402
from app.mailbox_client import parse_mailbox_message  # noqa: E402
from app.poller import MailboxPoller  # noqa: E402
from app.portal_client import PortalClient  # noqa: E402
from app.reference import Reference  # noqa: E402
from app.schemas import AirOption, MailboxMessage  # noqa: E402
from app.storage import MessageStore  # noqa: E402
from app.tariffs import Tariffs  # noqa: E402
from app.workflow import Workflow  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / "tests/fixtures"
OUT_DIR = ROOT / "data/replay"


class FixtureMailbox:
    """Read-only mailbox over the recorded mails. It has no way to send."""

    def __init__(self, mails: dict[int, MailboxMessage]) -> None:
        self._mails = mails

    async def get_message(self, message_id: int) -> MailboxMessage:
        return self._mails[message_id]

    async def send(self, *args, **kwargs):
        raise RuntimeError("the replay never sends mail")


class CachedPortal(PortalClient):
    """Portal client that keeps every rate read on disk (prices are stable per dataset)."""

    def __init__(self, settings, cache_path: Path) -> None:
        super().__init__(settings)
        self._cache_path = cache_path
        self._cache = json.loads(cache_path.read_text()) if cache_path.exists() else {}
        self.live_reads = 0

    async def rate(self, origin, destination, airline_code, weight_kg, volume_cbm) -> AirOption:
        key = f"{origin}-{destination}-{airline_code}-{weight_kg:g}-{volume_cbm:g}"
        if key not in self._cache:
            option = await super().rate(origin, destination, airline_code, weight_kg, volume_cbm)
            self.live_reads += 1
            self._cache[key] = option.model_dump(mode="json")
            self._cache_path.write_text(json.dumps(self._cache, indent=1))
        return AirOption(**self._cache[key])


async def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--rate-reads-per-minute", type=int, default=3)
    parser.add_argument("--only", type=int, nargs="*", help="replay only these mail ids")
    args = parser.parse_args()

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    db_path = OUT_DIR / "replay.db"
    for leftover in OUT_DIR.glob("replay.db*"):
        leftover.unlink()

    settings = evaluation_settings().model_copy(
        update={
            "send_enabled": False,
            "sqlite_db_path": str(db_path),
            "portal_rate_limit_per_minute": args.rate_reads_per_minute,
        }
    )
    data = json.loads((FIXTURES / "mails.json").read_text())
    labels = json.loads((FIXTURES / "labels.json").read_text())
    everything = {m["id"]: parse_mailbox_message(m) for m in data["inbound"] + data["outbound"]}
    inbound = [parse_mailbox_message(m) for m in sorted(data["inbound"], key=lambda m: m["id"])]
    if args.only:
        inbound = [m for m in inbound if m.id in set(args.only)]

    clock = {"now": datetime.fromisoformat(inbound[0].created_at)}
    store = MessageStore(str(db_path))
    llm = StructuredLLM(settings)
    portal = CachedPortal(settings, OUT_DIR / "rates.json")
    mailbox = FixtureMailbox(everything)
    workflow = Workflow(
        settings, store, mailbox, portal, Reference(settings.start_kit_dir),
        Tariffs(settings.tariffs_path), Extractors(settings, llm), Composer(settings, llm),
        clock=lambda: clock["now"],
    )  # fmt: skip
    poller = MailboxPoller(settings, mailbox, DecisionClassifier(settings, llm=llm), store, workflow)

    classified = []
    for msg in inbound:
        clock["now"] = datetime.fromisoformat(msg.created_at)
        await workflow.tick()  # deadlines that passed before this mail arrived
        decision = await poller.process_message(msg)
        expected = labels.get(str(msg.id), {}).get("category")
        classified.append(
            {"id": msg.id, "from": msg.sender, "category": decision.category.value,
             "expected": expected, "confidence": decision.confidence}
        )  # fmt: skip
        mark = "" if decision.category.value == expected else f"   <-- expected {expected}"
        print(f"{msg.created_at[11:19]} {msg.id:>5} {decision.category.value:<21}{mark}", flush=True)
    clock["now"] += timedelta(seconds=settings.trucker_fallback_seconds + 1)
    await workflow.tick()

    shipments = []
    for shipment in reversed(store.list_shipments()):
        shipments.append(
            {
                "shipment": shipment.model_dump(mode="json"),
                "trucker_requests": [t.model_dump() for t in store.trucker_requests(shipment.id)],
                "air_options": [o.model_dump() for o in store.air_options(shipment.id)],
                "outbound": [m.model_dump() for m in store.outbound_mails(shipment.id)],
            }
        )
    (OUT_DIR / "result.json").write_text(
        json.dumps({"classified": classified, "shipments": shipments}, indent=1, default=str)
    )

    right = sum(c["category"] == c["expected"] for c in classified)
    print(f"\nclassification: {right}/{len(classified)} as labelled; live rate reads: {portal.live_reads}")
    for entry in shipments:
        s, quote = entry["shipment"], entry["shipment"]["quote"] or {}
        print(
            f"{s['id']:>5} {s['state']:<13} {s['origin_airport'] or '?'}-{s['request']['destination_iata']} "
            f"{quote.get('all_in_eur', '')} {quote.get('airline', '')} "
            f"truck={quote.get('truck_eur', '')}{' (fallback)' if quote.get('truck_fallback') else ''} "
            f"{s['error'] or ''}"
        )
    llm.langfuse.flush()


if __name__ == "__main__":
    asyncio.run(main())
