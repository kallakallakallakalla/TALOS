from datetime import date

import pytest

from app import pricing
from app.schemas import AirOption, TariffLane, TruckerRequest
from app.tariffs import Tariffs

LANE = TariffLane(
    airline="Test Air",
    origin="CGN",
    destination="ORD",
    transit_days=3,
    minimum_eur=100.0,
    breaks_kg=[0, 45, 100, 500, 1000],
    rates_per_kg=[4.0, 3.0, 2.5, 2.0, 1.5],
    fuel_per_kg=0.5,
    security_per_kg=0.1,
    valid_from=date(2026, 11, 1),
    valid_to=date(2026, 11, 30),
)


def _air(airline: str, total: float, days: int) -> AirOption:
    return AirOption(
        source="portal", airline=airline, origin="CGN", destination="ORD",
        billed_kg=700, total_eur=total, transit_days=days,
    )  # fmt: skip


def _truck(name: str, price: float | None, hours: float | None) -> TruckerRequest:
    return TruckerRequest(
        out_message_id=hash(name), shipment_id=1, trucker=name, sent_at="",
        answered=True, price_eur=price, pickup_hours=hours,
    )  # fmt: skip


def test_chargeable_weight_is_the_higher_of_actual_and_volume() -> None:
    assert pricing.chargeable_kg(679, 4.19) == 699.73  # volume wins (round 4)
    assert pricing.chargeable_kg(1540, 5.9) == 1540  # actual wins (round 3)


def test_minimum_applies_to_freight_only_and_surcharges_come_on_top() -> None:
    # 10 kg: freight 10 x 4.0 = 40 -> minimum 100; surcharges 10 x 0.6 = 6
    assert pricing.tariff_total(LANE, 10) == (106.0, 10)
    # 200 kg: 200 x 2.5 + 200 x 0.6
    assert pricing.tariff_total(LANE, 200) == (620.0, 200)


def test_anti_weight_break_bills_966_kg_as_1000() -> None:
    # at 966.93 kg: 966.93 x (2.0 + 0.6) = 2514.02; as 1000 kg: 1000 x (1.5 + 0.6) = 2100
    assert pricing.tariff_total(LANE, 966.93) == (2100.0, 1000)
    # far below the break the real weight is cheaper
    assert pricing.tariff_total(LANE, 600)[1] == 600


def test_pick_best_cheapest_fastest_and_tie_breaks() -> None:
    air = [_air("Slow Cheap", 1000, 5), _air("Fast", 1500, 2), _air("Fast Cheaper", 1400, 2)]
    trucks = [_truck("Cheap", 100, 29), _truck("Quick", 200, 11), _truck("Quick Dear", 250, 11),
              _truck("Declined", None, None)]  # fmt: skip

    best_air, best_truck = pricing.pick_best(air, trucks, "cheapest")
    assert (best_air.airline, best_truck.trucker) == ("Slow Cheap", "Cheap")
    best_air, best_truck = pricing.pick_best(air, trucks, "fastest")
    assert (best_air.airline, best_truck.trucker) == ("Fast Cheaper", "Quick")
    # no goal stated -> cheapest (ADR 08); no trucker -> None
    assert pricing.pick_best(air, [], None)[0].airline == "Slow Cheap"
    assert pricing.pick_best(air, [], None)[1] is None


def test_all_in_adds_trucker_and_margin() -> None:
    assert pricing.all_in(1483.43, 115.98, 0.0) == 1599.41  # round 4


def test_eta_adds_pickup_days_then_transit() -> None:
    dates = pricing.eta(date(2026, 11, 28), pickup_hours=29, transit_days=3)
    assert dates == {
        "pickup": date(2026, 11, 28), "departure": date(2026, 11, 30), "arrival": date(2026, 12, 3)
    }  # fmt: skip
    assert pricing.eta(date(2026, 11, 27), None, 3)["arrival"] == date(2026, 11, 30)


def test_fallback_truck_prefers_same_zone_then_all_then_default() -> None:
    history = [("51", 100.0, 500.0), ("51", 150.0, 500.0), ("51", 400.0, 500.0), ("61", 50.0, 500.0)]
    assert pricing.fallback_truck(1000, "51", history, 0.28) == 300.0  # median 0.30 of zone 51
    assert pricing.fallback_truck(1000, "20", history, 0.28) == 250.0  # median of all zones
    assert pricing.fallback_truck(679, "51", [], 0.28) == pytest.approx(190.12)


def test_tariff_db_has_ten_airlines_and_filters_expired_lanes() -> None:
    tariffs = Tariffs("../data/tariffs.json")
    assert len(tariffs.airlines) == 10
    # spot checks against the source files (Yellowtail txt, Danube Wings scan)
    (lane,) = [l for l in tariffs.lanes("FRA", "ORD", date(2026, 11, 21)) if l.airline.startswith("Yellowtail")]
    assert (lane.transit_days, lane.minimum_eur, lane.rates_per_kg[0], lane.rates_per_kg[-1]) == (4, 110.0, 3.73, 1.14)
    (lane,) = [l for l in tariffs.lanes("STR", "SGN", date(2026, 11, 21)) if l.airline.startswith("Danube")]
    assert (lane.rates_per_kg, lane.fuel_per_kg, lane.security_per_kg) == ([4.63, 4.21, 3.45, 2.93, 2.4, 2.18], 1.09, 0.11)
    assert tariffs.lanes("FRA", "ORD", date(2026, 12, 1)) == []  # tariff expired on 30 Nov


def test_deliver_by_keeps_only_options_in_time_else_the_fastest() -> None:
    from datetime import date

    ready = date(2026, 12, 1)
    slow_cheap = AirOption(source="tariff", airline="Slow", origin="FRA", destination="MIA", billed_kg=100, total_eur=900, transit_days=8)
    mid = AirOption(source="tariff", airline="Mid", origin="FRA", destination="MIA", billed_kg=100, total_eur=1000, transit_days=4)
    fast = AirOption(source="tariff", airline="Fast", origin="FRA", destination="MIA", billed_kg=100, total_eur=1500, transit_days=2)
    air = [slow_cheap, mid, fast]
    assert pricing.pick_best(air, [], "cheapest")[0].airline == "Slow"
    assert pricing.pick_best(air, [], "cheapest", ready, date(2026, 12, 6))[0].airline == "Mid"
    assert pricing.pick_best(air, [], "cheapest", ready, date(2026, 12, 2))[0].airline == "Fast"  # none in time


def test_impossible_dates_become_questions() -> None:
    from datetime import date

    today = date(2026, 11, 21)
    assert pricing.date_problems(date(2026, 11, 27), date(2026, 12, 10), today) == []
    swapped = pricing.date_problems(date(2026, 12, 10), date(2026, 11, 27), today)
    assert len(swapped) == 1 and "swapped" in swapped[0]
    assert "past" in pricing.date_problems(date(2026, 11, 1), None, today)[0]
    assert "90 days" in pricing.date_problems(date(2027, 6, 1), None, today)[0]


def test_carrier_names_match_loosely_but_not_by_accident() -> None:
    assert pricing.carrier_in("Solace Air Cargo", ["Solace"])
    assert pricing.carrier_in("Flinkwagen Logistik", ["flinkwagen logistik GmbH"])
    assert not pricing.carrier_in("Solace Air Cargo", ["Nova Atlas Cargo"])
    assert not pricing.carrier_in("Solace Air Cargo", [None, "Air Cargo"])  # noise words only
