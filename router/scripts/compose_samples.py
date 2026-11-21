"""Write one sample of each LLM-written mail kind with the live composer prompt and print it.

    .venv/bin/python scripts/compose_samples.py [--label staging]

This is the human read for the composer (workflow.md section 6, point 5): the fact check
is automatic, tone and helpfulness are judged by reading. Nothing is sent.
"""

import argparse
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.composer import Composer  # noqa: E402
from app.config import get_settings  # noqa: E402
from app.llm import StructuredLLM  # noqa: E402

_SHIPMENT = {"ready_date": "2026-11-28", "pieces": 6, "gross_weight": "679 kg", "volume": "4.19 cbm"}
SAMPLES = [
    ("quote", "customer", "Deniz", "Air freight quote to ORD", {
        "route": "CGN -> ORD", "airline": "Xenon Cargo Airlines", "transit_days": 3,
        "all_in_price": "1599.41 EUR",
        "price_includes": "pickup at your site, air freight, fuel and security surcharges",
        "pickup_included": True, "pickup_from": "51896 Bergisch Gladbach", **_SHIPMENT,
        "estimated_pickup": "2026-11-28", "estimated_departure": "2026-11-30",
        "estimated_arrival": "2026-12-03", "extra_questions": ["earliest arrival at ORD"],
        "valid_until": "2026-11-21 15:34 UTC", "must_appear": ["1599.41 EUR"],
    }),
    ("clarification", "customer", "Mira", "Air freight quote to PVG", {
        "missing_fields": ["pickup address with postal code, or the German airport you deliver the goods to"],
        "must_appear": [],
    }),
    ("booking_note", "customer", "Deniz", "Air freight quote to ORD", {
        "route": "CGN -> ORD", "airline": "Xenon Cargo Airlines", "booking_ref": "BK-ERC5OOW3",
        "ready_date": "2026-11-28", "estimated_arrival": "2026-12-03",
        "pickup_included": True, "must_appear": ["BK-ERC5OOW3"],
    }),
    ("booking_request", "airline", "", None, {
        **_SHIPMENT, "airline": "Elbflug Air Freight", "lane": "CGN-ORD",
        "chargeable_weight": "699.73 kg", "tariff_total": "1700.00 EUR", "our_reference": "947",
        "must_appear": ["CGN-ORD", "947", "1700.00 EUR"],
    }),
    ("answer", "customer", "Deniz", "Air freight quote to ORD", {
        "question": "Can you tell me when the truck will come to collect?",
        "route": "CGN -> ORD", "estimated_pickup": "2026-11-28", "estimated_arrival": "2026-12-03",
        "must_appear": [],
    }),
]  # fmt: skip


async def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--label", default=None)
    args = parser.parse_args()
    settings = get_settings()
    llm = StructuredLLM(settings)
    composer = Composer(settings, llm)
    for kind, role, name, subject, facts in SAMPLES:
        mail, used_template = await composer.compose(
            kind, facts, recipient_role=role, recipient_name=name,
            reply_to_subject=subject, prompt_label=args.label,
        )  # fmt: skip
        print(f"\n===== {kind} | fact check {'FAILED -> template' if used_template else 'passed'}")
        print(f"Subject: {mail.subject}\n\n{mail.body}")
    llm.langfuse.flush()


if __name__ == "__main__":
    asyncio.run(main())
