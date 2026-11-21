"""Pricing rules. Pure functions: no I/O, no clock, no LLM (docs/data.md, "Pricing formula")."""

import math
from datetime import date, timedelta
from statistics import median

from app.schemas import AirOption, Goal, TariffLane, TruckerRequest

VOLUME_KG_PER_CBM = 167
MAX_DAYS_AHEAD = 90  # ponytail: a ready date further out is most likely a typo


def chargeable_kg(gross_kg: float, volume_cbm: float) -> float:
    """The higher of actual weight and volume weight (1 cbm = 167 kg)."""
    return round(max(gross_kg, volume_cbm * VOLUME_KG_PER_CBM), 2)


def tariff_total(lane: TariffLane, chargeable: float) -> tuple[float, float]:
    """Cheapest way to bill `chargeable` kg on a tariff lane: `(total_eur, billed_kg)`.

    Anti-weight-break: billing a higher break weight at its lower rate can be cheaper than
    billing the real weight. The minimum applies to the freight only; fuel and security are
    charged per billed kg on top.
    """
    own_bracket = max(i for i, start in enumerate(lane.breaks_kg) if start <= chargeable)
    candidates = []
    for i in range(own_bracket, len(lane.breaks_kg)):
        billed = max(chargeable, lane.breaks_kg[i])
        freight = max(lane.minimum_eur, billed * lane.rates_per_kg[i])
        surcharges = billed * (lane.fuel_per_kg + lane.security_per_kg)
        candidates.append((round(freight + surcharges, 2), billed))
    return min(candidates)


def tariff_option(lane: TariffLane, gross_kg: float, volume_cbm: float) -> AirOption:
    """Price a shipment on a local tariff lane."""
    total, billed = tariff_total(lane, chargeable_kg(gross_kg, volume_cbm))
    return AirOption(
        source="tariff",
        airline=lane.airline,
        origin=lane.origin,
        destination=lane.destination,
        billed_kg=billed,
        total_eur=total,
        transit_days=lane.transit_days,
        valid_until=lane.valid_to.isoformat(),
        weight_kg=gross_kg,
        volume_cbm=volume_cbm,
    )


_SOURCE_RANK = {"portal": 0, "email": 1, "tariff": 2}


def by_source(options: list[AirOption]) -> list[AirOption]:
    """One price source per airline: portal, else its email reply, else the local tariff
    (ADR 51). Matters once an update mail gives a portal or email airline local lanes."""
    best = {o.airline: min(_SOURCE_RANK[p.source] for p in options if p.airline == o.airline) for o in options}
    return [o for o in options if _SOURCE_RANK[o.source] == best[o.airline]]


def pick_best(
    air: list[AirOption],
    trucks: list[TruckerRequest],
    goal: Goal | None,
    ready_date: date | None = None,
    deliver_by: date | None = None,
) -> tuple[AirOption, TruckerRequest | None]:
    """Choose the air option and the trucker for a goal (ADR 08). No goal means cheapest.

    Air - fastest: fewest transit days, price breaks ties; cheapest: lowest air total.
    Trucker (ADR 52), counted in days like `eta`: always the cheapest one, unless the dates
    need a faster one. Fastest goal: the cheapest of those that reach the earliest flight.
    deliver_by (ADR 44): per air option the cheapest trucker that still arrives by then; only
    options with such a trucker count. If none does, the fastest air with the fastest trucker.
    `trucks` may be empty (customer delivers, or no trucker answered).
    """
    priced = [t for t in trucks if t.price_eur is not None]
    days = lambda t: pickup_days(t.pickup_hours)  # noqa: E731
    cheapest = lambda ts: min(ts, key=lambda t: (t.price_eur, days(t)), default=None)  # noqa: E731
    if goal == "fastest" and priced:
        first = min(map(days, priced))
        priced = [t for t in priced if days(t) == first]
    if deliver_by and ready_date:
        on_time = lambda o: [  # noqa: E731
            t for t in priced if eta(ready_date, t.pickup_hours, o.transit_days)["arrival"] <= deliver_by
        ]
        fits = [o for o in air if on_time(o) or (not priced and eta(ready_date, None, o.transit_days)["arrival"] <= deliver_by)]
        if not fits:
            fastest = min(air, key=lambda o: (o.transit_days, o.total_eur))
            return fastest, min(priced, key=lambda t: (days(t), t.price_eur), default=None)
        choice = _by_goal(fits, goal)
        return choice, cheapest(on_time(choice))
    return _by_goal(air, goal), cheapest(priced)


def _by_goal(air: list[AirOption], goal: Goal | None) -> AirOption:
    if goal == "fastest":
        return min(air, key=lambda o: (o.transit_days, o.total_eur))
    return min(air, key=lambda o: (o.total_eur, o.transit_days))


def pickup_days(pickup_hours: float | None) -> int:
    """Days the trucker adds before the flight: a pickup of up to 24 h flies the next day."""
    return math.ceil(pickup_hours / 24) if pickup_hours else 0


_NOISE = {"air", "airlines", "airline", "cargo", "freight", "airways", "logistik", "logistics",
          "gmbh", "spedition", "transport", "ag", "kg", "co", "the"}


def _words(name: str) -> set[str]:
    return {w for w in "".join(c if c.isalnum() else " " for c in name.lower()).split() if w not in _NOISE}


def carrier_in(name: str, wanted: list[str | None]) -> bool:
    """Is `name` one of the carriers the customer named? ("Solace" matches "Solace Air Cargo")."""
    words = _words(name)
    return any(w and (ws := _words(w)) and ws <= words for w in wanted)


def date_problems(ready: date | None, deliver_by: date | None, today: date) -> list[str]:
    """Dates that cannot be right, as questions for the customer (ADR 44)."""
    problems = []
    if ready and ready < today:
        problems.append(f"the ready date: {ready.isoformat()} is in the past - which date did you mean?")
    if ready and deliver_by and deliver_by < ready:
        problems.append(
            f"the dates: the delivery date {deliver_by.isoformat()} is before the ready date "
            f"{ready.isoformat()} - are they swapped (ready {deliver_by.isoformat()}, delivery by "
            f"{ready.isoformat()})?"
        )
    if ready and (ready - today).days > MAX_DAYS_AHEAD:
        problems.append(f"the ready date: {ready.isoformat()} is more than {MAX_DAYS_AHEAD} days ahead - is it correct?")
    return problems


def all_in(air_total_eur: float, truck_eur: float, margin_eur: float) -> float:
    """The one price the customer sees."""
    return round(air_total_eur + truck_eur + margin_eur, 2)


def eta(ready_date: date, pickup_hours: float | None, transit_days: int) -> dict[str, date]:
    """Estimated dates: collected on the ready date, flown once the pickup has arrived."""
    departure = ready_date + timedelta(days=pickup_days(pickup_hours))
    return {
        "pickup": ready_date,
        "departure": departure,
        "arrival": departure + timedelta(days=transit_days),
    }


def fallback_truck(
    gross_kg: float,
    zone: str | None,
    history: list[tuple[str | None, float, float]],
    default_eur_per_kg: float,
) -> float:
    """Trucker price to quote when no trucker answered in time (ADR 10).

    `history` holds earlier trucker quotes as `(zone, price_eur, gross_kg)`. The median EUR/kg
    of the same zone is used, then of all zones, then `default_eur_per_kg`.
    """
    per_kg = [(z, price / kg) for z, price, kg in history if kg > 0]
    same_zone = [rate for z, rate in per_kg if z == zone]
    rates = same_zone or [rate for _, rate in per_kg]
    return round((median(rates) if rates else default_eur_per_kg) * gross_kg, 2)
