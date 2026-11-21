from pathlib import Path

from app.schemas import AirOption, Shipment, ShipmentRequest, ShipmentState, TruckerRequest
from app.storage import MessageStore


def _store(tmp_path: Path) -> MessageStore:
    return MessageStore(db_path=str(tmp_path / "wf.db"))


def test_shipment_round_trips_through_all_states(tmp_path: Path) -> None:
    store = _store(tmp_path)
    shipment = Shipment(
        id=947,
        customer="rheintal-elektronik@hackloop.mail",
        subject="Air freight quote to ORD",
        request=ShipmentRequest(pickup_postal_code="51896", destination_iata="ORD", gross_kg=679, volume_cbm=4.19),
        goal="cheapest",
        zone="51",
    )
    for state in ShipmentState:
        shipment.state = state
        store.save_shipment(shipment)
        loaded = store.get_shipment(947)
        assert loaded.state == state
        assert loaded.request.gross_kg == 679
        assert [s.id for s in store.list_shipments(state)] == [947]

    shipment.chosen_option = AirOption(
        source="portal", airline="Xenon Cargo Airlines", airline_code="XC", origin="CGN",
        destination="ORD", billed_kg=699.73, total_eur=1483.43, transit_days=3, rate_id="R-p5XnA5nn",
    )  # fmt: skip
    shipment.quote = {"all_in_eur": 1599.41}
    store.save_shipment(shipment)
    loaded = store.get_shipment(947)
    assert loaded.chosen_option.rate_id == "R-p5XnA5nn"
    assert loaded.quote == {"all_in_eur": 1599.41}
    assert loaded.created_at and loaded.updated_at
    assert store.get_shipment(1) is None


def test_trucker_requests_air_options_and_outbound(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.save_shipment(
        Shipment(id=947, customer="c@x", subject="s", zone="51", request=ShipmentRequest(gross_kg=679))
    )
    request = TruckerRequest(out_message_id=964, shipment_id=947, trucker="elbkante@x", sent_at="2026-11-21T15:19:15")
    store.save_trucker_request(request)

    # exact match on the mail we sent; otherwise the trucker's open request (dry run)
    assert store.find_trucker_request(964, "someone@x").trucker == "elbkante@x"
    assert store.find_trucker_request(1033, "elbkante@x").out_message_id == 964
    assert store.find_trucker_request(1033, "maintal@x") is None

    request.answered, request.price_eur, request.pickup_hours, request.ref = True, 115.98, 29, "TRK-375"
    store.save_trucker_request(request)
    assert store.trucker_requests(947)[0].price_eur == 115.98
    assert store.find_trucker_request(None, "elbkante@x") is None  # already answered
    assert store.trucker_quote_history() == [("51", 115.98, 679.0)]

    option = AirOption(source="tariff", airline="Elbflug Air Freight", origin="CGN", destination="ORD",
                       billed_kg=699.73, total_eur=1700.0, transit_days=4)  # fmt: skip
    store.replace_air_options(947, [option, option.model_copy(update={"total_eur": 1500.0})])
    store.replace_air_options(947, [option])
    assert store.air_options(947) == [option]

    mail = store.add_outbound(kind="quote", to="c@x", subject="Re: s", body="1599.41 EUR", shipment_id=947, in_reply_to=947)
    assert store.find_outbound(-mail.id).body == "1599.41 EUR"  # dry-run id
    store.mark_outbound_sent(mail.id, 1082)
    assert store.find_outbound(1082).kind == "quote"
    assert [m.id for m in store.outbound_mails(947)] == [mail.id]
