"""The quote workflow: a shipment state machine driven by classified mails and a timer.

    NEW -> AWAITING_INFO -> NEW -> PRICING -> QUOTED -> BOOKING -> BOOKED -> CONFIRMED
                                      |          |          |
                                    FAILED    REJECTED    FAILED

`handle()` reacts to one classified mail, `tick()` to the passing of time (trucker deadlines,
booking reminders). Every loop is bounded (docs/workflow.md section 5). This module decides
what happens and who gets which mail; LLMs only read mail (extractors) and write mail
(composer). With `send_enabled=False` nothing is sent and nothing is booked.
"""

import asyncio
import logging
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

from langfuse import get_client, observe, propagate_attributes

from app import attachments, pricing, templates
from app.composer import Composer
from app.config import Settings
from app.extractors import Extractors, mail_text, missing_fields
from app.mailbox_client import MailboxClient
from app.portal_client import PortalClient, PortalError, RateExpired
from app.reference import Reference, zone_of
from app.schemas import (
    AirOption,
    ClassificationDecision,
    EmailCategory,
    MailboxMessage,
    MailContext,
    Shipment,
    ShipmentRequest,
    ShipmentState,
    TrackingQuery,
    TruckerQuote,
    TruckerRequest,
)
from app.storage import MessageStore
from app.tariffs import Tariffs

logger = logging.getLogger(__name__)

DRY_RUN_BOOKING_REF = "DRY-RUN"
_THREAD_MAX_CHARS = 2000


def _kg(value: float | None) -> str:
    return f"{round(value or 0, 2):g} kg"


def _cbm(value: float | None) -> str:
    return f"{round(value or 0, 2):g} cbm"


def _eur(value: float) -> str:
    return f"{value:.2f} EUR"


# What we tell a customer who asks where the shipment is, per state. Our own records only.
TRACKING_STATUS = {
    ShipmentState.CONFIRMED: "booked and confirmed by the airline, ready to ship",
    ShipmentState.BOOKED: "booked with the airline; the airline's written confirmation is on its way",
    ShipmentState.BOOKING: "the booking with the airline is in progress",
    ShipmentState.QUOTED: "quoted and waiting for your go-ahead; not booked yet",
    ShipmentState.PRICING: "we are preparing your quote; not booked yet",
    ShipmentState.AWAITING_INFO: "we are waiting for the details we asked you for; not booked yet",
    ShipmentState.NEW: "we are preparing your quote; not booked yet",
    ShipmentState.REJECTED: "our quote was declined, so nothing is booked",
    ShipmentState.FAILED: "not booked",
}
_TRACKING_KG_TOLERANCE = 1.0


def match_shipment(query: TrackingQuery, shipments: list[Shipment]) -> Shipment | None:
    """Find the shipment a tracking question is about.

    Tracking mails come from the tracking desk, unthreaded, so the sender and thread say
    nothing. A stated booking reference wins. Otherwise destination and gross weight must
    match; among several, the one with the same ready date and origin, then the newest.
    """
    if query.booking_ref:
        by_ref = [s for s in shipments if s.booking_ref == query.booking_ref]
        if by_ref:
            return by_ref[0]
    if not query.destination_iata or query.gross_kg is None:
        return None
    candidates = [
        s
        for s in shipments
        if s.request.destination_iata == query.destination_iata
        and s.request.gross_kg is not None
        and abs(s.request.gross_kg - query.gross_kg) <= _TRACKING_KG_TOLERANCE
    ]

    def closeness(s: Shipment) -> tuple[bool, bool, int]:
        same_origin = query.origin in (s.request.pickup_postal_code, s.origin_airport)
        return (s.request.ready_date == query.ready_date, bool(query.origin) and same_origin, s.id)

    return max(candidates, key=closeness, default=None)


def _price_in_text(price: float | None, text: str) -> bool:
    """Grounding check: an extracted price must stand in the mail as written (no price: fine)."""
    if price is None:
        return True
    forms = {f"{price:.2f}", f"{price:g}"}
    return any(form in text or form.replace(".", ",") in text for form in forms)


def needs_pickup(shipment: Shipment) -> bool:
    """We collect unless the customer brings the goods to a named airport themselves."""
    request = shipment.request
    return bool(request.pickup_postal_code) and not (
        request.delivers_to_airport and request.origin_airport
    )


class Workflow:
    """Owns every shipment from the first mail to the booking."""

    def __init__(
        self,
        settings: Settings,
        store: MessageStore,
        mailbox: MailboxClient,
        portal: PortalClient,
        reference: Reference,
        tariffs: Tariffs,
        extractors: Extractors,
        composer: Composer,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self._settings = settings
        self._store = store
        self._mailbox = mailbox
        self._portal = portal
        self._reference = reference
        self._tariffs = tariffs
        self._extractors = extractors
        self._composer = composer
        self._now = clock
        self._handlers = {
            EmailCategory.RFQ: self._on_rfq,
            EmailCategory.CUSTOMER_INFO: self._on_customer_info,
            EmailCategory.TRUCKER_QUOTE: self._on_trucker_reply,
            EmailCategory.TRUCKER_DECLINE: self._on_trucker_reply,
            EmailCategory.BOOKING_CONFIRMATION: self._on_booking_confirmation,
            EmailCategory.CUSTOMER_ACCEPT: self._on_customer_accept,
            EmailCategory.CUSTOMER_REJECT: self._on_customer_reject,
            EmailCategory.CUSTOMER_QUESTION: self._on_customer_question,
            EmailCategory.TRACKING_REQUEST: self._on_tracking_request,
            # rate_offer, tariff_update and other have no handler:
            # they are stored and tagged, and never trigger a mail (ADR 19).
        }

    # --- Entry points ---------------------------------------------------------------------

    async def context(self, msg: MailboxMessage) -> MailContext:
        """Who sent this and which shipment it belongs to. API metadata only, no LLM (ADR 16)."""
        role = self._reference.role_for_email(msg.sender)
        shipment = trucker_request = None
        if role == "trucker":
            trucker_request = self._store.find_trucker_request(
                msg.in_reply_to,
                (msg.sender or "").lower(),
                # Anything from a trucker we asked counts as their answer (ADR 41).
                match_by_sender=True,
            )
            if trucker_request:
                shipment = self._store.get_shipment(trucker_request.shipment_id)
        elif role == "customer":
            # references[0] is the customer's first mail, which is the shipment id.
            root = msg.references[0] if msg.references else msg.in_reply_to
            shipment = self._store.get_shipment(root) if root else None
        return MailContext(
            sender_role=role,
            shipment=shipment,
            trucker_request=trucker_request,
            our_mail=await self._our_mail(msg.in_reply_to),
        )

    async def handle(
        self, msg: MailboxMessage, decision: ClassificationDecision, ctx: MailContext
    ) -> None:
        """Act on one classified mail. Errors are stored on the shipment, never raised."""
        handler = self._handlers.get(decision.category)
        if handler is None:
            return
        try:
            await handler(msg, ctx)
        except Exception as exc:  # noqa: BLE001 - one mail must never stop the poller
            logger.exception("Workflow failed on mail id=%s (%s)", msg.id, decision.category.value)
            if ctx.shipment and (shipment := self._store.get_shipment(ctx.shipment.id)):
                shipment.error = f"mail {msg.id}: {exc}"[:500]
                self._store.save_shipment(shipment)

    async def tick(self) -> None:
        """Timer work: quote shipments whose trucker wait is over, follow up open bookings."""
        waiting = self._store.list_shipments(ShipmentState.PRICING)
        booking = self._store.list_shipments(ShipmentState.BOOKING)
        for shipment, step in [(s, self._maybe_quote) for s in waiting] + [
            (s, self._booking_followup) for s in booking
        ]:
            try:
                with propagate_attributes(session_id=f"shipment-{shipment.id}", tags=["tick"]):
                    await step(shipment)
            except Exception as exc:  # noqa: BLE001 - one shipment must not block the others
                logger.exception("Tick failed for shipment %s", shipment.id)
                shipment.error = f"tick: {exc}"[:500]
                self._store.save_shipment(shipment)

    # --- Customer request -----------------------------------------------------------------

    async def _on_rfq(self, msg: MailboxMessage, ctx: MailContext) -> None:
        if ctx.sender_role != "customer":
            return  # a carrier's mail never opens a shipment, whatever the classifier says
        if ctx.shipment is not None:
            # A second "request" in a known thread adds details to the same shipment.
            await self._on_customer_info(msg, ctx)
            return
        request = await self._extractors.extract_rfq(msg)
        shipment = Shipment(
            id=msg.references[0] if msg.references else msg.id,
            customer=msg.sender or "",
            subject=msg.subject,
            language=request.language,
            request=request,
            goal=request.goal,
        )
        self._store.save_shipment(shipment)
        await self._advance(shipment, reply_to=msg.id)

    async def _on_customer_info(self, msg: MailboxMessage, ctx: MailContext) -> None:
        shipment = ctx.shipment
        if shipment is None:
            await self._on_rfq(msg, ctx)
            return
        if shipment.state not in (ShipmentState.NEW, ShipmentState.AWAITING_INFO):
            logger.info("Shipment %s is %s; extra details ignored", shipment.id, shipment.state)
            return
        shipment.request = await self._extractors.extract_rfq(
            msg, previous=shipment.request, thread=ctx.our_mail
        )
        shipment.goal = shipment.request.goal
        shipment.state = ShipmentState.NEW
        self._store.save_shipment(shipment)
        await self._advance(shipment, reply_to=msg.id)

    async def read_attachments(self, msg: MailboxMessage) -> None:
        """Text (and, where needed, one vision call) for each customer attachment (ADR 42/43)."""
        await attachments.enrich(msg, self._mailbox, self._extractors.read_attachment)

    async def _advance(self, shipment: Shipment, reply_to: int) -> None:
        """NEW -> PRICING if the request is complete, else ask (max 3 times) or fail."""
        request = shipment.request
        missing = missing_fields(request)
        get_client().score_current_trace(name="extraction_complete", value=0 if missing else 1)

        zone = airport = None
        if not missing:
            if needs_pickup(shipment):
                zone = zone_of(request.pickup_postal_code)
                airport = self._reference.airport_for_zone(zone)
                if airport is None:
                    zone = None
                    missing.append(
                        "a German airport you can deliver the goods to (we cannot arrange a "
                        f"pickup from postal code {request.pickup_postal_code})"
                    )
            else:
                airport = request.origin_airport

        if missing:
            await self._clarify(shipment, missing, reply_to)
            return
        shipment.zone, shipment.origin_airport = zone, airport
        await self._start_pricing(shipment)

    async def _clarify(self, shipment: Shipment, missing: list[str], reply_to: int) -> None:
        if shipment.clarifications >= self._settings.max_clarifications:
            shipment.state = ShipmentState.FAILED
            shipment.error = f"still incomplete after {shipment.clarifications} questions: {missing}"
            self._store.save_shipment(shipment)
            return
        facts = {"missing_fields": missing, "what_we_have": self._cargo_facts(shipment), "must_appear": []}
        await self._compose_and_send(
            "clarification", facts, shipment, to=shipment.customer, in_reply_to=reply_to
        )
        shipment.clarifications += 1
        shipment.state = ShipmentState.AWAITING_INFO
        self._store.save_shipment(shipment)

    # --- Pricing --------------------------------------------------------------------------

    async def _start_pricing(self, shipment: Shipment) -> None:
        """Ask the zone's truckers and price the air leg at the same time (response time)."""
        truckers = self._reference.truckers_for_zone(shipment.zone) if shipment.zone else []
        _, options = await asyncio.gather(
            self._request_truckers(shipment, [t.email for t in truckers]),
            self._air_options(shipment),
        )
        self._store.replace_air_options(shipment.id, options)
        if not options:
            shipment.state = ShipmentState.FAILED
            shipment.error = (
                f"no airline prices {shipment.origin_airport}-{shipment.request.destination_iata}"
            )
            self._store.save_shipment(shipment)
            return
        shipment.state = ShipmentState.PRICING
        shipment.pricing_started_at = self._now().isoformat()
        self._store.save_shipment(shipment)
        await self._maybe_quote(shipment)

    async def _request_truckers(self, shipment: Shipment, trucker_emails: list[str]) -> None:
        """The same fixed request to each trucker of the zone; each is asked once (ADR 05).

        A template, not the LLM (ADR 38): the readers are carriers who need the numbers,
        and skipping the call gets the request out a second or two earlier.
        """
        if not trucker_emails:
            return
        request = shipment.request
        facts = {
            **self._cargo_facts(shipment),
            "pickup_postal_code": request.pickup_postal_code,
            "pickup_city": request.pickup_city,
            "airport": shipment.origin_airport,
        }
        mail = templates.render("trucker_request", facts, self._settings.mail_signature)
        for email in trucker_emails:
            out_id = await self._send("trucker_request", email, mail, shipment.id, None, True)
            self._store.save_trucker_request(
                TruckerRequest(
                    out_message_id=out_id,
                    shipment_id=shipment.id,
                    trucker=email,
                    sent_at=self._now().isoformat(),
                )
            )

    @observe(name="air.options")
    async def _air_options(self, shipment: Shipment) -> list[AirOption]:
        """Portal rates and valid local tariff lanes are equal price sources (ADR 06)."""
        request = shipment.request
        origin, destination = shipment.origin_airport, request.destination_iata
        try:
            options = await self._portal.portal_options(
                origin, destination, request.gross_kg, request.volume_cbm
            )
        except Exception as exc:  # noqa: BLE001 - tariffs can still price the lane
            logger.warning("Portal unavailable for %s-%s: %s", origin, destination, exc)
            options = []
        options += [
            pricing.tariff_option(lane, request.gross_kg, request.volume_cbm)
            for lane in self._tariffs.lanes(origin, destination, self._now().date())
        ]
        return options

    async def _maybe_quote(self, shipment: Shipment) -> None:
        """PRICING -> QUOTED once every asked trucker answered, or after the wait (60 s)."""
        requests = self._store.trucker_requests(shipment.id)
        if shipment.zone:
            elapsed = (
                self._now() - datetime.fromisoformat(shipment.pricing_started_at)
            ).total_seconds()
            if not all(r.answered for r in requests) and elapsed < self._settings.trucker_wait_seconds:
                return
            requests = await self._read_trucker_replies(requests)
        await self._quote(shipment, requests)

    async def _read_trucker_replies(self, requests: list[TruckerRequest]) -> list[TruckerRequest]:
        """Read every reply that came back in ONE LLM call and store the offers (ADR 41).

        A price is only accepted if that exact number stands in that trucker's mail; one
        retry, then the offer is dropped. If the call fails, no offer is used and the quote
        falls back to the estimated trucker price, so the customer still gets an answer.
        """
        unread = [r for r in requests if r.reply_message_id and r.price_eur is None]
        bodies = [(m.body if (m := self._store.get_message(r.reply_message_id)) else "") for r in unread]
        if not unread:
            return requests
        offers: dict[int, TruckerQuote] = {}
        for _attempt in range(2):
            try:
                result = await self._extractors.extract_trucker_quotes(bodies)
            except Exception as exc:  # noqa: BLE001 - quote with the fallback price instead
                logger.warning("Reading trucker replies failed: %s", exc)
                continue
            read = {q.reply: q for q in result.quotes if 1 <= q.reply <= len(unread)}
            ungrounded = [n for n, q in read.items() if not _price_in_text(q.price_eur, bodies[n - 1])]
            offers = {n: q for n, q in read.items() if n not in ungrounded}
            if not ungrounded:
                break
            logger.warning("Trucker prices not found in their mails (replies %s)", ungrounded)
        for number, request in enumerate(unread, start=1):
            if offer := offers.get(number):
                request.price_eur = offer.price_eur
                request.pickup_hours = offer.pickup_hours
                request.valid_until = offer.valid_until
                request.ref = offer.quote_ref
                self._store.save_trucker_request(request)
        return requests

    @observe(name="pricing.pick_best")
    def _price(self, shipment: Shipment, requests: list[TruckerRequest]) -> dict[str, Any]:
        """Choose airline and trucker for the goal and compute the all-in price."""
        request = shipment.request
        air, truck = pricing.pick_best(self._store.air_options(shipment.id), requests, shipment.goal)
        truck_eur, truck_fallback = 0.0, False
        if shipment.zone:
            if truck:
                truck_eur = truck.price_eur
            else:
                truck_fallback = True
                truck_eur = pricing.fallback_truck(
                    request.gross_kg,
                    shipment.zone,
                    self._store.trucker_quote_history(),
                    self._settings.fallback_truck_eur_per_kg,
                )
        dates = pricing.eta(
            request.ready_date or self._now().date(),
            truck.pickup_hours if truck else None,
            air.transit_days,
        )
        return {
            "all_in_eur": pricing.all_in(air.total_eur, truck_eur, self._settings.margin_eur),
            "air_eur": air.total_eur,
            "airline": air.airline,
            "air_source": air.source,
            "billed_kg": air.billed_kg,
            "transit_days": air.transit_days,
            "truck_eur": truck_eur,
            "trucker": truck.trucker if truck else None,
            "trucker_ref": truck.ref if truck else None,
            "truck_fallback": truck_fallback,
            "pickup_hours": truck.pickup_hours if truck else None,
            **{f"estimated_{name}": day.isoformat() for name, day in dates.items()},
            "chosen_option": air,
        }

    async def _quote(self, shipment: Shipment, requests: list[TruckerRequest]) -> None:
        if shipment.quote_message_id is not None:
            return  # only the first quote in a thread counts (ADR 19)
        quote = self._price(shipment, requests)
        air: AirOption = quote.pop("chosen_option")
        request = shipment.request
        price = _eur(quote["all_in_eur"])
        valid_until = self._quote_validity(air, requests, quote["trucker"])
        facts = {
            "route": f"{air.origin} -> {air.destination}",
            "airline": air.airline,
            "transit_days": air.transit_days,
            "all_in_price": price,
            "price_includes": (
                "pickup at your site and trucking to the airport, air freight, fuel and "
                "security surcharges"
                if shipment.zone
                else "air freight, fuel and security surcharges (you deliver to the airport)"
            ),
            "pickup_included": bool(shipment.zone),
            "pickup_from": f"{request.pickup_postal_code} {request.pickup_city or ''}".strip()
            if shipment.zone
            else None,
            **self._cargo_facts(shipment),
            "chargeable_weight": _kg(air.billed_kg),
            "estimated_pickup": quote["estimated_pickup"] if shipment.zone else None,
            "estimated_departure": quote["estimated_departure"],
            "estimated_arrival": quote["estimated_arrival"],
            "extra_questions": request.extra_questions,
            "valid_until": valid_until,
            "must_appear": [price],
        }
        out_id = await self._compose_and_send(
            "quote", facts, shipment, to=shipment.customer, in_reply_to=shipment.id
        )
        shipment.quote = {**quote, "valid_until": valid_until}
        shipment.chosen_option = air
        shipment.quote_message_id = out_id
        shipment.state = ShipmentState.QUOTED
        self._store.save_shipment(shipment)
        logger.info("Quoted shipment %s: %s (%s)", shipment.id, price, air.airline)

    @staticmethod
    def _quote_validity(air: AirOption, requests: list[TruckerRequest], trucker: str | None) -> str:
        """The quote holds as long as its shortest-lived part (rate id or trucker offer)."""
        limits = [air.valid_until] if air.source == "portal" else []
        limits += [r.valid_until for r in requests if r.trucker == trucker and r.valid_until]
        instants = [datetime.fromisoformat(limit) for limit in limits if limit]
        if not instants:
            return air.valid_until or ""
        return min(i if i.tzinfo else i.replace(tzinfo=UTC) for i in instants).strftime(
            "%Y-%m-%d %H:%M UTC"
        )

    # --- Trucker replies ------------------------------------------------------------------

    def awaited_trucker_reply(self, ctx: MailContext) -> bool:
        """True for a mail from a trucker we asked and who has not answered yet.

        Such a mail needs no classifier call: it is their answer, whatever it says. What it
        says is read later, together with the other replies.
        """
        return ctx.trucker_request is not None and not ctx.trucker_request.answered

    async def _on_trucker_reply(self, msg: MailboxMessage, ctx: MailContext) -> None:
        """Note that this trucker answered; quote at once if they were the last one.

        No LLM call here, and no mail ever goes back to a carrier because of a carrier's
        reply (ADR 19).
        """
        request = ctx.trucker_request
        if request is None or request.answered:
            return  # not an answer to one of our requests, or this trucker already answered
        request.answered = True
        request.reply_message_id = msg.id
        self._store.save_trucker_request(request)
        shipment = self._store.get_shipment(request.shipment_id)
        if shipment and shipment.state == ShipmentState.PRICING:
            await self._maybe_quote(shipment)

    # --- Customer verdict -----------------------------------------------------------------

    async def _on_customer_accept(self, msg: MailboxMessage, ctx: MailContext) -> None:
        shipment = ctx.shipment
        if shipment is None or shipment.state != ShipmentState.QUOTED:
            return  # nothing open to accept; also stops "thanks for booking" loops
        self._verdict(shipment, 1)
        shipment.state = ShipmentState.BOOKING
        shipment.quote = {**(shipment.quote or {}), "accept_message_id": msg.id}
        self._store.save_shipment(shipment)
        await self._book(shipment)

    async def _on_customer_reject(self, msg: MailboxMessage, ctx: MailContext) -> None:
        shipment = ctx.shipment
        if shipment is None or shipment.state != ShipmentState.QUOTED:
            return
        self._verdict(shipment, 0)
        shipment.state = ShipmentState.REJECTED
        shipment.quote = {**(shipment.quote or {}), "reject_message_id": msg.id}
        self._store.save_shipment(shipment)

    @staticmethod
    def _verdict(shipment: Shipment, value: int) -> None:
        get_client().create_score(
            name="customer_verdict", value=value, session_id=f"shipment-{shipment.id}"
        )

    async def _on_customer_question(self, msg: MailboxMessage, ctx: MailContext) -> None:
        shipment = ctx.shipment
        if shipment is None or shipment.answers_sent >= self._settings.max_answers:
            return
        facts = {
            "question": mail_text(msg),
            "shipment_state": shipment.state.value,
            **self._cargo_facts(shipment),
            "quote": shipment.quote,
            "booking_reference": shipment.booking_ref,
            "must_appear": [],
        }
        await self._compose_and_send(
            "answer", facts, shipment, to=shipment.customer, in_reply_to=msg.id
        )
        shipment.answers_sent += 1
        self._store.save_shipment(shipment)

    # --- Tracking -------------------------------------------------------------------------

    async def _on_tracking_request(self, msg: MailboxMessage, ctx: MailContext) -> None:
        """Answer "where is my shipment" from our own records, in the thread it came in.

        No carrier is asked. Each tracking mail gets one answer; a thread that keeps coming
        back stops after `max_answers` of ours (every second mail in `references` is ours).
        """
        if len(msg.references) // 2 >= self._settings.max_answers:
            return
        query = await self._extractors.extract_tracking(msg)
        shipment = match_shipment(query, self._store.list_shipments())
        if shipment is None:
            facts: dict[str, Any] = {
                "found": False,
                "asked_about": query.model_dump(mode="json", exclude_none=True),
                "must_appear": [],
            }
        else:
            quote, option = shipment.quote or {}, shipment.chosen_option
            booked = shipment.state in (ShipmentState.BOOKED, ShipmentState.CONFIRMED)
            facts = {
                "found": True,
                "status": TRACKING_STATUS[shipment.state],
                "route": f"{shipment.origin_airport} -> {shipment.request.destination_iata}",
                "airline": option.airline if option else None,
                "booking_reference": shipment.booking_ref,
                "booked_at": self._minute(shipment.last_booking_attempt_at) if booked else None,
                "transit_days": option.transit_days if option else None,
                "handover": (
                    "we collect the goods at your site"
                    if shipment.zone
                    else f"you deliver the goods to {shipment.origin_airport} yourselves; no pickup by us"
                ),
                "estimated_pickup": quote.get("estimated_pickup") if shipment.zone else None,
                "estimated_departure": quote.get("estimated_departure"),
                "estimated_arrival": quote.get("estimated_arrival"),
                **self._cargo_facts(shipment),
                "must_appear": [shipment.booking_ref] if shipment.booking_ref else [],
            }
        # Facts we do not have are left out, so the mail does not list them as "not available".
        facts = {key: value for key, value in facts.items() if value is not None}
        mail, used_template = await self._composer.compose(
            "tracking_answer",
            facts,
            recipient_role="customer",
            recipient_name=query.contact_name or "",
            language=query.language or "en",
            thread=mail_text(msg)[:_THREAD_MAX_CHARS],
            reply_to_subject=msg.subject,
        )
        await self._send(
            "tracking_answer",
            msg.sender,
            mail,
            shipment.id if shipment else None,
            msg.id,
            used_template,
        )

    @staticmethod
    def _minute(timestamp: str | None) -> str | None:
        """An ISO timestamp as "2026-11-21 16:39 UTC"."""
        if not timestamp:
            return None
        return datetime.fromisoformat(timestamp).astimezone(UTC).strftime("%Y-%m-%d %H:%M UTC")

    # --- Booking --------------------------------------------------------------------------

    async def _book(self, shipment: Shipment) -> None:
        """Book right away: a late booking does not count (HANDOFF backlog 6)."""
        if shipment.chosen_option.source == "portal":
            await self._book_on_portal(shipment)
        else:
            await self._request_booking_by_email(shipment)

    async def _book_on_portal(self, shipment: Shipment) -> None:
        """BOOKING -> BOOKED via the booking API; an expired rate is refetched (max 3 tries)."""
        booking_ref = None
        while booking_ref is None and shipment.booking_attempts < self._settings.max_booking_attempts:
            shipment.booking_attempts += 1
            shipment.last_booking_attempt_at = self._now().isoformat()
            self._store.save_shipment(shipment)
            option = shipment.chosen_option
            if not self._settings.send_enabled:
                booking_ref = DRY_RUN_BOOKING_REF
                break
            try:
                result = await self._portal.book(option.rate_id, str(shipment.id))
                booking_ref = result["booking_reference"]
            except RateExpired:
                shipment.chosen_option = await self._portal.rate(
                    option.origin, option.destination, option.airline_code,
                    option.weight_kg, option.volume_cbm,
                )  # fmt: skip
            except PortalError as exc:
                shipment.error = str(exc)[:500]
                self._store.save_shipment(shipment)
                return  # stays BOOKING; tick() tries again

        if booking_ref is None:
            await self._booking_failed(shipment)
            return
        shipment.booking_ref = booking_ref
        shipment.state = ShipmentState.BOOKED
        shipment.error = None
        self._store.save_shipment(shipment)
        await self._send_booking_note(shipment)

    async def _request_booking_by_email(self, shipment: Shipment) -> None:
        """Tariff airlines have no rate_id: ask the airline by mail; its confirmation books."""
        if shipment.booking_attempts >= self._settings.max_booking_attempts:
            await self._booking_failed(shipment)
            return
        option = shipment.chosen_option
        lane = f"{option.origin}-{option.destination}"
        facts = {
            **self._cargo_facts(shipment),
            "airline": option.airline,
            "lane": lane,
            "chargeable_weight": _kg(option.billed_kg),
            "tariff_total": _eur(option.total_eur),
            "our_reference": str(shipment.id),
            "is_reminder": shipment.booking_attempts > 0,
            "must_appear": [lane, str(shipment.id), _eur(option.total_eur)],
        }
        to = self._reference.airline_email(option.airline)
        if to is None:
            raise ValueError(f"no booking address for airline {option.airline}")
        mail, used_template = await self._composer.compose(
            "booking_request", facts, recipient_role="airline", language="en"
        )
        await self._send("booking_request", to, mail, shipment.id, None, used_template)
        shipment.booking_attempts += 1
        shipment.last_booking_attempt_at = self._now().isoformat()
        self._store.save_shipment(shipment)

    async def _booking_followup(self, shipment: Shipment) -> None:
        """BOOKING for too long: retry the portal, or remind the airline after 10 minutes."""
        if shipment.chosen_option.source == "portal":
            await self._book_on_portal(shipment)
            return
        waited = (
            self._now() - datetime.fromisoformat(shipment.last_booking_attempt_at)
        ).total_seconds()
        if waited >= self._settings.booking_reminder_seconds:
            await self._request_booking_by_email(shipment)

    async def _booking_failed(self, shipment: Shipment) -> None:
        shipment.state = ShipmentState.FAILED
        shipment.error = f"not booked after {shipment.booking_attempts} attempts"
        self._store.save_shipment(shipment)

    async def _on_booking_confirmation(self, msg: MailboxMessage, ctx: MailContext) -> None:
        """Confirmations arrive unthreaded; "Your reference" is the shipment id we booked with."""
        confirmation = await self._extractors.extract_booking(msg)
        reference = (confirmation.our_reference or "").strip()
        shipment = self._store.get_shipment(int(reference)) if reference.isdigit() else None
        if shipment is None or shipment.state not in (ShipmentState.BOOKING, ShipmentState.BOOKED):
            logger.info("Booking confirmation %s matches no open booking", confirmation.booking_ref)
            return
        booked_by_email = shipment.state == ShipmentState.BOOKING
        shipment.booking_ref = confirmation.booking_ref or shipment.booking_ref
        shipment.state = ShipmentState.CONFIRMED
        self._store.save_shipment(shipment)
        if booked_by_email:
            await self._send_booking_note(shipment)

    async def _send_booking_note(self, shipment: Shipment) -> None:
        quote, option = shipment.quote or {}, shipment.chosen_option
        facts = {
            "booking_ref": shipment.booking_ref,
            "airline": option.airline,
            "route": f"{option.origin} -> {option.destination}",
            "all_in_price": _eur(quote["all_in_eur"]) if "all_in_eur" in quote else None,
            "ready_date": shipment.request.ready_date,
            "pickup_included": bool(shipment.zone),
            "estimated_departure": quote.get("estimated_departure"),
            "estimated_arrival": quote.get("estimated_arrival"),
            "must_appear": [shipment.booking_ref],
        }
        await self._compose_and_send(
            "booking_note", facts, shipment, to=shipment.customer,
            in_reply_to=quote.get("accept_message_id") or shipment.id,
        )  # fmt: skip

    # --- Mail helpers ---------------------------------------------------------------------

    @staticmethod
    def _cargo_facts(shipment: Shipment) -> dict[str, Any]:
        request = shipment.request
        return {
            "ready_date": request.ready_date.isoformat() if request.ready_date else None,
            "pieces": request.pieces,
            "gross_weight": _kg(request.gross_kg) if request.gross_kg else None,
            "volume": _cbm(request.volume_cbm) if request.volume_cbm else None,
            "commodity": request.commodity,
            "destination_airport": request.destination_iata,
        }

    async def _our_mail(self, message_id: int | None) -> str:
        """Text of the mail of ours that a reply answers ("" if unknown)."""
        if message_id is None:
            return ""
        mail = self._store.find_outbound(message_id)
        if mail is None:
            try:
                mail = await self._mailbox.get_message(message_id)
            except Exception as exc:  # noqa: BLE001 - context is helpful, not required
                logger.debug("Could not read mail %s: %s", message_id, exc)
                return ""
        return f"Subject: {mail.subject}\n\n{mail.body}"[:_THREAD_MAX_CHARS]

    async def _compose_and_send(
        self, kind: str, facts: dict[str, Any], shipment: Shipment, *, to: str, in_reply_to: int
    ) -> int:
        """Write a mail to the customer in their thread and language, and send it."""
        stored = self._store.get_message(in_reply_to)
        thread = f"Subject: {stored.subject}\n\n{stored.body}"[:_THREAD_MAX_CHARS] if stored else ""
        mail, used_template = await self._composer.compose(
            kind,
            facts,
            recipient_role="customer",
            recipient_name=shipment.request.contact_name or "",
            language=shipment.language or "en",
            thread=thread,
            reply_to_subject=shipment.subject,
        )
        return await self._send(kind, to, mail, shipment.id, in_reply_to, used_template)

    @observe(name="mail.send")
    async def _send(
        self,
        kind: str,
        to: str,
        mail,
        shipment_id: int | None,
        in_reply_to: int | None,
        used_template: bool,
    ) -> int:
        """Record the mail, and send it unless this is a dry run. Returns its mailbox id.

        A dry-run mail gets the negative id `-outbound.id`, which can never collide with a
        real mailbox id.
        """
        outbound = self._store.add_outbound(
            kind=kind, to=to, subject=mail.subject, body=mail.body,
            shipment_id=shipment_id, in_reply_to=in_reply_to, used_template=used_template,
        )  # fmt: skip
        if not self._settings.send_enabled:
            logger.info("DRY RUN, not sent: %s to %s (shipment %s)", kind, to, shipment_id)
            return -outbound.id
        sent = await self._mailbox.send(to, mail.subject, mail.body, in_reply_to)
        self._store.mark_outbound_sent(outbound.id, sent.id)
        return sent.id
