"""Pricing rules. Pure functions: no I/O, no clock, no LLM (docs/data.md, "Pricing formula")."""

import math
from datetime import date, timedelta
from statistics import median

from app.schemas import AirOption, Goal, TariffLane, TruckerRequest

VOLUME_KG_PER_CBM = 167


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


def pick_best(
    air: list[AirOption], trucks: list[TruckerRequest], goal: Goal | None
) -> tuple[AirOption, TruckerRequest | None]:
    """Choose the air option and the trucker for a goal (ADR 08). No goal means cheapest.

    fastest:  fewest transit days, then shortest pickup time, price breaks ties.
    cheapest: lowest air total plus lowest trucker price.
    `trucks` may be empty (customer delivers, or no trucker answered).
    """
    priced = [t for t in trucks if t.price_eur is not None]
    if goal == "fastest":
        best_air = min(air, key=lambda o: (o.transit_days, o.total_eur))
        hours = lambda t: math.inf if t.pickup_hours is None else t.pickup_hours  # noqa: E731
        best_truck = min(priced, key=lambda t: (hours(t), t.price_eur), default=None)
    else:
        best_air = min(air, key=lambda o: (o.total_eur, o.transit_days))
        best_truck = min(priced, key=lambda t: t.price_eur, default=None)
    return best_air, best_truck


def all_in(air_total_eur: float, truck_eur: float, margin_eur: float) -> float:
    """The one price the customer sees."""
    return round(air_total_eur + truck_eur + margin_eur, 2)


def eta(ready_date: date, pickup_hours: float | None, transit_days: int) -> dict[str, date]:
    """Estimated dates: collected on the ready date, flown once the pickup has arrived."""
    pickup_days = math.ceil(pickup_hours / 24) if pickup_hours else 0
    departure = ready_date + timedelta(days=pickup_days)
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
