"""Update mails change the reference store (ADR 50): put rules, checks, flags, price source."""

from datetime import date
from pathlib import Path

import pytest

from app import pricing, updates
from app.reference import Reference
from app.schemas import AirOption, DataChange
from app.tariffs import Tariffs

TODAY = date(2026, 11, 21)
COBALT = "cobalt-skylift@hackloop.mail"
RATEDESK = "ratedesk@hackloop.mail"


@pytest.fixture
def store(tmp_path: Path):
    db = str(tmp_path / "ref.db")
    return Reference("../data/start-kit", db), Tariffs("../data/tariffs.json", db)


def apply(store, sender, *changes, flags=None, message_id=1):
    ref, tariffs = store
    return updates.apply_update(
        ref, tariffs, message_id=message_id, sender=sender, subject="update",
        changes=list(changes), flags=flags or {}, today=TODAY, fx_eur_per_usd=None,
    )  # fmt: skip


def lane(store, day, origin="CGN", destination="EZE", airline="Cobalt Skylift"):
    return next(l for l in store[1].lanes(origin, destination, day) if l.airline == airline)


def test_start_kit_seeds_the_store_once(store) -> None:
    ref, tariffs = store
    assert len(ref.carriers) == 51 and len(tariffs.all_lanes()) == 158 and len(ref.zones) == 32
    again = Tariffs("../data/tariffs.json", str(Path(ref.db.execute("PRAGMA database_list").fetchone()[2])))
    assert len(again.all_lanes()) == 158  # not seeded twice


def test_single_field_change_from_today_keeps_the_rest_and_the_old_row(store) -> None:
    out = apply(store, COBALT, DataChange(action="put", target="tariff_lane", carrier="Cobalt", origin="CGN",
                                          destination="EZE", fuel_per_kg=0.75))  # fmt: skip
    assert [(o.field, o.before, o.after, o.status) for o in out] == [("fuel_per_kg", "0.7", "0.75", "applied")]
    now, before = lane(store, TODAY), lane(store, date(2026, 11, 20))
    assert now.fuel_per_kg == 0.75 and now.rates_per_kg == before.rates_per_kg and before.fuel_per_kg == 0.7


def test_december_tariff_starts_on_its_first_day(store) -> None:
    apply(store, COBALT, DataChange(action="put", target="tariff_lane", origin="CGN", destination="EZE",
                                    valid_from=date(2026, 12, 1), valid_to=date(2026, 12, 31),
                                    rates_per_kg=[5.6, 4.3, 3.3, 3.0, 2.6, 2.1, 1.7]))  # fmt: skip
    assert lane(store, date(2026, 11, 30)).rates_per_kg[0] == 5.41
    december = lane(store, date(2026, 12, 2))
    assert december.rates_per_kg[0] == 5.6 and december.fuel_per_kg == 0.7  # surcharges carried over


def test_jump_over_4x_is_held_but_plus_200_percent_is_applied(store) -> None:
    big = apply(store, COBALT, DataChange(action="put", target="tariff_lane", origin="CGN", destination="EZE",
                                          minimum=79 * 5))  # fmt: skip
    assert big[0].status == "held" and big[0].flag == "suspicious"
    ok = apply(store, COBALT, DataChange(action="put", target="tariff_lane", origin="CGN", destination="EZE",
                                         minimum=79 * 3), message_id=2)  # fmt: skip
    assert ok[0].status == "applied" and lane(store, TODAY).minimum_eur == 237


def test_relative_change_is_calculated_in_code_for_every_lane(store) -> None:
    out = apply(store, COBALT, DataChange(action="adjust", target="tariff_lane", percent=5,
                                          valid_from=date(2026, 12, 1)))  # fmt: skip
    cobalt_routes = {(l.origin, l.destination) for l in store[1].all_lanes("Cobalt Skylift")}
    assert len({o.record_key for o in out}) == len(cobalt_routes)
    assert lane(store, date(2026, 12, 1)).rates_per_kg[0] == round(5.41 * 1.05, 2)
    assert lane(store, TODAY).rates_per_kg[0] == 5.41


def test_removal_ends_the_lane_the_day_before(store) -> None:
    apply(store, COBALT, DataChange(action="remove", target="tariff_lane", origin="CGN", destination="EZE",
                                    valid_from=date(2026, 11, 25)))  # fmt: skip
    assert lane(store, date(2026, 11, 24))
    assert not [l for l in store[1].lanes("CGN", "EZE", date(2026, 11, 25)) if l.airline == "Cobalt Skylift"]


def test_carriers_may_only_change_their_own_data_the_rate_desk_anything(store) -> None:
    change = DataChange(action="put", target="tariff_lane", carrier="Kestrel Airways", origin="CGN",
                        destination="JFK", fuel_per_kg=0.8)  # fmt: skip
    assert apply(store, COBALT, change)[0].status == "held"
    assert apply(store, RATEDESK, change, message_id=2)[0].status == "applied"
    zone = DataChange(action="put", target="zone", zone="90", area="Nürnberg", airport="MUC")
    assert apply(store, COBALT, zone, message_id=3)[0].status == "held"
    assert apply(store, RATEDESK, zone, message_id=4)[0].status == "applied"
    assert store[0].airport_for_zone("90") == "MUC"


def test_llm_suspicious_flag_holds_and_unclear_still_applies(store) -> None:
    a = DataChange(action="put", target="tariff_lane", origin="CGN", destination="EZE", fuel_per_kg=0.75)
    b = DataChange(action="put", target="tariff_lane", origin="CGN", destination="BOG", fuel_per_kg=0.75)
    out = apply(store, COBALT, a, b, flags={1: ("suspicious", "one-shipment offer"), 2: ("unclear", "date vague")})
    assert [(o.change_no, o.status, o.flag) for o in out] == [(1, "held", "suspicious"), (2, "applied", "unclear")]
    assert store[0].db.execute("SELECT status FROM data_updates").fetchone()[0] == "partly_applied"


def test_trucker_zones_grow_email_change_keeps_old_address(store) -> None:
    ref = store[0]
    apply(store, "neckarstrom-kurier@hackloop.mail",
          DataChange(action="put", target="carrier", serves_add=["52"], email="dispatch@neckarstrom.example"))
    carrier = ref.carrier_named("Neckarstrom Kurier")
    assert "52" in carrier.serves and "24" in carrier.serves and carrier.email == "dispatch@neckarstrom.example"
    assert ref.role_for_email("neckarstrom-kurier@hackloop.mail") == "trucker"  # late replies still match


def test_a_zone_never_loses_its_last_trucker(store) -> None:
    ref = store[0]
    apply(store, RATEDESK, DataChange(action="put", target="zone", zone="90", area="Nürnberg", airport="MUC"),
          DataChange(action="put", target="carrier", carrier="Grautal Trucking", serves_add=["90"]))
    out = apply(store, "grautal-trucking@hackloop.mail",
                DataChange(action="remove", target="carrier", serves_remove=["90"]), message_id=2)
    assert out[0].status == "held" and "no trucker left" in out[0].reason
    assert [t.name for t in ref.truckers_for_zone("90")] == ["Grautal Trucking"]


def test_prices_in_usd_are_converted_or_held(store) -> None:
    usd = DataChange(action="put", target="tariff_lane", origin="CGN", destination="EZE", fuel_per_kg=1.0, currency="USD")
    assert apply(store, COBALT, usd)[0].status == "held"
    out = apply(store, COBALT, usd.model_copy(update={"eur_per_unit": 0.9}), message_id=2)
    assert out[0].status == "applied" and lane(store, TODAY).fuel_per_kg == 0.9


def test_new_carrier_from_the_rate_desk_and_a_stopped_airline_still_books(store) -> None:
    ref = store[0]
    apply(store, RATEDESK, DataChange(action="put", target="carrier", carrier="Alpenfracht", kind="trucker",
                                      email="alpenfracht@hackloop.mail", serves_add=["82", "83"]))  # fmt: skip
    assert "Alpenfracht" in [t.name for t in ref.truckers_for_zone("82")]
    apply(store, COBALT, DataChange(action="remove", target="carrier"), message_id=2)
    assert "Cobalt Skylift" not in [c.name for c in ref.carriers]
    assert ref.airline_email("Cobalt Skylift") == COBALT


def test_new_lane_copies_the_missing_fields_from_the_airline(store) -> None:
    out = apply(store, COBALT, DataChange(action="put", target="tariff_lane", origin="CGN", destination="LIS",
                                          copy_from_destination="EZE", transit_days=2))  # fmt: skip
    assert out[0].field == "lane" and out[0].status == "applied"
    new = lane(store, TODAY, destination="LIS")
    assert new.rates_per_kg == lane(store, TODAY).rates_per_kg and new.transit_days == 2
    assert "LIS" in store[0].carrier_named("Cobalt Skylift").serves  # served via its lanes


def test_one_price_source_per_airline_portal_then_email_then_tariff() -> None:
    def option(source, airline, total):
        return AirOption(source=source, airline=airline, origin="CGN", destination="ORD",
                         billed_kg=100, total_eur=total, transit_days=3)  # fmt: skip

    kept = pricing.by_source([
        option("tariff", "Aurelia Air Cargo", 100), option("portal", "Aurelia Air Cargo", 300),
        option("tariff", "Zephyr Steppe Cargo", 100), option("email", "Zephyr Steppe Cargo", 200),
        option("tariff", "Cobalt Skylift", 150),
    ])  # fmt: skip
    assert {(o.airline, o.source) for o in kept} == {
        ("Aurelia Air Cargo", "portal"), ("Zephyr Steppe Cargo", "email"), ("Cobalt Skylift", "tariff")
    }


@pytest.mark.anyio
async def test_update_mail_is_applied_confirmed_once_and_used_by_the_next_quote(tmp_path: Path) -> None:
    from app.schemas import ChangeFlag, ClassificationDecision, DataUpdate, DataUpdateFlags, MailboxMessage
    from tests.fakes import ScriptedLLM
    from tests.test_workflow import Harness

    def answer(schema, system, user):
        if schema is ClassificationDecision:
            return ClassificationDecision(category="tariff_update", confidence=0.95, reasoning="new fuel")
        if schema is DataUpdate:
            return DataUpdate(changes=[DataChange(action="put", target="tariff_lane", carrier="Cobalt Skylift",
                                                  origin="CGN", destination="EZE", fuel_per_kg=0.75,
                                                  quote="Fuel CGN-EZE now 0.75 EUR/kg")])  # fmt: skip
        if schema is DataUpdateFlags:
            assert "lane CGN-EZE" in user  # the flag call sees what we store today
            return DataUpdateFlags(flags=[ChangeFlag(change=1, flag="ok")])
        raise AssertionError(schema)

    cobalt = Tariffs("../data/tariffs.json").all_lanes("Cobalt Skylift")
    h = Harness(tmp_path, llm=ScriptedLLM(answer), tariff_lanes=cobalt)
    mail = MailboxMessage(id=9001, sender=COBALT, subject="Fuel surcharge", body="Fuel CGN-EZE now 0.75 EUR/kg.",
                          created_at="2026-11-21T15:00:00Z")  # fmt: skip
    await h.receive(mail)
    await h.receive(mail)  # a replay or restart applies and confirms nothing twice
    confirmations = h.outbound("update_confirmation")
    assert len(confirmations) == 1 and confirmations[0].to == COBALT
    assert "fuel_per_kg 0.7 -> 0.75" in confirmations[0].body
    lanes = h.workflow._tariffs.lanes("CGN", "EZE", h.now.date())
    assert next(l for l in lanes if l.airline == "Cobalt Skylift").fuel_per_kg == 0.75
    assert [r.status for r in h.store.data_updates()] == ["applied"]


def test_only_a_reply_to_our_request_skips_the_classifier(tmp_path: Path) -> None:
    from app.schemas import MailboxMessage, TruckerRequest
    from app.schemas import MailContext
    from tests.test_workflow import Harness

    h = Harness(tmp_path)
    request = TruckerRequest(out_message_id=500, shipment_id=1, trucker="x@hackloop.mail", sent_at="t")
    ctx = MailContext(sender_role="trucker", shipment=None, trucker_request=request, airline_request=None, our_mail="")
    assert h.workflow.awaited_trucker_reply(ctx, MailboxMessage(id=1, in_reply_to=500))
    assert not h.workflow.awaited_trucker_reply(ctx, MailboxMessage(id=2, in_reply_to=None))  # e.g. a new tariff
    dry = request.model_copy(update={"out_message_id": -3})
    assert h.workflow.awaited_trucker_reply(ctx.model_copy(update={"trucker_request": dry}), MailboxMessage(id=3))
