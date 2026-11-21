"""The workflow end to end, on recorded mails, with a mocked platform and scripted LLM answers.

Mails go through the real poller pipeline (context -> classify -> store -> workflow). The
platform (mailbox + portal) is an httpx.MockTransport; the LLM answers come from the hand
labels in tests/fixtures/labels.json.
"""

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import pytest

from app.classifier import DecisionClassifier
from app.composer import Composer
from app.config import Settings
from app.extractors import Extractors
from app.llm import StructuredLLM
from app.mailbox_client import MailboxClient, parse_mailbox_message
from app.poller import MailboxPoller
from app.portal_client import PortalClient
from app.reference import Reference
from app.schemas import (
    BookingConfirmation,
    ClassificationDecision,
    ComposedMail,
    MailboxMessage,
    ShipmentRequest,
    ShipmentState,
    TariffLane,
    TruckerQuote,
)
from app.storage import MessageStore
from app.tariffs import Tariffs
from app.workflow import DRY_RUN_BOOKING_REF, Workflow
from tests.fakes import ScriptedLLM, labels, recorded_mails

MAILS = recorded_mails()
LABELS = labels()
ROUND_4_RFQ, ROUND_4_ACCEPT = 947, 1156
ROUND_4_TRUCKER_REPLIES = (1056, 1063, 1071, 1079)  # Maintal, Elbkante 115.98, Westfalen, Bodensee
# Portal totals for CGN-ORD at the real weight (699.73 kg chargeable); XC is the cheapest.
CGN_ORD_TOTALS = {"AU": "1920.10", "IB": "1701.55", "MD": "1650.00", "XC": "1483.43"}


class Platform:
    """The Hackloop API as far as the workflow uses it. Records every write."""

    def __init__(self) -> None:
        self.sent: list[dict] = []
        self.bookings: list[dict] = []
        self.rate_reads: list[dict] = []
        self.expired_rate_ids: set[str] = set()

    def __call__(self, request: httpx.Request) -> httpx.Response:
        path, params = request.url.path, request.url.params
        if path == "/v1/mailbox/messages" and request.method == "POST":
            mail = {**json.loads(request.content), "id": 90000 + len(self.sent), "from": "talos@hackloop.mail"}
            self.sent.append(mail)
            return httpx.Response(201, json=mail)
        if path.startswith("/v1/mailbox/messages/"):
            return httpx.Response(404)
        if path == "/v1/portal/ports":
            airlines = [{"code": code, "name": code} for code in CGN_ORD_TOTALS]
            return httpx.Response(200, json={"lanes": [{"origin": "CGN", "destination": "ORD", "airlines": airlines}]})
        if path == "/v1/portal/brackets":
            return httpx.Response(200, json={"breaks_kg": [0, 45, 100, 250, 500, 1000, 2000]})
        if path == "/v1/portal/rates":
            self.rate_reads.append(dict(params))
            weight = float(params["weight_kg"])
            at_real_weight = weight < 1000
            return httpx.Response(200, json={
                "rate_id": f"R-{params['airline']}{len(self.rate_reads):08d}",
                "airline": "Xenon Cargo Airlines" if params["airline"] == "XC" else params["airline"],
                "airline_code": params["airline"], "origin": params["origin"],
                "destination": params["destination"], "weight_kg": params["weight_kg"],
                "chargeable_weight_kg": str(round(max(weight, float(params["volume_cbm"]) * 167), 2)),
                "total": CGN_ORD_TOTALS[params["airline"]] if at_real_weight else "2100.00",
                "currency": "EUR", "transit_days": 3, "valid_until": "2026-11-21T15:34:18+00:00",
            })  # fmt: skip
        if path == "/v1/portal/bookings":
            body = json.loads(request.content)
            if body["rate_id"] in self.expired_rate_ids:
                return httpx.Response(410, json={"detail": "rate expired"})
            self.bookings.append(body)
            return httpx.Response(201, json={"booking_reference": "BK-ERC5OOW3", **body})
        raise AssertionError(f"unexpected call {request.method} {path}")


def recorded_llm(overrides: dict | None = None) -> ScriptedLLM:
    """Answers every LLM call with the hand label of the mail in the prompt."""
    overrides = overrides or {}

    def mail_id_in(user: str) -> int:
        return next(i for i, m in MAILS.items() if m.body.strip() and m.body.strip() in user)

    def answer(schema, system: str, user: str):
        if schema is ClassificationDecision:
            mail_id = int(user.split("Input ID: ")[1].split("\n")[0])
            # A re-sent copy of a recorded mail (new id) keeps the original's label.
            label_id = mail_id if mail_id in LABELS else mail_id_in(user)
            category = overrides.get(label_id) or LABELS[label_id]["category"]
            return ClassificationDecision(category=category, confidence=0.99, reasoning="label")
        if schema is ShipmentRequest:
            label = LABELS[mail_id_in(user)]["rfq"]
            return ShipmentRequest(**label, contact_name="Deniz", language="en", pickup_city="Bergisch Gladbach")
        if schema is TruckerQuote:
            return TruckerQuote(**LABELS[mail_id_in(user)]["trucker_quote"])
        if schema is BookingConfirmation:
            reference = user.split("Your reference: ")[1].split("\n")[0]
            return BookingConfirmation(booking_ref="BK-ERC5OOW3", our_reference=reference)
        facts = json.loads(system.split("FACTS (the only source of truth):\n")[1].split("\n\nHard rules")[0])
        kind = system.split("Mail to write: ")[1].split()[0]
        return ComposedMail(subject=kind, body=f"{kind}\n" + "\n".join(map(str, facts["must_appear"])))

    return ScriptedLLM(answer)


class Harness:
    """A router with a fake clock, a mocked platform and scripted LLM answers."""

    def __init__(self, tmp_path: Path, llm: ScriptedLLM | None = None, tariff_lanes=(), **settings) -> None:
        self.now = datetime(2026, 11, 21, 15, 19, 13, tzinfo=UTC)
        self.platform = Platform()
        self.llm = llm or recorded_llm()
        self.settings = Settings(
            langfuse_enabled=False, hackloop_api_key="team-key", decision_api_key="k",
            sqlite_db_path=str(tmp_path / "wf.db"), **settings,
        )  # fmt: skip
        self.store = MessageStore(self.settings.sqlite_db_path)
        http = httpx.AsyncClient(transport=httpx.MockTransport(self.platform))
        mailbox = MailboxClient(self.settings, http)
        structured = StructuredLLM(self.settings, openai_client=self.llm)
        tariffs = Tariffs(str(tmp_path / "tariffs.json"))
        if tariff_lanes:
            tariffs.replace_airline(tariff_lanes[0].airline, list(tariff_lanes))
        self.workflow = Workflow(
            self.settings, self.store, mailbox, PortalClient(self.settings, http),
            Reference(self.settings.start_kit_dir), tariffs,
            Extractors(self.settings, structured), Composer(self.settings, structured),
            clock=lambda: self.now,
        )  # fmt: skip
        self.poller = MailboxPoller(
            self.settings, mailbox, DecisionClassifier(self.settings, llm=structured), self.store, self.workflow
        )

    async def receive(self, mail: int | MailboxMessage, **changes) -> None:
        msg = MAILS[mail] if isinstance(mail, int) else mail
        await self.poller.process_message(msg.model_copy(update=changes))

    async def wait(self, seconds: float) -> None:
        self.now += timedelta(seconds=seconds)
        await self.workflow.tick()

    def shipment(self, shipment_id: int = ROUND_4_RFQ):
        return self.store.get_shipment(shipment_id)

    def outbound(self, kind: str) -> list:
        return [m for m in self.store.outbound_mails() if m.kind == kind]

    async def truckers_reply(self) -> None:
        """Each recorded trucker reply answers the request this router sent to that trucker."""
        request_id = {m["to"]: m["id"] for m in self.platform.sent if m["subject"] == "trucker_request"}
        for reply_id in ROUND_4_TRUCKER_REPLIES:
            await self.receive(reply_id, in_reply_to=request_id[MAILS[reply_id].sender])


@pytest.mark.anyio
async def test_round_4_replay_quotes_1599_41_and_books_with_reference_947(tmp_path: Path) -> None:
    h = Harness(tmp_path, send_enabled=True)

    await h.receive(ROUND_4_RFQ)
    assert h.shipment().state == ShipmentState.PRICING
    assert (h.shipment().zone, h.shipment().origin_airport) == ("51", "CGN")
    requests = [m for m in h.platform.sent if m["subject"] == "trucker_request"]
    assert sorted(m["to"] for m in requests) == [
        "bodenseefracht@hackloop.mail", "elbkante-logistik@hackloop.mail",
        "maintal-transport-logistik@hackloop.mail", "westfalenroute@hackloop.mail",
    ]  # fmt: skip
    assert all("679 kg" in m["body"] and "51896" in m["body"] for m in requests)
    assert len(h.platform.rate_reads) == 8  # 4 airlines x (real weight + next break)

    await h.truckers_reply()  # the 4th answer triggers the quote, no timer needed
    shipment = h.shipment()
    assert shipment.state == ShipmentState.QUOTED
    assert shipment.quote["all_in_eur"] == 1599.41
    assert (shipment.quote["air_eur"], shipment.quote["truck_eur"]) == (1483.43, 115.98)
    assert shipment.quote["trucker"] == "elbkante-logistik@hackloop.mail"
    (quote,) = [m for m in h.platform.sent if m["to"] == shipment.customer]
    assert "1599.41 EUR" in quote["body"]
    assert quote["in_reply_to"] == ROUND_4_RFQ and quote["subject"] == "Re: Air freight quote to ORD"

    await h.receive(ROUND_4_ACCEPT, in_reply_to=quote["id"], references=[ROUND_4_RFQ, quote["id"]])
    assert h.platform.bookings == [{"rate_id": shipment.chosen_option.rate_id, "team_reference": "947"}]
    assert h.shipment().state == ShipmentState.BOOKED
    assert h.shipment().booking_ref == "BK-ERC5OOW3"
    note = h.platform.sent[-1]
    assert note["to"] == shipment.customer and "BK-ERC5OOW3" in note["body"]
    assert note["in_reply_to"] == ROUND_4_ACCEPT

    confirmation = MAILS[2225].model_copy(update={"body": MAILS[2225].body.replace("TALOS-947-1", "947")})
    await h.receive(confirmation)
    assert h.shipment().state == ShipmentState.CONFIRMED

    # A second acceptance or a "thanks for booking" must not book or mail again (reply loop).
    sent_before = len(h.platform.sent)
    await h.receive(ROUND_4_ACCEPT, id=5000, references=[ROUND_4_RFQ, quote["id"]])
    await h.receive(2204)  # "Thank you for confirming the booking. Please keep us informed..."
    assert len(h.platform.sent) == sent_before and len(h.platform.bookings) == 1


@pytest.mark.anyio
async def test_dry_run_composes_everything_but_never_sends_or_books(tmp_path: Path) -> None:
    h = Harness(tmp_path)  # SEND_ENABLED defaults to false
    assert h.settings.send_enabled is False

    await h.receive(ROUND_4_RFQ)
    for reply_id in ROUND_4_TRUCKER_REPLIES:
        await h.receive(reply_id)  # they answer the OLD process's mails; matched by sender
    await h.receive(ROUND_4_ACCEPT)

    shipment = h.shipment()
    assert shipment.state == ShipmentState.BOOKED and shipment.booking_ref == DRY_RUN_BOOKING_REF
    assert "1599.41 EUR" in h.outbound("quote")[0].body
    assert [len(h.outbound(k)) for k in ("trucker_request", "quote", "booking_note")] == [4, 1, 1]
    assert all(m.sent_message_id is None for m in h.store.outbound_mails())
    assert h.platform.sent == [] and h.platform.bookings == []


@pytest.mark.anyio
async def test_trucker_wait_45_seconds_with_a_price_and_fallback_at_90(tmp_path: Path) -> None:
    h = Harness(tmp_path)
    await h.receive(ROUND_4_RFQ)
    await h.receive(1056)  # Maintal 192.97; the other three stay silent
    await h.wait(44)
    assert h.shipment().state == ShipmentState.PRICING
    await h.wait(2)
    assert h.shipment().state == ShipmentState.QUOTED
    assert h.shipment().quote["all_in_eur"] == round(1483.43 + 192.97, 2)
    await h.receive(1063)  # a cheaper trucker arrives late: stored, but no second quote
    assert len(h.outbound("quote")) == 1

    silent = Harness(tmp_path / "silent")
    await silent.receive(ROUND_4_RFQ)
    await silent.wait(89)
    assert silent.shipment().state == ShipmentState.PRICING
    await silent.wait(2)
    quote = silent.shipment().quote
    assert quote["truck_fallback"] is True
    assert quote["truck_eur"] == round(679 * 0.28, 2)  # no stored quotes yet -> 0.28 EUR/kg
    assert quote["all_in_eur"] == round(1483.43 + 190.12, 2)


@pytest.mark.anyio
async def test_incomplete_request_is_asked_three_times_then_fails(tmp_path: Path) -> None:
    h = Harness(tmp_path, send_enabled=True)
    rfq = 3940  # "truck pickup from our warehouse": no postal code, no airport
    await h.receive(rfq)
    assert h.shipment(rfq).state == ShipmentState.AWAITING_INFO
    assert h.platform.rate_reads == []  # nothing is priced before the request is complete

    for attempt in (1, 2, 3):
        assert h.shipment(rfq).clarifications == attempt
        assert len(h.outbound("clarification")) == attempt
        vague = MAILS[rfq].model_copy(update={"id": 4000 + attempt, "references": [rfq]})
        h.llm._answer = _as_customer_info(h.llm._answer, vague.id)  # noqa: SLF001
        await h.receive(vague)
    assert h.shipment(rfq).state == ShipmentState.FAILED
    assert len(h.outbound("clarification")) == 3


def _as_customer_info(answer, mail_id: int):
    def wrapped(schema, system, user):
        if schema is ClassificationDecision and f"Input ID: {mail_id}\n" in user:
            return ClassificationDecision(category="customer_info", confidence=0.9, reasoning="test")
        return answer(schema, system, user)

    return wrapped


@pytest.mark.anyio
async def test_unknown_pickup_zone_asks_the_customer_for_an_airport(tmp_path: Path) -> None:
    def answer(schema, system, user):
        if schema is ShipmentRequest:
            return ShipmentRequest(pickup_postal_code="90402", destination_iata="ORD", gross_kg=100, volume_cbm=1)
        return fallback(schema, system, user)

    fallback = recorded_llm()._answer  # noqa: SLF001
    h = Harness(tmp_path, llm=ScriptedLLM(answer))
    await h.receive(ROUND_4_RFQ)
    assert h.shipment().state == ShipmentState.AWAITING_INFO
    facts = h.llm.calls[-1][1]
    assert "90402" in facts and "German airport" in facts
    assert h.outbound("trucker_request") == []


@pytest.mark.anyio
async def test_expired_rate_is_refetched_before_booking(tmp_path: Path) -> None:
    h = Harness(tmp_path, send_enabled=True)
    await h.receive(ROUND_4_RFQ)
    await h.truckers_reply()
    old_rate_id = h.shipment().chosen_option.rate_id
    h.platform.expired_rate_ids.add(old_rate_id)

    await h.receive(ROUND_4_ACCEPT)
    shipment = h.shipment()
    assert shipment.state == ShipmentState.BOOKED and shipment.booking_attempts == 2
    assert h.platform.bookings[0]["rate_id"] != old_rate_id
    assert h.platform.bookings[0]["team_reference"] == "947"


@pytest.mark.anyio
async def test_cheaper_tariff_airline_is_booked_by_email_and_reminded_at_most_three_times(
    tmp_path: Path,
) -> None:
    lane = TariffLane(
        airline="Elbflug Air Freight", origin="CGN", destination="ORD", transit_days=4,
        minimum_eur=80, breaks_kg=[0, 500], rates_per_kg=[1.5, 1.0], fuel_per_kg=0.4,
        security_per_kg=0.1, valid_from="2026-11-01", valid_to="2026-11-30",
    )  # fmt: skip
    h = Harness(tmp_path, tariff_lanes=[lane], send_enabled=True)
    await h.receive(ROUND_4_RFQ)
    await h.truckers_reply()
    shipment = h.shipment()
    assert shipment.chosen_option.source == "tariff"
    assert shipment.quote["air_eur"] == round(699.73 * 1.5, 2)  # 1049.60 beats the portal's 1483.43

    await h.receive(ROUND_4_ACCEPT)
    assert h.platform.bookings == []  # no rate_id: the booking API is not used
    request = h.platform.sent[-1]
    assert request["to"] == "elbflug-air-freight@hackloop.mail"
    assert "947" in request["body"] and "CGN-ORD" in request["body"]
    assert h.shipment().state == ShipmentState.BOOKING

    await h.wait(599)
    assert len(h.outbound("booking_request")) == 1
    await h.wait(2)  # 10 minutes without a confirmation: first reminder
    await h.wait(601)
    assert len(h.outbound("booking_request")) == 3
    await h.wait(601)  # three requests used up
    assert h.shipment().state == ShipmentState.FAILED

    # The airline's confirmation books the shipment and the customer gets the booking note.
    booked = Harness(tmp_path / "booked", tariff_lanes=[lane], send_enabled=True)
    await booked.receive(ROUND_4_RFQ)
    await booked.truckers_reply()
    await booked.receive(ROUND_4_ACCEPT)
    confirmation = parse_mailbox_message({
        "id": 6000, "from": "elbflug-air-freight@hackloop.mail", "subject": "Booking BK-ERC5OOW3 confirmed",
        "body": "Booking reference: BK-ERC5OOW3\nYour reference: 947\nStatus: booked",
    })  # fmt: skip
    booked.llm._answer = _as_category(booked.llm._answer, 6000, "booking_confirmation")  # noqa: SLF001
    await booked.receive(confirmation)
    assert booked.shipment().state == ShipmentState.CONFIRMED
    assert booked.platform.sent[-1]["to"] == booked.shipment().customer
    assert "BK-ERC5OOW3" in booked.platform.sent[-1]["body"]


def _as_category(answer, mail_id: int, category: str):
    def wrapped(schema, system, user):
        if schema is ClassificationDecision and f"Input ID: {mail_id}\n" in user:
            return ClassificationDecision(category=category, confidence=0.9, reasoning="test")
        return answer(schema, system, user)

    return wrapped


@pytest.mark.anyio
async def test_rejection_closes_the_shipment_and_questions_are_answered_at_most_three_times(
    tmp_path: Path,
) -> None:
    h = Harness(tmp_path, llm=recorded_llm({ROUND_4_ACCEPT: "customer_question"}), send_enabled=True)
    await h.receive(ROUND_4_RFQ)
    await h.truckers_reply()
    for n in range(5):
        await h.receive(ROUND_4_ACCEPT, id=7000 + n)
    assert len(h.outbound("answer")) == 3 and h.shipment().answers_sent == 3

    await h.receive(379, references=[ROUND_4_RFQ, 1])  # "That seems too low to be right..."
    assert h.shipment().state == ShipmentState.REJECTED
    assert h.platform.bookings == []
