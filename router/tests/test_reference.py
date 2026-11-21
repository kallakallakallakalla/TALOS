from app.reference import Reference, zone_of

REF = Reference("../data/start-kit")


def test_zone_gives_airport_and_only_the_truckers_serving_it() -> None:
    zone = zone_of("51447")
    assert REF.airport_for_zone(zone) == "CGN"
    assert sorted(t.name for t in REF.truckers_for_zone(zone)) == [
        "Bodenseefracht",
        "Elbkante Logistik",
        "Maintal Transport Logistik",
        "Westfalenroute",
    ]
    assert REF.airport_for_zone(zone_of("61018")) == "FRA"
    assert REF.airport_for_zone(zone_of("90402")) is None  # Nuremberg: not served


def test_sender_roles_and_airline_address() -> None:
    assert REF.role_for_email("elbkante-logistik@hackloop.mail") == "trucker"
    assert REF.role_for_email("Fjordline-Air-Cargo@hackloop.mail") == "airline"
    assert REF.role_for_email("tracking@hackloop.mail") == "tracking"
    assert REF.role_for_email("ratedesk@hackloop.mail") == "ratedesk"
    assert REF.role_for_email("rheintal-elektronik@hackloop.mail") == "customer"
    assert REF.airline_email("Yellowtail Air Freight") == "yellowtail-air-freight@hackloop.mail"
