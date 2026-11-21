"""Figures for the CRM, computed from stored rows only.

Everything here is derived from the database (shipments, air options, trucker requests,
composed mails, classified mails) and the start-kit reference files. Nothing is estimated.
A figure that cannot be measured from what is stored is left out, and `limits` in the
snapshot says why: in a dry-run replay the clocks are mixed and the recorded customer
verdicts answered the old process's quotes, so timings and win rates are not judged there.
"""

import statistics
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from typing import Any

from app.airports import AIRPORTS, distance_km
from app.config import Settings
from app.extractors import missing_fields
from app.reference import Reference
from app.schemas import AirOption, OutboundMail, Shipment, ShipmentState, TruckerRequest
from app.storage import MessageStore
from app.workflow import DRY_RUN_BOOKING_REF, needs_pickup

S = ShipmentState
STATE_ORDER = [
    S.NEW, S.AWAITING_INFO, S.PRICING, S.QUOTED, S.BOOKING, S.BOOKED, S.CONFIRMED,
    S.REJECTED, S.FAILED,
]  # fmt: skip
ACCEPTED = {S.BOOKING, S.BOOKED, S.CONFIRMED}
OPEN = {S.NEW, S.AWAITING_INFO, S.PRICING, S.QUOTED}
VOLUME_KG_PER_CBM = 167.0  # chargeable weight rule of every tariff (docs/data.md)
ROUND_WINDOW_MINUTES = 6.0  # a round closes 6 minutes after it is sent (docs/scoring.md)
LOW_CONFIDENCE = 0.7  # same threshold as the poller
STUCK_AFTER_MINUTES = 5.0  # pricing normally ends within the trucker wait (60 s)
_LEGACY = "legacy:"  # error text written by scripts/backfill_legacy.py


# --- small helpers ---------------------------------------------------------------------------


def _median(values: list[float]) -> float | None:
    return round(statistics.median(values), 2) if values else None


def _mean(values: list[float]) -> float | None:
    return round(statistics.fmean(values), 2) if values else None


def _pct(part: float, whole: float) -> float | None:
    return round(100 * part / whole, 1) if whole else None


def _parse_time(text: str | None) -> datetime | None:
    """ISO timestamps, and the quote validity format "2026-11-21 15:34 UTC"."""
    if not text:
        return None
    value = text.strip().replace("Z", "+00:00")
    if value.endswith(" UTC"):
        value = value[:-4].replace(" ", "T") + "+00:00"
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def _minutes(start: str | None, end: str | None) -> float | None:
    a, b = _parse_time(start), _parse_time(end)
    return round((b - a).total_seconds() / 60, 1) if a and b and b >= a else None


def _pretty(email: str | None) -> str:
    """"auer-maschinenbau@hackloop.mail" -> "Auer Maschinenbau"."""
    local = (email or "").split("@")[0]
    return " ".join(part.capitalize() for part in local.replace("_", "-").replace(".", "-").split("-") if part)


def _eur(value: float) -> str:
    return f"{value:,.0f} EUR"


def _n(count: int, noun: str) -> str:
    """"1 request", "3 requests"."""
    return f"{count} {noun}" if count == 1 else f"{count} {noun}s"


# --- data ------------------------------------------------------------------------------------


@dataclass
class Dataset:
    """Everything the CRM reads, loaded once per request."""

    shipments: list[Shipment]
    truckers: dict[int, list[TruckerRequest]]
    options: dict[int, list[AirOption]]
    outbound: dict[int | None, list[OutboundMail]]
    mails: list[dict[str, Any]]  # newest first, without bodies
    failures: list[dict[str, Any]]

    @classmethod
    def load(cls, store: MessageStore) -> "Dataset":
        truckers: dict[int, list[TruckerRequest]] = defaultdict(list)
        for request in store.all_trucker_requests():
            truckers[request.shipment_id].append(request)
        outbound: dict[int | None, list[OutboundMail]] = defaultdict(list)
        for mail in store.outbound_mails():
            outbound[mail.shipment_id].append(mail)
        return cls(
            shipments=store.list_shipments(),
            truckers=truckers,
            options=store.all_air_options(),
            outbound=outbound,
            mails=store.mail_index(),
            failures=store.message_failures(),
        )

    def mail_shipments(self) -> dict[int, int]:
        """Which shipment an inbound mail belongs to: its thread, or a reply to one of our mails."""
        ours = {
            (mail.sent_message_id if mail.sent_message_id is not None else -mail.id): mail.shipment_id
            for mails in self.outbound.values()
            for mail in mails
            if mail.shipment_id is not None
        }
        replies = {
            request.reply_message_id: request.shipment_id
            for requests in self.truckers.values()
            for request in requests
            if request.reply_message_id is not None
        }
        ids = {s.id for s in self.shipments}
        linked: dict[int, int] = {}
        for mail in self.mails:
            root = mail["references"][0] if mail["references"] else None
            shipment = (
                mail["id"] if mail["id"] in ids
                else root if root in ids
                else ours.get(mail["in_reply_to"]) or replies.get(mail["id"])
            )  # fmt: skip
            if shipment is not None:
                linked[mail["id"]] = shipment
        return linked


def is_legacy(shipment: Shipment) -> bool:
    """Handled by the old process before cutover: no request data, no prices."""
    return bool(shipment.error and shipment.error.startswith(_LEGACY))


# --- one shipment as a deal ------------------------------------------------------------------


def _air_figures(options: list[AirOption], chosen: AirOption | None) -> dict[str, Any] | None:
    """How the chosen flight compares with everything that was priced for the shipment."""
    if not options:
        return None
    cheapest = min(options, key=lambda o: (o.total_eur, o.transit_days))
    fastest_days = min(o.transit_days for o in options)
    cheapest_fast = min((o for o in options if o.transit_days == fastest_days), key=lambda o: o.total_eur)
    dearest = max(o.total_eur for o in options)
    figures: dict[str, Any] = {
        "n": len(options),
        "cheapest_eur": cheapest.total_eur,
        "cheapest_airline": cheapest.airline,
        "cheapest_days": cheapest.transit_days,
        "dearest_eur": dearest,
        "fastest_days": fastest_days,
        "fastest_eur": cheapest_fast.total_eur,
        "fastest_airline": cheapest_fast.airline,
        "spread_pct": _pct(dearest - cheapest.total_eur, cheapest.total_eur) if len(options) > 1 else None,
        # what arriving sooner costs on this shipment, if the fastest flight is not the cheapest
        "speed_days": cheapest.transit_days - fastest_days,
        "speed_eur": round(cheapest_fast.total_eur - cheapest.total_eur, 2),
    }
    if chosen:
        figures["chosen_rank"] = 1 + sum(o.total_eur < chosen.total_eur for o in options)
        figures["premium_eur"] = round(chosen.total_eur - cheapest.total_eur, 2)
        figures["premium_pct"] = _pct(chosen.total_eur - cheapest.total_eur, cheapest.total_eur)
    return figures


def _truck_figures(requests: list[TruckerRequest], chosen: str | None) -> dict[str, Any] | None:
    if not requests:
        return None
    prices = [r.price_eur for r in requests if r.answered and r.price_eur is not None]
    figures: dict[str, Any] = {"asked": len(requests), "answered": len(prices)}
    if prices:
        figures |= {
            "cheapest_eur": min(prices),
            "dearest_eur": max(prices),
            "median_eur": _median(prices),
            "spread_pct": _pct(max(prices) - min(prices), min(prices)) if len(prices) > 1 else None,
        }
        chosen_price = next((r.price_eur for r in requests if r.trucker == chosen and r.price_eur), None)
        if chosen_price is not None:
            figures["chosen_is_cheapest"] = chosen_price <= min(prices)
    return figures


def deal(shipment: Shipment, data: Dataset, *, live: bool, received: dict[int, str | None]) -> dict[str, Any]:
    """One shipment, flattened, with the figures the lists and the insights need."""
    request, quote = shipment.request, shipment.quote or {}
    options, truckers = data.options.get(shipment.id, []), data.truckers.get(shipment.id, [])
    outbound = data.outbound.get(shipment.id, [])
    legacy = is_legacy(shipment)

    chargeable = None
    if request.gross_kg:
        chargeable = round(max(request.gross_kg, (request.volume_cbm or 0) * VOLUME_KG_PER_CBM), 2)
    billed = quote.get("billed_kg")
    truck_mode = None
    if quote:
        truck_mode = "estimate" if quote.get("truck_fallback") else "quoted" if quote.get("trucker") else "none"
    quote_mail = next((m for m in outbound if m.kind == "quote"), None)
    destination = request.destination_iata
    assumptions = [
        label
        for flag, label in (
            (request.weight_computed, "weight computed from the pieces"),
            (request.volume_computed, "volume computed from the dimensions"),
            (request.destination_inferred, "destination airport inferred"),
        )
        if flag
    ]
    all_in = quote.get("all_in_eur")
    return {
        "id": shipment.id,
        "account": shipment.customer,
        "company": request.company or _pretty(shipment.customer),
        "contact": request.contact_name,
        "subject": shipment.subject,
        "language": shipment.language,
        "state": shipment.state.value,
        "legacy": legacy,
        "dry_run_booking": shipment.booking_ref == DRY_RUN_BOOKING_REF,
        "goal": shipment.goal,
        "origin": shipment.origin_airport,
        "destination": destination,
        "lane": f"{shipment.origin_airport}-{destination}" if shipment.origin_airport and destination else None,
        "distance_km": distance_km(shipment.origin_airport, destination),
        "zone": shipment.zone,
        "pickup_city": request.pickup_city,
        "pickup_postal_code": request.pickup_postal_code,
        "pickup": needs_pickup(shipment) if not legacy else None,
        "ready_date": request.ready_date.isoformat() if request.ready_date else None,
        "pieces": request.pieces,
        "gross_kg": request.gross_kg,
        "volume_cbm": request.volume_cbm,
        "chargeable_kg": chargeable,
        "volumetric": bool(chargeable and request.gross_kg and chargeable > request.gross_kg),
        "commodity": request.commodity,
        # what main's extractor reads besides the basics (ADR 43 to 46)
        "dangerous_goods": request.dangerous_goods,
        "dg_documents_received": request.dg_documents_received if request.dangerous_goods else None,
        "special_cargo": request.special_cargo,
        "deliver_by": request.deliver_by.isoformat() if request.deliver_by else None,
        "preferred_airline": request.preferred_airline,
        "preferred_trucker": request.preferred_trucker,
        "excluded_carriers": request.excluded_carriers,
        "extra_questions": request.extra_questions,
        "assumptions": assumptions,
        "missing": missing_fields(request) if shipment.state == S.AWAITING_INFO else [],
        "all_in_eur": all_in,
        "air_eur": quote.get("air_eur"),
        "truck_eur": quote.get("truck_eur"),
        "truck_mode": truck_mode,
        "airline": quote.get("airline"),
        "air_source": quote.get("air_source"),
        "transit_days": quote.get("transit_days"),
        "billed_kg": billed,
        # billed above the chargeable weight: the next weight break was cheaper
        "weight_break": bool(billed and chargeable and billed > chargeable + 0.5),
        "air_eur_per_kg": round(quote["air_eur"] / billed, 3) if billed and quote.get("air_eur") else None,
        "trucker": quote.get("trucker"),
        "valid_until": quote.get("valid_until"),
        "estimated_pickup": quote.get("estimated_pickup"),
        "estimated_departure": quote.get("estimated_departure"),
        "estimated_arrival": quote.get("estimated_arrival"),
        "booking_ref": shipment.booking_ref,
        "booking_attempts": shipment.booking_attempts,
        "last_booking_attempt_at": shipment.last_booking_attempt_at,
        "clarifications": shipment.clarifications,
        "answers_sent": shipment.answers_sent,
        "error": shipment.error,
        "received_at": received.get(shipment.id),
        "updated_at": shipment.updated_at,
        # request to quote, wall clock; a dry run mixes two clocks, so it has no such figure
        "minutes_to_quote": _minutes(received.get(shipment.id), quote_mail.created_at) if live and quote_mail else None,
        "air": _air_figures(options, shipment.chosen_option),
        "truck": _truck_figures(truckers, quote.get("trucker")),
        "mails_out": len(outbound),
        "templates_used": sum(m.used_template for m in outbound),
    }


# --- aggregates ------------------------------------------------------------------------------


def _outcome(deals: list[dict], live: bool) -> dict[str, Any]:
    """Accepted and rejected quotes. The win rate only where the verdicts answer our own quotes."""
    accepted = [d for d in deals if d["state"] in ACCEPTED]
    rejected = [d for d in deals if d["state"] == S.REJECTED]
    return {
        "accepted": len(accepted),
        "rejected": len(rejected),
        "win_rate_pct": _pct(len(accepted), len(accepted) + len(rejected)) if live else None,
        "accepted_eur": round(sum(d["all_in_eur"] or 0 for d in accepted), 2),
    }


def kpis(deals: list[dict], live: bool) -> dict[str, Any]:
    quoted = [d for d in deals if d["all_in_eur"] is not None]
    waiting = [d for d in deals if d["state"] == S.QUOTED]
    minutes = [d["minutes_to_quote"] for d in deals if d["minutes_to_quote"] is not None]
    return {
        "requests": len(deals),
        "quoted": len(quoted),
        "open": sum(d["state"] in OPEN for d in deals),
        "failed": sum(d["state"] == S.FAILED for d in deals),
        **_outcome(deals, live),
        "quoted_eur": round(sum(d["all_in_eur"] for d in quoted), 2),
        "awaiting_decision": len(waiting),
        "awaiting_decision_eur": round(sum(d["all_in_eur"] or 0 for d in waiting), 2),
        "median_all_in_eur": _median([d["all_in_eur"] for d in quoted]),
        "median_minutes_to_quote": _median(minutes),
        "quotes_timed": len(minutes),
        "quotes_in_window": sum(m <= ROUND_WINDOW_MINUTES for m in minutes),
    }


def pipeline(deals: list[dict]) -> list[dict[str, Any]]:
    """Shipments per state, in the order of the state machine."""
    return [
        {
            "state": state.value,
            "count": sum(d["state"] == state for d in deals),
            "eur": round(sum(d["all_in_eur"] or 0 for d in deals if d["state"] == state), 2),
        }
        for state in STATE_ORDER
    ]


def accounts(deals: list[dict], legacy: list[dict], data: Dataset, reference: Reference, live: bool) -> list[dict[str, Any]]:
    """One row per customer address that ever wrote to us."""
    mails_by_sender: dict[str, list[dict]] = defaultdict(list)
    for mail in data.mails:
        if mail["sender"] and reference.role_for_email(mail["sender"]) == "customer":
            mails_by_sender[mail["sender"]].append(mail)
    by_account: dict[str, list[dict]] = defaultdict(list)
    for d in deals:
        by_account[d["account"]].append(d)
    legacy_count = Counter(d["account"] for d in legacy)
    total = len(deals)
    rows = []
    for email in sorted(set(by_account) | set(mails_by_sender) | set(legacy_count)):
        own, mails = by_account.get(email, []), mails_by_sender.get(email, [])
        quoted = [d for d in own if d["all_in_eur"] is not None]
        companies = Counter(d["company"] for d in own if d["company"])
        times = [m["received_at"] or m["processed_at"] for m in mails]
        rows.append(
            {
                "email": email,
                "company": companies.most_common(1)[0][0] if companies else _pretty(email),
                "contacts": sorted({d["contact"] for d in own if d["contact"]}),
                "languages": sorted({d["language"] for d in own if d["language"]}),
                "requests": len(own),
                "legacy_requests": legacy_count.get(email, 0),
                "share_pct": _pct(len(own), total),
                "quoted": len(quoted),
                "open": sum(d["state"] in OPEN for d in own),
                **_outcome(own, live),
                "quoted_eur": round(sum(d["all_in_eur"] for d in quoted), 2),
                "avg_all_in_eur": _mean([d["all_in_eur"] for d in quoted]),
                "avg_gross_kg": _mean([d["gross_kg"] for d in own if d["gross_kg"]]),
                "goals": dict(Counter(d["goal"] or "not stated" for d in own)),
                "pickup_requests": sum(bool(d["pickup"]) for d in own),
                "destinations": sorted({d["destination"] for d in own if d["destination"]}),
                "lanes": sorted({d["lane"] for d in own if d["lane"]}),
                "mails_in": len(mails),
                "first_mail_at": min(times) if times else None,
                "last_mail_at": max(times) if times else None,
            }
        )
    return sorted(rows, key=lambda r: (-r["requests"], -r["mails_in"], r["email"]))


def airlines(deals: list[dict], data: Dataset, reference: Reference, tariff_airlines: set[str]) -> list[dict[str, Any]]:
    """Every airline of the carrier list, with how it did wherever it was priced."""
    state = {d["id"]: d for d in deals}
    seen: dict[str, dict[str, Any]] = defaultdict(
        lambda: {"compared": 0, "cheapest_in": 0, "fastest_in": 0, "per_kg": [], "transit": [], "lanes": set(), "source": None, "code": None}
    )
    for shipment_id, options in data.options.items():
        if shipment_id not in state or not options:
            continue
        low, fast = min(o.total_eur for o in options), min(o.transit_days for o in options)
        for o in options:
            row = seen[o.airline]
            row["compared"] += 1
            row["cheapest_in"] += o.total_eur == low
            row["fastest_in"] += o.transit_days == fast
            row["transit"].append(o.transit_days)
            row["lanes"].add(f"{o.origin}-{o.destination}")
            row["source"], row["code"] = o.source, o.airline_code or row["code"]
            if o.billed_kg:
                row["per_kg"].append(o.total_eur / o.billed_kg)
    chosen = Counter(d["airline"] for d in deals if d["airline"])
    accepted = Counter(d["airline"] for d in deals if d["airline"] and d["state"] in ACCEPTED)
    directory = {c.name: c for c in reference.carriers if c.kind == "airline"}
    rows = []
    for name in sorted(set(directory) | set(seen)):
        row, carrier = seen.get(name), directory.get(name)
        rows.append(
            {
                "name": name,
                "code": row["code"] if row else None,
                "email": carrier.email if carrier else None,
                "source": row["source"] if row else "tariff" if name in tariff_airlines else None,
                "serves": sorted(carrier.serves) if carrier else [],
                "compared": row["compared"] if row else 0,
                "cheapest_in": row["cheapest_in"] if row else 0,
                "fastest_in": row["fastest_in"] if row else 0,
                "chosen": chosen.get(name, 0),
                "accepted": accepted.get(name, 0),
                "avg_eur_per_kg": round(statistics.fmean(row["per_kg"]), 3) if row and row["per_kg"] else None,
                "avg_transit_days": _mean(row["transit"]) if row else None,
                "lanes": sorted(row["lanes"]) if row else [],
            }
        )
    return sorted(rows, key=lambda r: (-r["chosen"], -r["compared"], r["name"]))


def truckers(deals: list[dict], data: Dataset, reference: Reference, *, live: bool, received: dict[int, str | None]) -> list[dict[str, Any]]:
    """Every trucker of the carrier list, with answer rate and prices where we asked."""
    state = {d["id"]: d for d in deals}
    seen: dict[str, dict[str, Any]] = defaultdict(
        lambda: {"asked": 0, "prices": [], "per_100kg": [], "hours": [], "cheapest_in": 0, "response": []}
    )
    for shipment_id, requests in data.truckers.items():
        d = state.get(shipment_id)
        if d is None:
            continue
        prices = [r.price_eur for r in requests if r.answered and r.price_eur is not None]
        for r in requests:
            row = seen[r.trucker]
            row["asked"] += 1
            if r.answered and r.price_eur is not None:
                row["prices"].append(r.price_eur)
                row["cheapest_in"] += r.price_eur == min(prices)
                if d["gross_kg"]:
                    row["per_100kg"].append(100 * r.price_eur / d["gross_kg"])
                if r.pickup_hours is not None:
                    row["hours"].append(r.pickup_hours)
                # a dry run replays answers that were given to the old process's requests
                took = _minutes(r.sent_at, received.get(r.reply_message_id)) if live else None
                if took is not None:
                    row["response"].append(took * 60)
    chosen = Counter(d["trucker"] for d in deals if d["trucker"])
    directory = {c.email: c for c in reference.carriers if c.kind == "trucker"}
    rows = []
    for email in sorted(set(directory) | set(seen)):
        row, carrier = seen.get(email), directory.get(email)
        zones = sorted(carrier.serves) if carrier else []
        rows.append(
            {
                "email": email,
                "name": carrier.name if carrier else _pretty(email),
                "zones": zones,
                "airports": sorted({a for a in (reference.airport_for_zone(z) for z in zones) if a}),
                "asked": row["asked"] if row else 0,
                "answered": len(row["prices"]) if row else 0,
                "answer_rate_pct": _pct(len(row["prices"]), row["asked"]) if row else None,
                "cheapest_in": row["cheapest_in"] if row else 0,
                "chosen": chosen.get(email, 0),
                "avg_price_eur": _mean(row["prices"]) if row else None,
                "avg_eur_per_100kg": _mean(row["per_100kg"]) if row else None,
                "median_pickup_hours": _median(row["hours"]) if row else None,
                "median_response_s": _median(row["response"]) if row else None,
            }
        )
    return sorted(rows, key=lambda r: (-r["asked"], -r["chosen"], r["name"]))


def lanes(deals: list[dict], data: Dataset, today: date, live: bool) -> list[dict[str, Any]]:
    """Demand and prices per origin-destination pair. Requests without a known origin are
    listed under their destination alone."""
    grouped: dict[tuple[str | None, str], list[dict]] = defaultdict(list)
    for d in deals:
        if d["destination"]:
            grouped[(d["origin"], d["destination"])].append(d)
    rows = []
    for (origin, destination), own in grouped.items():
        options = [o for d in own for o in data.options.get(d["id"], [])]
        per_kg = [(o.total_eur / o.billed_kg, o.airline) for o in options if o.billed_kg]
        quoted = [d for d in own if d["all_in_eur"] is not None]
        rows.append(
            {
                "origin": origin,
                "destination": destination,
                "lane": f"{origin or '?'}-{destination}",
                "distance_km": distance_km(origin, destination),
                "requests": len(own),
                "quoted": len(quoted),
                "open": sum(d["state"] in OPEN for d in own),
                **_outcome(own, live),
                "avg_all_in_eur": _mean([d["all_in_eur"] for d in quoted]),
                "avg_air_eur_per_kg": _mean([d["air_eur_per_kg"] for d in quoted if d["air_eur_per_kg"]]),
                "airlines_priced": len({o.airline for o in options}),
                "best_airline": min(per_kg)[1] if per_kg else None,
                "best_eur_per_kg": round(min(per_kg)[0], 3) if per_kg else None,
                "fastest_days": min((o.transit_days for o in options), default=None),
                "accounts": sorted({d["company"] for d in own}),
                "shipments": [d["id"] for d in own],
                # accepted shipments between planned departure and planned arrival
                "in_transit": [
                    d["id"]
                    for d in own
                    if d["state"] in ACCEPTED
                    and d["estimated_departure"]
                    and d["estimated_arrival"]
                    and d["estimated_departure"] <= today.isoformat() <= d["estimated_arrival"]
                ],
            }
        )
    return sorted(rows, key=lambda r: (-r["requests"], r["lane"]))


def attention(deals: list[dict], data: Dataset, settings: Settings, *, live: bool, now: datetime) -> list[dict[str, Any]]:
    """Open actions, most urgent first. `kind` names the rule; time-based rules need live data."""
    items: list[dict[str, Any]] = []

    def add(level: str, kind: str, d: dict | None, title: str, detail: str, **extra: Any) -> None:
        items.append(
            {
                "level": level,
                "kind": kind,
                "shipment_id": d["id"] if d else None,
                "company": d["company"] if d else None,
                "lane": d["lane"] if d else None,
                "title": title,
                "detail": detail,
                **extra,
            }
        )

    for d in deals:
        state = d["state"]
        idle = _minutes(d["updated_at"], now.isoformat()) if live else None
        if state == S.FAILED:
            add("critical", "failed", d, "Processing failed", d["error"] or "No error text stored.")
        elif state == S.BOOKING:
            add("warning", "booking_open", d, "Booking in progress",
                f"Attempt {d['booking_attempts']} of {settings.max_booking_attempts} with {d['airline']}.")  # fmt: skip
        elif state == S.AWAITING_INFO:
            add("warning", "awaiting_information", d, "Awaiting customer information",
                f"Clarification {d['clarifications']} of {settings.max_clarifications} sent. Missing: "
                + ("; ".join(d["missing"]) or "not determined") + ".")  # fmt: skip
        elif state in (S.NEW, S.PRICING) and idle is not None and idle > STUCK_AFTER_MINUTES:
            add("warning", "pricing_delayed", d, "Pricing delayed",
                f"No change for {idle:.0f} minutes. Pricing normally completes within {settings.trucker_wait_seconds:.0f} s.")  # fmt: skip
        elif state == S.QUOTED:
            expires = _parse_time(d["valid_until"])
            if d["truck_mode"] == "estimate":
                add("warning", "estimated_trucking", d, "Quote contains estimated trucking cost",
                    f"No trucker offer was received in time. {_eur(d['truck_eur'])} of {_eur(d['all_in_eur'])} is an estimate.")  # fmt: skip
            elif live and expires and expires < now:
                add("warning", "quote_expired", d, "Quote expired without response", f"Valid until {d['valid_until']}.")
            else:
                add("info", "awaiting_decision", d, "Awaiting customer decision",
                    f"{_eur(d['all_in_eur'])} with {d['airline']}, valid until {d['valid_until']}.")  # fmt: skip
        elif state == S.BOOKED and not d["dry_run_booking"]:
            add("info", "confirmation_pending", d, "Airline confirmation pending", f"Booking reference {d['booking_ref']}.")
    for failure in data.failures:
        add("warning", "mail_failed", None, f"Mail {failure['message_id']} could not be processed",
            f"{failure['attempts']} of {settings.max_message_attempts} attempts. {failure['last_error'] or ''}".strip(),
            mail_id=failure["message_id"])  # fmt: skip
    order = {"critical": 0, "warning": 1, "info": 2}
    return sorted(items, key=lambda item: order[item["level"]])


# --- findings --------------------------------------------------------------------------------


def findings(deals: list[dict], settings: Settings, *, live: bool) -> list[dict[str, Any]]:
    """Pricing and demand metrics, each with the number of cases behind it.

    A metric without cases is left out. `value` and `unit` are the figure, `title` its name,
    `text` one sentence on how to read it.
    """
    out: list[dict[str, Any]] = []

    def add(key: str, title: str, value: float | None, unit: str, text: str, n: int) -> None:
        if n and value is not None:
            out.append({"key": key, "title": title, "value": value, "unit": unit, "text": text, "n": n})

    quoted = [d for d in deals if d["all_in_eur"] is not None]
    compared = [d for d in quoted if d["air"] and d["air"]["n"] > 1]

    with_pickup = [d for d in quoted if d["truck_mode"] in ("quoted", "estimate")]
    estimated = [d for d in with_pickup if d["truck_mode"] == "estimate"]
    if with_pickup:
        add("truck_estimates", "Quotes with estimated trucking", len(estimated), "count",
            f"{len(estimated)} of {_n(len(with_pickup), 'quote')} with pickup were issued without a trucker offer "
            f"(none received within {settings.trucker_wait_seconds:.0f} s). Estimated amount: "
            f"{_eur(sum(d['truck_eur'] for d in estimated))}.", len(with_pickup))  # fmt: skip

    spreads = [d["air"]["spread_pct"] for d in compared]
    if spreads:
        widest = max(compared, key=lambda d: d["air"]["spread_pct"])
        add("air_spread", "Air rate spread", _median(spreads), "pct",
            f"Median difference between the lowest and the highest air freight price per shipment. "
            f"Maximum: {widest['air']['spread_pct']:.0f}% on {widest['lane']} (shipment {widest['id']}).", len(compared))  # fmt: skip

    speed = [d for d in compared if d["air"]["speed_days"] > 0]
    if speed:
        days = _median([d["air"]["speed_days"] for d in speed])
        extra = _median([d["air"]["speed_eur"] for d in speed])
        share = _median([100 * d["air"]["speed_eur"] / d["air"]["cheapest_eur"] for d in speed])
        add("speed_cost", "Cost of faster transit", extra, "eur",
            f"Median surcharge of the fastest air option over the lowest-priced one (+{share:.0f}%), for a median "
            f"{days:g} {'day' if days == 1 else 'days'} less transit. Applies to {len(speed)} of {_n(len(compared), 'shipment')}.",
            len(speed))  # fmt: skip

    fast_goal = [d for d in compared if d["goal"] == "fastest" and d["air"].get("premium_pct") is not None]
    if fast_goal:
        premium = _median([d["air"]["premium_pct"] for d in fast_goal])
        add("fastest_premium", "Premium on fastest-priority requests", premium, "pct",
            "Median amount by which the quoted flight exceeds the lowest-priced option where the customer "
            "requested the fastest transit.", len(fast_goal))  # fmt: skip

    trucked = [d for d in quoted if d["truck_eur"]]
    if trucked:
        share = _median([100 * d["truck_eur"] / d["all_in_eur"] for d in trucked])
        add("truck_share", "Trucking share of all-in price", share, "pct",
            f"Median share of the pickup in the all-in price (median amount "
            f"{_eur(_median([d['truck_eur'] for d in trucked]))}).", len(trucked))  # fmt: skip

    haggled = [d for d in quoted if d["truck"] and d["truck"].get("spread_pct") is not None]
    if haggled:
        gap = _median([d["truck"]["spread_pct"] for d in haggled])
        add("trucker_spread", "Trucking offer spread", gap, "pct",
            "Median difference between the lowest and the highest trucker offer for the same pickup.", len(haggled))

    sized = [d for d in deals if d["chargeable_kg"]]
    if sized:
        bulky = [d for d in sized if d["volumetric"]]
        add("volumetric", "Volumetric shipments", len(bulky), "count",
            f"{len(bulky)} of {_n(len(sized), 'shipment')} are billed on volume weight (1 cbm = 167 kg) because it "
            f"exceeds the gross weight.", len(sized))  # fmt: skip

    if quoted:
        breaks = [d for d in quoted if d["weight_break"]]
        add("weight_breaks", "Weight-break optimisation applied", len(breaks), "count",
            f"{len(breaks)} of {_n(len(quoted), 'quote')} are billed at the next higher weight break because the "
            f"total is lower than at the chargeable weight.", len(quoted))  # fmt: skip
        tariff = [d for d in quoted if d["air_source"] == "tariff"]
        add("tariff_quotes", "Quotes on tariff airlines", len(tariff), "count",
            f"{len(tariff)} of {_n(len(quoted), 'quote')} use an airline priced from its tariff file; "
            f"{len(quoted) - len(tariff)} use a portal rate.", len(quoted))  # fmt: skip

    if deals:
        asked = [d for d in deals if d["clarifications"] or d["state"] == S.AWAITING_INFO]
        add("clarifications", "Requests requiring clarification", len(asked), "count",
            f"{len(asked)} of {_n(len(deals), 'request')} could not be priced without further information from the "
            f"customer.", len(deals))  # fmt: skip
        pickups = [d for d in deals if d["pickup"]]
        add("pickup_share", "Requests with pickup", _pct(len(pickups), len(deals)), "pct",
            f"{len(pickups)} of {_n(len(deals), 'request')} include collection at the customer's site; the remainder "
            f"is delivered to the airport by the customer.", len(deals))  # fmt: skip

    if live:
        decided = _outcome(deals, live)
        n = decided["accepted"] + decided["rejected"]
        add("win_rate", "Acceptance rate", decided["win_rate_pct"], "pct",
            f"{decided['accepted']} of {_n(n, 'decided quote')} accepted.", n)
        minutes = [d["minutes_to_quote"] for d in deals if d["minutes_to_quote"] is not None]
        if minutes:
            on_time = sum(m <= ROUND_WINDOW_MINUTES for m in minutes)
            add("time_to_quote", "Time to quote", _median(minutes), "min",
                f"Median from receipt of the request to the quote. {on_time} of {_n(len(minutes), 'quote')} issued "
                f"within the {ROUND_WINDOW_MINUTES:.0f}-minute round window.", len(minutes))  # fmt: skip
    return out


# --- mail ------------------------------------------------------------------------------------


def mail_stats(data: Dataset, reference: Reference, labels: list[dict] | None = None) -> dict[str, Any]:
    roles = Counter(reference.role_for_email(m["sender"]) for m in data.mails)
    times = sorted(t for t in (_parse_time(m["received_at"]) for m in data.mails) if t)
    buckets: list[dict[str, Any]] = []
    if times:
        start = times[0].replace(minute=times[0].minute - times[0].minute % 10, second=0, microsecond=0)
        counts = Counter(int((t - start).total_seconds() // 600) for t in times)
        buckets = [
            {"at": (start + timedelta(minutes=10 * i)).isoformat(), "count": counts.get(i, 0)}
            for i in range(max(counts) + 1)
        ]
    stats: dict[str, Any] = {
        "total": len(data.mails),
        "by_category": [{"category": c, "count": n} for c, n in Counter(m["category"] for m in data.mails).most_common()],
        "by_role": [{"role": r, "count": n} for r, n in roles.most_common()],
        "low_confidence": sum(m["confidence"] < LOW_CONFIDENCE for m in data.mails),
        "per_10_minutes": buckets[-72:],  # the last twelve hours of mail
        "first_at": times[0].isoformat() if times else None,
        "last_at": times[-1].isoformat() if times else None,
    }
    labelled = [row for row in labels or [] if row.get("expected")]
    if labelled:
        wrong = [row for row in labelled if row["category"] != row["expected"]]
        stats["classification"] = {"labelled": len(labelled), "correct": len(labelled) - len(wrong), "wrong": wrong}
    return stats


def outbound_stats(data: Dataset) -> dict[str, Any]:
    mails = [m for group in data.outbound.values() for m in group]
    return {
        "total": len(mails),
        "by_kind": [{"kind": k, "count": n} for k, n in Counter(m.kind for m in mails).most_common()],
        "templates_used": sum(m.used_template for m in mails),
        "sent": sum(m.sent_message_id is not None for m in mails),
    }


# --- the snapshot ----------------------------------------------------------------------------


def limits(source: str) -> list[str]:
    """What the current data does not support."""
    if source == "live":
        return []
    return [
        "Timings are not reported: the replay clock and the recorded mail times differ.",
        "Acceptance rates are not reported: the recorded customer decisions refer to the previous process's quotes.",
        "Bookings are simulated: nothing was sent or booked.",
    ]


def snapshot(
    store: MessageStore,
    reference: Reference,
    settings: Settings,
    *,
    source: str,
    tariff_airlines: set[str] | None = None,
    labels: list[dict] | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Everything the CRM lists and charts, in one read of the database."""
    live, now = source == "live", now or datetime.now(UTC)
    data = Dataset.load(store)
    received = {m["id"]: m["received_at"] or m["processed_at"] for m in data.mails}
    rows = [deal(s, data, live=live, received=received) for s in data.shipments]
    deals, legacy = [d for d in rows if not d["legacy"]], [d for d in rows if d["legacy"]]
    used = {code for d in deals for code in (d["origin"], d["destination"]) if code}
    zone_map = reference.zones  # zone -> (area, airport)
    origins = {airport for _, airport in zone_map.values()}
    zones = [
        {
            "zone": zone,
            "area": area,
            "airport": airport,
            "truckers": sorted(c.name for c in reference.truckers_for_zone(zone)),
            "requests": sum(d["zone"] == zone for d in deals),
        }
        for zone, (area, airport) in sorted(zone_map.items())
    ]
    return {
        "source": source,
        "generated_at": now.isoformat(),
        "limits": limits(source),
        "kpis": kpis(deals, live),
        "pipeline": pipeline(deals),
        "deals": deals,
        "legacy": [{k: d[k] for k in ("id", "account", "company", "subject", "received_at")} for d in legacy],
        "accounts": accounts(deals, legacy, data, reference, live),
        "airlines": airlines(deals, data, reference, tariff_airlines or set()),
        "truckers": truckers(deals, data, reference, live=live, received=received),
        "lanes": lanes(deals, data, now.date(), live),
        "zones": zones,
        "airports": {
            code: {"city": city, "lat": lat, "lon": lon, "origin": code in origins, "used": code in used}
            for code, (city, lat, lon) in AIRPORTS.items()
        },
        "attention": attention(deals, data, settings, live=live, now=now),
        "findings": findings(deals, settings, live=live),
        "mail": mail_stats(data, reference, labels),
        "outbound": outbound_stats(data),
    }


def shipment_detail(store: MessageStore, reference: Reference, shipment_id: int, *, source: str) -> dict[str, Any] | None:
    """One shipment with its compared flights, trucker offers and the whole conversation."""
    shipment = store.get_shipment(shipment_id)
    if shipment is None:
        return None
    live = source == "live"
    data = Dataset.load(store)
    received = {m["id"]: m["received_at"] or m["processed_at"] for m in data.mails}
    row = deal(shipment, data, live=live, received=received)
    options, requests = data.options.get(shipment_id, []), data.truckers.get(shipment_id, [])
    chosen = shipment.chosen_option
    low = min((o.total_eur for o in options), default=None)
    fast = min((o.transit_days for o in options), default=None)
    prices = [r.price_eur for r in requests if r.answered and r.price_eur is not None]
    inbound, outbound = store.shipment_inbound(shipment_id), data.outbound.get(shipment_id, [])

    # Live: every timestamp is wall time. A dry run mixes the recorded mail times with the
    # clock of the replay, so there each of our mails is placed after the mail it answers,
    # after our previous mail and, for the quote, after the last trucker answer.
    in_time = {m.message_id: m.received_at or m.processed_at for m in inbound}
    sent_at = {r.out_message_id: r.sent_at for r in requests}
    last_answer = max((in_time.get(r.reply_message_id) or "" for r in requests), default="")
    timeline: list[tuple[str, dict[str, Any]]] = []
    for m in inbound:
        role = reference.role_for_email(m.sender)
        carrier = reference.carrier_for_email(m.sender)
        timeline.append(
            (
                in_time[m.message_id] or "",
                {
                    "direction": "in", "id": m.message_id, "role": role,
                    "who": carrier.name if carrier else row["company"] if m.sender == shipment.customer else _pretty(m.sender),
                    "address": m.sender, "category": m.decision.category.value,
                    "confidence": m.decision.confidence, "at": m.received_at or m.processed_at,
                    "subject": m.subject, "body": m.body,
                },
            )
        )  # fmt: skip
    previous = ""
    for o in outbound:
        if live:
            key = o.created_at
        else:
            own_id = o.sent_message_id if o.sent_message_id is not None else -o.id
            key = sent_at.get(own_id) or max(previous, in_time.get(o.in_reply_to) or "", last_answer if o.kind == "quote" else "") + "~"
            previous = key
        carrier = reference.carrier_for_email(o.to)
        timeline.append(
            (
                key,
                {
                    "direction": "out", "id": o.id, "kind": o.kind,
                    "who": carrier.name if carrier else row["company"] if o.to == shipment.customer else _pretty(o.to),
                    "address": o.to, "at": o.created_at if live else None,
                    "sent": o.sent_message_id is not None, "used_template": o.used_template,
                    "subject": o.subject, "body": o.body,
                },
            )
        )  # fmt: skip
    return {
        "source": source,
        "deal": row,
        "options": [
            {
                "airline": o.airline, "code": o.airline_code, "source": o.source,
                "total_eur": o.total_eur, "transit_days": o.transit_days, "billed_kg": o.billed_kg,
                "eur_per_kg": round(o.total_eur / o.billed_kg, 3) if o.billed_kg else None,
                "chosen": bool(chosen and o.airline == chosen.airline and o.total_eur == chosen.total_eur),
                "cheapest": o.total_eur == low, "fastest": o.transit_days == fast,
            }
            for o in sorted(options, key=lambda o: o.total_eur)
        ],  # fmt: skip
        "truckers": [
            {
                "email": r.trucker,
                "name": (c.name if (c := reference.carrier_for_email(r.trucker)) else _pretty(r.trucker)),
                "answered": r.answered, "price_eur": r.price_eur, "pickup_hours": r.pickup_hours,
                "ref": r.ref, "valid_until": r.valid_until,
                "chosen": r.trucker == row["trucker"],
                "cheapest": bool(prices) and r.price_eur == min(prices),
                "response_s": round(took * 60) if live and (took := _minutes(r.sent_at, in_time.get(r.reply_message_id))) is not None else None,
            }
            for r in sorted(requests, key=lambda r: (r.price_eur is None, r.price_eur or 0))
        ],  # fmt: skip
        "timeline": [item for _, item in sorted(timeline, key=lambda pair: pair[0])],
    }


def mails(
    store: MessageStore,
    reference: Reference,
    *,
    query: str = "",
    category: str = "",
    role: str = "",
    sender: str = "",
    offset: int = 0,
    limit: int = 100,
) -> dict[str, Any]:
    """Stored mails with their text, newest first, filtered and paged."""
    data = Dataset.load(store)
    linked = data.mail_shipments()
    needle = query.strip().lower()
    rows = []
    for mail in store.mail_index(with_body=True):
        mail_role = reference.role_for_email(mail["sender"])
        if category and mail["category"] != category:
            continue
        if role and mail_role != role:
            continue
        if sender and mail["sender"] != sender:
            continue
        if needle and needle not in f"{mail['id']} {mail['sender']} {mail['subject']} {mail['body']}".lower():
            continue
        carrier = reference.carrier_for_email(mail["sender"])
        rows.append(
            {
                **{k: mail[k] for k in ("id", "sender", "subject", "category", "confidence", "reasoning", "body", "trace_id")},
                "who": carrier.name if carrier else _pretty(mail["sender"]),
                "role": mail_role,
                "at": mail["received_at"] or mail["processed_at"],
                "shipment_id": linked.get(mail["id"]),
            }
        )
    return {"total": len(rows), "offset": offset, "items": rows[offset : offset + limit]}
