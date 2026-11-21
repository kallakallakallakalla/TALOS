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
    AirlineRate,
    AirlineRates,
    AttachmentText,
    BookingConfirmation,
    MailboxAttachment,
    ClassificationDecision,
    ComposedMail,
    MailboxMessage,
    ShipmentRequest,
    ShipmentState,
    TariffLane,
    TrackingQuery,
    TruckerQuote,
    TruckerQuotes,
)
from app.storage import MessageStore
from app.tariffs import Tariffs
from app.workflow import DRY_RUN_BOOKING_REF, Workflow, match_shipment
from tests.fakes import ScriptedLLM, labels, recorded_mails

MAILS = recorded_mails()
LABELS = labels()
ROUND_4_RFQ, ROUND_4_ACCEPT = 947, 1156
ROUND_4_TRUCKER_REPLIES = (1056, 1063, 1071, 1079)  # Maintal, Elbkante 115.98, Westfalen, Bodensee
# Portal totals for CGN-ORD at the real weight (699.73 kg chargeable); XC is the cheapest.
PORTAL_DESTINATIONS = (
    "ATL BKK BOG BOM CAI DEL DFW DOH DXB EZE GRU HKG ICN JFK JNB KUL LAX LIM LOS MEL MEX MIA "
    "MNL NBO NRT ORD PEK PVG RUH SCL SGN SIN SYD TLV TPE YYZ"
).split()  # docs/platform-api.md
CGN_ORD_TOTALS = {"AU": "1920.10", "IB": "1701.55", "MD": "1650.00", "XC": "1483.43"}


class Platform:
    """The Hackloop API as far as the workflow uses it. Records every write."""

    def __init__(self) -> None:
        self.sent: list[dict] = []
        self.bookings: list[dict] = []
        self.rate_reads: list[dict] = []
        self.expired_rate_ids: set[str] = set()
        self.booking_closed = False
        self.files: dict[str, bytes] = {}  # attachment id -> bytes

    def __call__(self, request: httpx.Request) -> httpx.Response:
        path, params = request.url.path, request.url.params
        if path == "/v1/mailbox/messages" and request.method == "POST":
            mail = {**json.loads(request.content), "id": 90000 + len(self.sent), "from": "talos@hackloop.mail"}
            self.sent.append(mail)
            return httpx.Response(201, json=mail)
        if "/attachments/" in path and path.rsplit("/", 1)[1] in self.files:
            return httpx.Response(200, content=self.files[path.rsplit("/", 1)[1]])
        if path.startswith("/v1/mailbox/messages/"):
            return httpx.Response(404)
        if path == "/v1/portal/ports":
            airlines = [{"code": code, "name": code} for code in CGN_ORD_TOTALS]
            # The other portal destinations exist, but this mock prices none of them.
            others = [{"origin": "FRA", "destination": d, "airlines": []} for d in PORTAL_DESTINATIONS]
            lanes = [{"origin": "CGN", "destination": "ORD", "airlines": airlines}, *others]
            return httpx.Response(200, json={"lanes": lanes})
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
            if self.booking_closed:
                return httpx.Response(503, json={"detail": "Booking is not available. Please book by email."})
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
        if schema is TruckerQuotes:  # "REPLY 1:\n<body>\n\nREPLY 2:\n<body>..."
            bodies = [part.split(":\n", 1)[1] for part in user.split("REPLY ")[1:]]
            return TruckerQuotes(quotes=[
                TruckerQuote(reply=n, **LABELS[mail_id_in(body)].get("trucker_quote", {}))
                for n, body in enumerate(bodies, start=1)
            ])  # fmt: skip
        if schema is TrackingQuery:
            return overrides.get("tracking") or TrackingQuery(
                company="Rheintal Elektronik GmbH", contact_name="Deniz", origin="51896",
                destination_iata="ORD", gross_kg=679, ready_date="2026-11-28", language="en",
            )  # fmt: skip
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
            sqlite_db_path=str(tmp_path / "wf.db"), portal_lanes_cache_path=str(tmp_path / "lanes.json"),
            **settings,
        )  # fmt: skip
        self.store = MessageStore(self.settings.sqlite_db_path)
        http = httpx.AsyncClient(transport=httpx.MockTransport(self.platform))
        mailbox = MailboxClient(self.settings, http)
        structured = StructuredLLM(self.settings, openai_client=self.llm)
        tariffs = Tariffs(str(tmp_path / "tariffs.json"), self.settings.sqlite_db_path)
        with tariffs.db:
            tariffs.put(list(tariff_lanes))
        self.workflow = Workflow(
            self.settings, self.store, mailbox, PortalClient(self.settings, http),
            Reference(self.settings.start_kit_dir, self.settings.sqlite_db_path), tariffs,
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
        request_id = {m["to"]: m["id"] for m in self.platform.sent if m["subject"].startswith("Pickup quote")}
        for reply_id in ROUND_4_TRUCKER_REPLIES:
            await self.receive(reply_id, in_reply_to=request_id[MAILS[reply_id].sender])


@pytest.mark.anyio
async def test_round_4_replay_quotes_1599_41_and_books_with_reference_947(tmp_path: Path) -> None:
    h = Harness(tmp_path, send_enabled=True)

    await h.receive(ROUND_4_RFQ)
    assert h.shipment().state == ShipmentState.PRICING
    assert (h.shipment().zone, h.shipment().origin_airport) == ("51", "CGN")
    requests = [m for m in h.platform.sent if m["subject"].startswith("Pickup quote")]
    assert sorted(m["to"] for m in requests) == [
        "bodenseefracht@hackloop.mail", "elbkante-logistik@hackloop.mail",
        "maintal-transport-logistik@hackloop.mail", "westfalenroute@hackloop.mail",
    ]  # fmt: skip
    assert all("679 kg" in m["body"] and "51896" in m["body"] for m in requests)
    assert requests[0]["subject"] == "Pickup quote 51896 Bergisch Gladbach to CGN on 2026-11-28"
    assert len({m["body"] for m in requests}) == 1  # one fixed text for all truckers
    assert not any(schema == "ComposedMail" for schema, _, _ in h.llm.calls)  # no LLM call for it
    assert len(h.platform.rate_reads) == 8  # 4 airlines x (real weight + next break)

    await h.truckers_reply()  # the 4th answer triggers the quote, no timer needed
    shipment = h.shipment()
    # 4 trucker replies cost ONE LLM call: no classifier call each, one batched read
    schemas = [schema for schema, _, _ in h.llm.calls]
    assert schemas.count("ClassificationDecision") == 1 and schemas.count("TruckerQuotes") == 1
    assert [m.decision.category.value for m in map(h.store.get_message, ROUND_4_TRUCKER_REPLIES)] == ["trucker_quote"] * 4
    assert shipment.state == ShipmentState.QUOTED
    assert shipment.quote["all_in_eur"] == 1599.41
    assert (shipment.quote["air_eur"], shipment.quote["truck_eur"]) == (1483.43, 115.98)
    assert shipment.quote["trucker"] == "elbkante-logistik@hackloop.mail"  # cheapest, 115.98 (ADR 52)
    (quote,) = [m for m in h.platform.sent if m["to"] == shipment.customer]
    assert "1599.41 EUR" in quote["body"]
    assert quote["in_reply_to"] == ROUND_4_RFQ and quote["subject"] == "Re: Air freight quote to ORD"

    await h.receive(ROUND_4_ACCEPT, in_reply_to=quote["id"], references=[ROUND_4_RFQ, quote["id"]])
    # The price is checked on the portal again before booking; it held, so the fresh rate is booked.
    assert h.platform.bookings == [{"rate_id": h.shipment().chosen_option.rate_id, "team_reference": "947"}]
    assert h.shipment().chosen_option.rate_id != shipment.chosen_option.rate_id
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
async def test_after_60_seconds_the_quote_uses_whatever_truckers_came_back(tmp_path: Path) -> None:
    h = Harness(tmp_path)
    await h.receive(ROUND_4_RFQ)
    await h.receive(1056)  # Maintal 192.97; the other three stay silent
    await h.wait(59)
    assert h.shipment().state == ShipmentState.PRICING
    assert not any(schema == "TruckerQuotes" for schema, _, _ in h.llm.calls)  # not read yet
    await h.wait(2)
    assert h.shipment().state == ShipmentState.QUOTED
    assert h.shipment().quote["all_in_eur"] == round(1483.43 + 192.97, 2)
    await h.receive(1063)  # a cheaper trucker arrives late: noted, but no second quote
    assert len(h.outbound("quote")) == 1

    silent = Harness(tmp_path / "silent")
    await silent.receive(ROUND_4_RFQ)
    await silent.wait(59)
    assert silent.shipment().state == ShipmentState.PRICING
    await silent.wait(2)
    quote = silent.shipment().quote
    assert quote["truck_fallback"] is True
    assert quote["truck_eur"] == round(679 * 0.28, 2)  # no stored quotes yet -> 0.28 EUR/kg
    assert quote["all_in_eur"] == round(1483.43 + 190.12, 2)


@pytest.mark.anyio
async def test_a_trucker_price_that_is_not_in_the_mail_is_never_used(tmp_path: Path) -> None:
    """Accuracy guard: an extracted price must stand in that trucker's own mail."""
    answers = recorded_llm()._answer  # noqa: SLF001

    def swapped(schema, system, user):
        result = answers(schema, system, user)
        if schema is TruckerQuotes:  # the model mixes up the replies / invents a price
            for quote in result.quotes:
                quote.price_eur = 99.99
        return result

    h = Harness(tmp_path, llm=ScriptedLLM(swapped))
    await h.receive(ROUND_4_RFQ)
    for reply_id in ROUND_4_TRUCKER_REPLIES:
        await h.receive(reply_id)
    quote = h.shipment().quote
    assert [s for s, _, _ in h.llm.calls].count("TruckerQuotes") == 2  # one retry
    assert quote["truck_fallback"] is True and quote["trucker"] is None  # 99.99 was not used


@pytest.mark.anyio
async def test_a_trucker_who_asks_back_counts_as_answered_without_a_price(tmp_path: Path) -> None:
    h = Harness(tmp_path)
    await h.receive(1977)  # Fulda -> LOS; the recorded truckers answered "we need the weight"
    for reply_id in (2062, 2063, 2068):
        await h.receive(reply_id)
    assert h.shipment(1977).state != ShipmentState.PRICING  # all three answered: no 60 s wait
    assert all(r.answered and r.price_eur is None for r in h.store.trucker_requests(1977))


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
async def test_quoted_rate_is_refetched_before_booking(tmp_path: Path) -> None:
    h = Harness(tmp_path, send_enabled=True)
    await h.receive(ROUND_4_RFQ)
    await h.truckers_reply()
    old_rate_id = h.shipment().chosen_option.rate_id
    h.platform.expired_rate_ids.add(old_rate_id)

    await h.receive(ROUND_4_ACCEPT)
    shipment = h.shipment()
    assert shipment.state == ShipmentState.BOOKED and shipment.booking_attempts == 1
    assert h.platform.bookings[0]["rate_id"] not in (old_rate_id, None)
    assert h.platform.bookings[0]["team_reference"] == "947"


@pytest.mark.anyio
@pytest.mark.parametrize(("new_air", "went"), [("1583.43", "up"), ("1383.43", "down")])
async def test_changed_portal_price_asks_the_customer_again_before_booking(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, new_air: str, went: str
) -> None:
    h = Harness(tmp_path, send_enabled=True)
    await h.receive(ROUND_4_RFQ)
    await h.truckers_reply()
    monkeypatch.setitem(CGN_ORD_TOTALS, "XC", new_air)  # the airline price moved since the quote

    await h.receive(ROUND_4_ACCEPT)
    shipment = h.shipment()
    assert shipment.state == ShipmentState.QUOTED and h.platform.bookings == []
    new_all_in = round(1599.41 - 1483.43 + float(new_air), 2)
    assert shipment.quote["all_in_eur"] == new_all_in
    (update,) = h.outbound("price_update")
    assert f"{new_all_in:.2f} EUR" in h.platform.sent[-1]["body"]
    assert h.platform.sent[-1]["to"] == shipment.customer

    await h.receive(ROUND_4_ACCEPT, id=5001)  # the customer confirms the new price
    assert h.shipment().state == ShipmentState.BOOKED and len(h.platform.bookings) == 1


@pytest.mark.anyio
async def test_portal_booking_closed_books_by_email_with_the_rate_id(tmp_path: Path) -> None:
    h = Harness(tmp_path, send_enabled=True)
    await h.receive(ROUND_4_RFQ)
    await h.truckers_reply()
    h.platform.booking_closed = True

    await h.receive(ROUND_4_ACCEPT)
    shipment = h.shipment()
    rate_id = shipment.chosen_option.rate_id  # the fresh rate from the price recheck
    assert shipment.state == ShipmentState.BOOKING and h.platform.bookings == []
    request = h.platform.sent[-1]
    assert request["to"] == "xenon-cargo-airlines@hackloop.mail"
    assert rate_id in request["body"] and "947" in request["body"]
    assert len(h.outbound("booking_request")) == 1


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


@pytest.mark.anyio
async def test_a_carrier_mail_never_opens_a_shipment_even_if_classified_as_rfq(tmp_path: Path) -> None:
    """ADR 16: the old classifier labelled trucker quotes "RFQ"; that must not start a shipment."""
    h = Harness(tmp_path, llm=recorded_llm({1063: "rfq"}), send_enabled=True)
    await h.receive(1063)  # Elbkante's trucker quote
    assert h.store.list_shipments() == [] and h.platform.sent == []


@pytest.mark.anyio
async def test_after_the_legacy_backfill_old_threads_trigger_nothing(tmp_path: Path) -> None:
    """Cutover: threads the old process handled are closed, so late replies do nothing."""
    from scripts.backfill_legacy import LEGACY_NOTE, backfill

    # Even if the classifier misreads a late reply as new details or a new request.
    h = Harness(tmp_path, llm=recorded_llm({ROUND_4_ACCEPT: "customer_info", 2204: "rfq"}), send_enabled=True)
    old_mails = [MAILS[i] for i in (ROUND_4_RFQ, *ROUND_4_TRUCKER_REPLIES)]
    assert backfill(h.store, old_mails, Reference(h.settings.start_kit_dir)) == (5, 1)
    assert backfill(h.store, old_mails, Reference(h.settings.start_kit_dir)) == (0, 0)  # idempotent
    assert h.store.get_since_id() == 1079
    assert (h.shipment().state, h.shipment().error) == (ShipmentState.FAILED, LEGACY_NOTE)

    await h.receive(ROUND_4_ACCEPT)  # accept of the OLD process's quote
    await h.receive(2204)  # "Thank you for confirming the booking..."
    assert h.platform.sent == [] and h.platform.bookings == [] and h.platform.rate_reads == []
    assert [s.id for s in h.store.list_shipments()] == [ROUND_4_RFQ]

    await h.receive(1638)  # a NEW request after cutover is handled normally
    assert h.shipment(1638) is not None and h.shipment(1638).error != LEGACY_NOTE


def _tracking_mail(mail_id: int = 8000, references: list[int] | None = None) -> MailboxMessage:
    return parse_mailbox_message({
        "id": mail_id, "from": "tracking@hackloop.mail", "references": references or [],
        "subject": "Status of our shipment 51896 to ORD, 679 kg",
        "body": "Hello, could you please tell me the current status of our shipment: Rheintal "
                "Elektronik GmbH, 51896 to ORD, 679 kg, ready on 2026-11-28? Where is it now?\nDeniz",
    })  # fmt: skip


@pytest.mark.anyio
async def test_tracking_question_is_answered_from_our_booking_record(tmp_path: Path) -> None:
    llm = recorded_llm()
    h = Harness(tmp_path, llm=llm, send_enabled=True)
    llm._answer = _as_category(llm._answer, 8000, "tracking_request")  # noqa: SLF001
    await h.receive(ROUND_4_RFQ)
    await h.truckers_reply()
    await h.receive(ROUND_4_ACCEPT)  # booked: BK-ERC5OOW3

    await h.receive(_tracking_mail())
    answer = h.platform.sent[-1]
    assert answer["to"] == "tracking@hackloop.mail" and answer["in_reply_to"] == 8000
    assert answer["subject"] == "Re: Status of our shipment 51896 to ORD, 679 kg"
    assert "BK-ERC5OOW3" in answer["body"]
    facts = llm.calls[-1][1]
    assert '"found": true' in facts and "CGN -> ORD" in facts and "Xenon Cargo Airlines" in facts
    assert '"never_booked"' not in facts and "booked with the airline" in facts and '"estimated_arrival": "2026-12-03"' in facts  # 29 h pickup: 2 days (ADR 52)
    assert h.platform.rate_reads and len(h.platform.bookings) == 1  # nothing new was looked up


@pytest.mark.anyio
async def test_tracking_for_an_unknown_shipment_asks_for_the_reference_at_most_three_times(
    tmp_path: Path,
) -> None:
    llm = recorded_llm()
    h = Harness(tmp_path, llm=llm, send_enabled=True)
    for mail_id, references in ((8000, []), (8001, [8000, 1]), (8002, [8000, 1, 8001, 2]),
                                (8003, [8000, 1, 8001, 2, 8002, 3])):  # fmt: skip
        llm._answer = _as_category(llm._answer, mail_id, "tracking_request")  # noqa: SLF001
        await h.receive(_tracking_mail(mail_id, references))
    assert len(h.outbound("tracking_answer")) == 3  # the 4th mail of the thread gets no reply
    composed = [system for schema, system, _ in llm.calls if schema == "ComposedMail"]
    assert len(composed) == 3 and '"found": false' in composed[-1]
    assert '"never_booked": true' in composed[-1]  # nothing booked: no tracking to give


@pytest.mark.anyio
async def test_tracking_for_a_quote_never_accepted_says_nothing_is_booked(tmp_path: Path) -> None:
    llm = recorded_llm()
    h = Harness(tmp_path, llm=llm, send_enabled=True)
    llm._answer = _as_category(llm._answer, 8000, "tracking_request")  # noqa: SLF001
    await h.receive(ROUND_4_RFQ)
    await h.truckers_reply()  # quoted, never accepted
    await h.receive(_tracking_mail())
    facts = llm.calls[-1][1]
    assert '"found": true' in facts and '"never_booked": true' in facts


def test_match_shipment_uses_destination_weight_then_date_and_origin(tmp_path: Path) -> None:
    from app.schemas import Shipment, ShipmentRequest

    def shipment(shipment_id, dest, kg, ready, postal=None, airport=None, ref=None):
        request = ShipmentRequest(destination_iata=dest, gross_kg=kg, ready_date=ready, pickup_postal_code=postal)
        return Shipment(id=shipment_id, customer="c", subject="s", request=request, origin_airport=airport, booking_ref=ref)

    older = shipment(1, "EZE", 676, "2026-11-20", airport="STR")
    wanted = shipment(2, "EZE", 676, "2026-11-26", postal="28261", airport="HAM", ref="BK-8Q49D0LR")
    other = shipment(3, "EZE", 1805, "2026-11-30", postal="72146")
    everything = [older, wanted, other]
    query = TrackingQuery(origin="28261", destination_iata="EZE", gross_kg=676, ready_date="2026-11-26")
    assert match_shipment(query, everything) is wanted
    assert match_shipment(TrackingQuery(destination_iata="EZE", gross_kg=1805.4), everything) is other
    assert match_shipment(TrackingQuery(destination_iata="SGN", gross_kg=676), everything) is None
    assert match_shipment(TrackingQuery(destination_iata="EZE"), everything) is None  # weight missing
    assert match_shipment(TrackingQuery(booking_ref="BK-8Q49D0LR"), everything) is wanted


@pytest.mark.anyio
async def test_backfill_closes_dry_run_shipments_so_cutover_cannot_book_them(tmp_path: Path) -> None:
    from scripts.backfill_legacy import LEGACY_NOTE, backfill

    h = Harness(tmp_path)  # dry run
    await h.receive(ROUND_4_RFQ)
    for reply_id in ROUND_4_TRUCKER_REPLIES:
        await h.receive(reply_id)
    assert h.shipment().state == ShipmentState.QUOTED and h.shipment().quote_message_id < 0

    assert backfill(h.store, [], Reference(h.settings.start_kit_dir)) == (0, 1)
    assert (h.shipment().state, h.shipment().error) == (ShipmentState.FAILED, LEGACY_NOTE)


@pytest.mark.anyio
async def test_an_image_attachment_is_read_by_one_vision_call_and_stored(tmp_path: Path) -> None:
    """ADR 43: a photo has no text; one vision call reads it when the mail comes in, and the
    extractor gets that text. The text is stored with the mail, so it is read only once."""
    vision_text = "Pickup postal code: 51465 Bergisch Gladbach"
    base = recorded_llm()._answer  # noqa: SLF001

    def answer(schema, system, user):
        if schema is AttachmentText:
            return AttachmentText(text=vision_text)
        return base(schema, system, user)

    h = Harness(tmp_path, llm=ScriptedLLM(answer))
    h.platform.files["7"] = b"\xff\xd8 a photo"
    rfq = 3940
    photo = MailboxAttachment(id=7, filename="label.jpg", content_type="image/jpeg", size=9)
    await h.receive(rfq, attachments=[photo])

    vision = [c for c in h.llm.calls if c[0] == "AttachmentText"]
    assert len(vision) == 1 and vision[0][2][1]["type"] == "image_url"
    extraction = next(c[2] for c in h.llm.calls if c[0] == "ShipmentRequest")
    assert f"ATTACHMENT label.jpg:\n{vision_text}" in extraction
    stored = h.store.get_message(rfq).attachments[0]
    assert stored.text == vision_text and stored.readable and stored.vision_tried


@pytest.mark.anyio
async def test_dangerous_goods_are_not_quoted_until_the_dg_documents_arrive(tmp_path: Path) -> None:
    """ADR 43 (mails 2267, 2540, 10142): DG goods get a request for the DG documents, no quote."""
    base = recorded_llm()._answer  # noqa: SLF001
    received = {"docs": False}

    def answer(schema, system, user):
        result = base(schema, system, user)
        if schema is ShipmentRequest:
            result.dangerous_goods = "UN3481, Class 9, lithium ion batteries"
            result.dg_documents_received = received["docs"]
        return result

    h = Harness(tmp_path, llm=ScriptedLLM(answer))
    await h.receive(ROUND_4_RFQ)
    assert h.shipment().state == ShipmentState.AWAITING_INFO
    assert h.platform.rate_reads == [] and not h.outbound("quote")
    facts = next(c[1] for c in h.llm.calls if "Mail to write: clarification" in c[1])
    assert "Shipper's Declaration for Dangerous Goods" in facts and "UN3481" in facts

    received["docs"] = True
    docs = MAILS[ROUND_4_RFQ].model_copy(update={"id": 4100, "references": [ROUND_4_RFQ]})
    h.llm._answer = _as_customer_info(h.llm._answer, docs.id)  # noqa: SLF001
    await h.receive(docs)
    assert h.shipment().state != ShipmentState.AWAITING_INFO
    assert h.platform.rate_reads  # now it is priced


@pytest.mark.anyio
async def test_a_readable_attachment_reaches_the_extractor_without_vision(tmp_path: Path) -> None:
    h = Harness(tmp_path)
    pdf = Path(__file__).parent / "fixtures/attachments/SLI_53351.pdf"
    h.platform.files["41"] = pdf.read_bytes()
    att = MailboxAttachment(id=41, filename="SLI_53351.pdf", content_type="application/pdf", size=2045)
    await h.receive(947, attachments=[att])
    extraction = next(c[2] for c in h.llm.calls if c[0] == "ShipmentRequest")
    assert "ATTACHMENT SLI_53351.pdf:\n" in extraction and "Total volume (cbm): 1.92" in extraction
    assert "AttachmentText" not in [c[0] for c in h.llm.calls]


@pytest.mark.anyio
async def test_a_preferred_airline_is_quoted_and_the_cheaper_one_is_named(tmp_path: Path) -> None:
    """ADR 46: the customer asks for AU (1920.10); Xenon (1599.41 all-in) is cheaper and is named."""
    base = recorded_llm()._answer  # noqa: SLF001

    def answer(schema, system, user):
        result = base(schema, system, user)
        if schema is ShipmentRequest:
            result.preferred_airline = "AU"
            result.excluded_carriers = ["MD"]
        return result

    h = Harness(tmp_path, llm=ScriptedLLM(answer), send_enabled=True)
    await h.receive(ROUND_4_RFQ)
    await h.truckers_reply()
    await h.wait(61)
    quote = h.shipment().quote
    assert quote["airline"] == "AU"
    facts = next(c[1] for c in reversed(h.llm.calls) if "Mail to write: quote" in c[1])
    alternative = facts.split('"cheaper_alternative": ')[1].split("}")[0]
    assert "Xenon Cargo Airlines" in alternative and "1599.41 EUR" in alternative  # air 1483.43 + truck


@pytest.mark.anyio
async def test_a_route_only_email_airlines_fly_is_priced_by_email_and_booked_by_email(
    tmp_path: Path,
) -> None:
    """ADR 47 (mail 10830, FRA-ULN failed silently): no portal or tariff price, so the four
    email-only airlines get a rate request; Zephyr answers; after 60 s it is quoted."""
    base = recorded_llm()._answer  # noqa: SLF001

    def answer(schema, system, user):
        if schema is AirlineRates:
            return AirlineRates(rates=[AirlineRate(reply=1, total_eur=2450.0, transit_days=4, quote_ref="ZS-1")])
        result = base(schema, system, user)
        if schema is ShipmentRequest:
            result.destination_iata = "ULN"
        return result

    h = Harness(tmp_path, llm=ScriptedLLM(answer), send_enabled=True)
    await h.receive(ROUND_4_RFQ)
    asked = {m["to"]: m["id"] for m in h.platform.sent if m["subject"].startswith("Rate request")}
    assert set(asked) == {
        "zephyr-steppe-cargo@hackloop.mail", "himalaya-bridge-air@hackloop.mail",
        "lagoon-isles-airways@hackloop.mail", "pampero-air-cargo@hackloop.mail",
    }
    assert h.shipment().state == ShipmentState.PRICING

    reply = MailboxMessage(
        id=95000, sender="zephyr-steppe-cargo@hackloop.mail", subject="Re: Rate request",
        body="Dear team, our all-in rate CGN-ULN is 2450.00 EUR, transit 4 days. Ref ZS-1.",
        in_reply_to=asked["zephyr-steppe-cargo@hackloop.mail"], created_at="2026-11-21T15:19:30+00:00",
    )  # fmt: skip
    await h.receive(reply)
    assert "ClassificationDecision" not in [c[0] for c in h.llm.calls[-1:]]  # no classifier call
    await h.wait(61)
    quote = h.shipment().quote
    assert h.shipment().state == ShipmentState.QUOTED
    assert (quote["airline"], quote["air_source"], quote["air_eur"]) == ("Zephyr Steppe Cargo", "email", 2450.0)

    accept = MAILS[ROUND_4_ACCEPT].model_copy(update={"in_reply_to": h.shipment().quote_message_id})
    await h.receive(accept)
    booking = [m for m in h.platform.sent if m["to"] == "zephyr-steppe-cargo@hackloop.mail"][-1]
    assert booking["subject"] == "booking_request"  # the scripted composer writes the kind


@pytest.mark.anyio
async def test_no_airline_price_at_all_tells_the_customer(tmp_path: Path) -> None:
    base = recorded_llm()._answer  # noqa: SLF001

    def answer(schema, system, user):
        result = base(schema, system, user)
        if schema is ShipmentRequest:
            result.destination_iata = "XXX"  # nobody flies there
        return result

    h = Harness(tmp_path, llm=ScriptedLLM(answer), send_enabled=True)
    await h.receive(ROUND_4_RFQ)
    assert h.shipment().state == ShipmentState.FAILED
    to_customer = [m for m in h.platform.sent if m["to"] == MAILS[ROUND_4_RFQ].sender]
    assert len(to_customer) == 1 and to_customer[0]["body"].startswith("no_price")


@pytest.mark.anyio
async def test_when_the_portal_is_down_the_airlines_of_the_destination_are_emailed(tmp_path: Path) -> None:
    """ADR 47: 503 "The rate portal is down. Contact the airline directly." -> rate requests by
    email to every non-tariff airline that serves ORD; no 60 s retry for that message."""
    h = Harness(tmp_path, send_enabled=True)
    original = h.platform.__call__

    def down(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v1/portal/rates":
            return httpx.Response(503, json={"detail": "The rate portal is down. Contact the airline directly."})
        return original(request)

    h.platform.__class__ = type("DownPlatform", (Platform,), {"__call__": lambda self, r: down(r)})
    await h.receive(ROUND_4_RFQ)
    asked = {m["to"] for m in h.platform.sent if m["subject"].startswith("Rate request")}
    ord_airlines = {c.email for c in h.workflow._reference.carriers if c.kind == "airline" and "ORD" in c.serves}  # noqa: SLF001
    assert asked and asked <= ord_airlines
    assert h.shipment().state == ShipmentState.PRICING


@pytest.mark.anyio
async def test_a_429_on_the_poll_waits_its_retry_after_and_the_timers_still_run(tmp_path: Path) -> None:
    h = Harness(tmp_path, send_enabled=True)
    await h.receive(ROUND_4_RFQ)
    assert h.shipment().state == ShipmentState.PRICING
    original = h.platform.__call__

    def limited(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v1/mailbox/messages" and request.method == "GET":
            return httpx.Response(429, headers={"Retry-After": "4"})
        return original(request)

    h.platform.__class__ = type("Limited", (Platform,), {"__call__": lambda self, r: limited(r)})
    h.now += timedelta(seconds=61)
    await h.poller._cycle()  # noqa: SLF001
    assert h.poller._backoff == 5.0 and "429" in h.poller.last_error  # noqa: SLF001
    assert h.shipment().state == ShipmentState.QUOTED  # the quote went out despite the 429
