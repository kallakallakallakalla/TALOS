import pytest

from app.config import Settings
from app.extractors import Extractors, missing_fields
from app.llm import StructuredLLM
from app.schemas import ShipmentRequest, TruckerQuotes
from tests.fakes import ScriptedLLM, labels, recorded_mails

SETTINGS = Settings(langfuse_enabled=False, decision_api_key="test-key")


def test_missing_fields_needs_destination_weight_volume_and_an_origin() -> None:
    complete = ShipmentRequest(pickup_postal_code="51896", destination_iata="ORD", gross_kg=679, volume_cbm=4.19)
    assert missing_fields(complete) == []
    assert missing_fields(complete.model_copy(update={"goal": None})) == []  # goal is never asked
    delivers = ShipmentRequest(origin_airport="DUS", destination_iata="JFK", gross_kg=1540, volume_cbm=5.9)
    assert missing_fields(delivers) == []
    # mail 3940: "truck pickup from our warehouse", no postal code and no airport
    no_origin = ShipmentRequest(destination_iata="PVG", gross_kg=349, volume_cbm=2.4)
    assert len(missing_fields(no_origin)) == 1 and "postal code" in missing_fields(no_origin)[0]
    assert len(missing_fields(ShipmentRequest())) == 4


@pytest.mark.anyio
async def test_extractors_use_their_own_prompt_and_pass_context() -> None:
    mails = recorded_mails()
    fake = ScriptedLLM(lambda schema, system, user: schema())
    extractors = Extractors(SETTINGS, StructuredLLM(SETTINGS, openai_client=fake))

    previous = ShipmentRequest(destination_iata="PVG", gross_kg=349)
    await extractors.extract_rfq(mails[3940], previous=previous, thread="Which postal code?")
    schema, system, user = fake.calls[-1]
    assert schema == "ShipmentRequest"
    assert "intake desk" in system and "2026-11-21" in system  # today = day the mail arrived
    assert '"destination_iata":"PVG"' in system  # PREVIOUS is carried into the prompt
    assert "Which postal code?" in user and "349 kg gross" in user

    result = await extractors.extract_trucker_quotes([mails[1063].body, mails[1056].body])
    schema, system, user = fake.calls[-1]
    assert isinstance(result, TruckerQuotes) and schema == "TruckerQuotes"
    assert user.startswith("REPLY 1:\n") and "\n\nREPLY 2:\n" in user
    assert "115.98 EUR" in user and "192.97 EUR" in user and "From:" not in user  # bodies only

    await extractors.extract_booking(mails[1623])
    assert fake.calls[-1][0] == "BookingConfirmation" and "BK-8RCF17DZ" in fake.calls[-1][2]


def test_every_recorded_inbound_mail_has_a_label() -> None:
    import json

    from tests.fakes import FIXTURES

    inbound = {m["id"] for m in json.loads((FIXTURES / "mails.json").read_text())["inbound"]}
    assert inbound == set(labels())
