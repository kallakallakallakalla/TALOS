"""Second LLM step: one focused extractor per mail type that carries data (ADR 15).

Each extractor has one prompt and one small output schema. Mail text is never parsed with
regex (ADR 12); the only plain Python here is `missing_fields`.
"""

import json

from langfuse import observe

from app import prompts
from app.config import Settings
from app.llm import StructuredLLM
from app.schemas import BookingConfirmation, MailboxMessage, ShipmentRequest, TruckerQuote


def mail_text(msg: MailboxMessage) -> str:
    """A mail as the LLM sees it."""
    return f"From: {msg.sender}\nSubject: {msg.subject}\n\n{msg.body.strip()}"


def missing_fields(request: ShipmentRequest) -> list[str]:
    """What we still need before a request can be priced, as phrases the composer can ask for.

    Required: destination, weight, volume, and either a pickup postal code or a departure
    airport. A missing goal is not asked for; it defaults to cheapest (ADR 08).
    """
    missing = []
    if not request.destination_iata:
        missing.append("destination airport")
    if not request.gross_kg:
        missing.append("total gross weight in kg")
    if not request.volume_cbm:
        missing.append("total volume in cbm (or the dimensions of each piece)")
    if not request.pickup_postal_code and not request.origin_airport:
        missing.append(
            "pickup address with postal code, or the German airport you deliver the goods to"
        )
    return missing


class Extractors:
    """Turns a classified mail into structured data."""

    def __init__(self, settings: Settings, llm: StructuredLLM) -> None:
        self._settings = settings
        self._llm = llm

    async def _parse(self, prompt_name: str, schema, user_content: str, label: str | None, **variables):
        return await self._llm.parse(
            prompt_name,
            schema,
            user_content,
            model=self._settings.extract_model,
            reasoning_effort=self._settings.extract_reasoning_effort,
            prompt_label=label,
            **variables,
        )

    @observe(name="extract.rfq")
    async def extract_rfq(
        self,
        msg: MailboxMessage,
        previous: ShipmentRequest | None = None,
        thread: str = "",
        prompt_label: str | None = None,
    ) -> ShipmentRequest:
        """Read a quote request. `previous` is what we already know when the customer adds
        details; `thread` is our question they are answering."""
        content = mail_text(msg)
        if thread:
            content = f"EARLIER IN THIS THREAD (our mail):\n{thread}\n\nTHE CUSTOMER'S MAIL:\n{content}"
        received = msg.created_at or ""
        return await self._parse(
            prompts.EXTRACT_RFQ,
            ShipmentRequest,
            content,
            prompt_label,
            today=received[:10],
            received_at=received,
            previous_json=previous.model_dump_json(exclude_defaults=True) if previous else "",
        )

    @observe(name="extract.trucker_quote")
    async def extract_trucker_quote(
        self, msg: MailboxMessage, our_request: str = "", prompt_label: str | None = None
    ) -> TruckerQuote:
        """Read a trucker's pickup offer."""
        return await self._parse(
            prompts.EXTRACT_TRUCKER_QUOTE, TruckerQuote, mail_text(msg), prompt_label,
            our_request=our_request,
        )  # fmt: skip

    @observe(name="extract.booking")
    async def extract_booking(
        self, msg: MailboxMessage, prompt_label: str | None = None
    ) -> BookingConfirmation:
        """Read an airline's booking confirmation."""
        return await self._parse(
            prompts.EXTRACT_BOOKING, BookingConfirmation, mail_text(msg), prompt_label
        )


def request_summary(request: ShipmentRequest) -> str:
    """Compact JSON of a request, for prompts and logs."""
    return json.dumps(request.model_dump(mode="json", exclude_defaults=True))
