import pytest

from app import templates
from app.composer import Composer, missing_facts, wording
from app.config import Settings
from app.llm import StructuredLLM
from app.schemas import ComposedMail
from tests.fakes import ScriptedLLM

FOOTER = "TALOS Air Freight\nTheresienstraße 90, 80333 München\nPhone: +49 89 99998 417"
FOOTER_BLOCK = f"\n\n{'-' * 40}\n{FOOTER}\n{'-' * 40}"
SETTINGS = Settings(
    langfuse_enabled=False, decision_api_key="test-key",
    mail_signature="Alex, TALOS Air Freight", mail_footer=FOOTER,
)  # fmt: skip

SHIPMENT = {"ready_date": "2026-11-28", "pieces": 6, "gross_weight": "679 kg", "volume": "4.19 cbm"}
FACTS = {
    "quote": {
        "route": "CGN -> ORD", "airline": "Xenon Cargo Airlines", "transit_days": 3,
        "all_in_price": "1599.41 EUR", "pickup_included": True,
        "pickup_from": "51896 Bergisch Gladbach", "estimated_departure": "2026-11-30",
        "estimated_arrival": "2026-12-03", "valid_until": "2026-11-21 15:34 UTC",
        "must_appear": ["1599.41 EUR"],
    },
    "trucker_request": {
        **SHIPMENT, "pickup_postal_code": "51896", "pickup_city": "Bergisch Gladbach",
        "airport": "CGN", "must_appear": ["51896", "CGN", "2026-11-28", "679 kg", "4.19 cbm"],
    },
    "airline_rate_request": {
        **SHIPMENT, "lane": "FRA-ULN", "commodity": "machine parts", "our_reference": "10830",
        "must_appear": ["FRA-ULN", "10830", "679 kg"],
    },
    "no_price": {"destination": "ULN", "route": "FRA-ULN", "must_appear": ["ULN"]},
    "clarification": {"missing_fields": ["total gross weight in kg"], "must_appear": []},
    "booking_note": {
        "route": "CGN -> ORD", "airline": "Xenon Cargo Airlines", "booking_ref": "BK-ERC5OOW3",
        "estimated_arrival": "2026-12-03", "ready_date": "2026-11-28",
        "must_appear": ["BK-ERC5OOW3"],
    },
    "booking_request": {
        **SHIPMENT, "lane": "FRA-ORD", "chargeable_weight": "699.73 kg",
        "tariff_total": "1700.00 EUR", "our_reference": "947",
        "must_appear": ["FRA-ORD", "947", "1700.00 EUR"],
    },
    "answer": {"question": "When does it arrive?", "must_appear": []},
    "tracking_answer": {
        "found": True, "route": "CGN -> ORD", "airline": "Xenon Cargo Airlines",
        "status": "booked and confirmed by the airline, ready to ship",
        "booking_reference": "BK-ERC5OOW3", "transit_days": 3,
        "estimated_arrival": "2026-12-03", "must_appear": ["BK-ERC5OOW3"],
    },
}  # fmt: skip


def _composer(answer) -> tuple[Composer, ScriptedLLM]:
    fake = ScriptedLLM(answer)
    return Composer(SETTINGS, StructuredLLM(SETTINGS, openai_client=fake)), fake


@pytest.mark.parametrize("kind", templates.MAIL_KINDS)
def test_every_template_passes_its_own_fact_check(kind: str) -> None:
    mail = templates.render(kind, FACTS[kind], SETTINGS.mail_signature, "Deniz")
    assert missing_facts(FACTS[kind], mail) == []
    assert mail.body.startswith("Dear Deniz,") and mail.body.endswith("Alex, TALOS Air Freight")


@pytest.mark.anyio
async def test_good_draft_is_used_and_reply_subject_is_set_in_code() -> None:
    composer, fake = _composer(
        lambda *_: ComposedMail(subject="Great offer!!", body="Hi Deniz, all-in 1599.41 EUR. Alex")
    )
    mail, used_template = await composer.compose(
        "quote", FACTS["quote"], recipient_role="customer", recipient_name="Deniz",
        reply_to_subject="Air freight quote to ORD",
    )  # fmt: skip
    assert not used_template and len(fake.calls) == 1
    assert mail.subject == "Re: Air freight quote to ORD"
    assert '"all_in_price": "1599.41 EUR"' in fake.calls[0][1]  # facts reach the prompt


@pytest.mark.anyio
async def test_wrong_price_is_retried_once_then_replaced_by_the_template() -> None:
    drafts = iter([
        ComposedMail(subject="s", body="all-in EUR 1,599.41"),  # reformatted price: rejected
        ComposedMail(subject="s", body="all-in 1599.41 EUR"),
    ])  # fmt: skip
    composer, fake = _composer(lambda *_: next(drafts))
    mail, used_template = await composer.compose("quote", FACTS["quote"], recipient_role="customer")
    assert not used_template and len(fake.calls) == 2
    assert "1599.41 EUR" in fake.calls[1][2]  # the retry names the missing value

    composer, fake = _composer(lambda *_: ComposedMail(subject="s", body="about 1600 euros"))
    mail, used_template = await composer.compose("quote", FACTS["quote"], recipient_role="customer")
    assert used_template and len(fake.calls) == 2
    assert "1599.41 EUR" in mail.body

    composer, fake = _composer(lambda *_: RuntimeError("gateway down"))
    mail, used_template = await composer.compose("booking_note", FACTS["booking_note"], recipient_role="customer")
    assert used_template and "BK-ERC5OOW3" in mail.body


@pytest.mark.anyio
async def test_customer_mail_is_plain_text_varied_and_ends_with_the_footer() -> None:
    draft = ComposedMail(subject="s", body="Hi Deniz,\n\n**1599.41 EUR** all-in — 3 days transit.\n\nBest,\nAlex\n")
    composer, fake = _composer(lambda *_: draft)
    mail, _ = await composer.compose("quote", FACTS["quote"], recipient_role="customer")
    assert mail.body == "Hi Deniz,\n\n1599.41 EUR all-in, 3 days transit.\n\nBest,\nAlex" + FOOTER_BLOCK
    request = fake.calls[0][2]
    assert "Today is 20" in request  # the model gets today's date for "today until ..."
    assert all(f'{slot} "' in request for slot in ("greeting", "opener", "call to action", "sign-off", "slip"))

    # the fallback template gets the footer too
    composer, _ = _composer(lambda *_: RuntimeError("gateway down"))
    mail, used_template = await composer.compose("quote", FACTS["quote"], recipient_role="customer")
    assert used_template and mail.body.endswith("Alex, TALOS Air Freight" + FOOTER_BLOCK)

    # a mail to an airline has no footer and no wording picks
    composer, fake = _composer(lambda *_: ComposedMail(subject="s", body="FRA-ORD, 947, 1700.00 EUR\nAlex"))
    mail, _ = await composer.compose("booking_request", FACTS["booking_request"], recipient_role="airline")
    assert mail.body.endswith("Alex") and "Wording" not in fake.calls[0][2]


def test_wording_changes_from_mail_to_mail() -> None:
    assert len({wording() for _ in range(50)}) > 10
