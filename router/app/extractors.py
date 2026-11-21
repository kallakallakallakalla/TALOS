"""Second LLM step: one focused extractor per mail type that carries data (ADR 15).

Each extractor has one prompt and one small output schema. Mail text is never parsed with
regex (ADR 12); the only plain Python here is `missing_fields`.
"""

from datetime import date

from langfuse import observe

from app import attachments, pricing, prompts
from app.config import Settings
from app.llm import StructuredLLM
from app.schemas import (
    AttachmentText,
    BookingConfirmation,
    MailboxMessage,
    ShipmentRequest,
    TrackingQuery,
    TruckerQuotes,
)


def mail_text(msg: MailboxMessage) -> str:
    """A mail as the LLM sees it, with its attachments' text (ADR 42)."""
    text = f"From: {msg.sender}\nSubject: {msg.subject}\n\n{msg.body.strip()}"
    files = attachments.as_text(msg)
    return f"{text}\n\n{files}" if files else text


# What we must know before quoting special cargo (ADR 45).
SPECIAL_CARGO_DETAILS = {
    "live_animals": "for the live animals: species and number of animals, crate dimensions and "
    "weight with the animals inside, and whether the crates meet the IATA Live Animals Regulations",
    "perishables": "for the perishable goods: the required temperature range and the shelf life",
    "temperature_controlled": "for the temperature-controlled goods: the required temperature "
    "range and whether active cooling is needed",
    "valuables": "for the valuable goods: the declared value in EUR",
    "human_remains": "for the human remains: the death certificate and the funeral home "
    "handling the shipment at destination",
}


def missing_fields(request: ShipmentRequest, today: date | None = None) -> list[str]:
    """What we still need before a request can be priced, as phrases the composer can ask for.

    Required: destination, weight, volume, and either a pickup postal code or a departure
    airport; for dangerous goods also the DG documents (ADR 43). A missing goal is not asked for; it defaults to cheapest (ADR 08).
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
    if today:
        missing += pricing.date_problems(request.ready_date, request.deliver_by, today)
    if request.special_cargo and not request.special_cargo_details_received:
        missing.append(f"the handling details {SPECIAL_CARGO_DETAILS[request.special_cargo]}")
    if request.dangerous_goods and not request.dg_documents_received:
        missing.append(
            f"the dangerous goods documents for {request.dangerous_goods}: the Shipper's "
            "Declaration for Dangerous Goods and the safety data sheet (MSDS)"
        )
    return missing


class Extractors:
    """Turns a classified mail into structured data."""

    def __init__(self, settings: Settings, llm: StructuredLLM) -> None:
        self._settings = settings
        self._llm = llm

    async def _parse(self, prompt_name: str, schema, user_content: str | list, label: str | None, **variables):
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

    @observe(name="extract.attachment_vision")
    async def read_attachment(self, filename: str, parts: list[dict], prompt_label: str | None = None) -> str:
        """One vision call: what a file shows that text extraction cannot read (ADR 43)."""
        content = [{"type": "text", "text": f"ATTACHMENT {filename}"}, *parts]
        result = await self._parse(prompts.READ_ATTACHMENT, AttachmentText, content, prompt_label)
        return result.text

    @observe(name="extract.trucker_quotes")
    async def extract_trucker_quotes(
        self, replies: list[str], prompt_label: str | None = None
    ) -> TruckerQuotes:
        """Read all trucker replies to one pickup request in a single call.

        `replies` are the mail bodies; reply n in the result refers to `replies[n - 1]`.
        Only the bodies are sent (no headers, no copy of our request) to keep the call small.
        """
        content = "\n\n".join(
            f"REPLY {number}:\n{body.strip()}" for number, body in enumerate(replies, start=1)
        )
        return await self._llm.parse(
            prompts.EXTRACT_TRUCKER_QUOTES,
            TruckerQuotes,
            content,
            model=self._settings.extract_model,
            reasoning_effort=self._settings.trucker_reasoning_effort,
            prompt_label=prompt_label,
        )

    @observe(name="extract.booking")
    async def extract_booking(
        self, msg: MailboxMessage, prompt_label: str | None = None
    ) -> BookingConfirmation:
        """Read an airline's booking confirmation."""
        return await self._parse(
            prompts.EXTRACT_BOOKING, BookingConfirmation, mail_text(msg), prompt_label
        )

    @observe(name="extract.tracking")
    async def extract_tracking(
        self, msg: MailboxMessage, prompt_label: str | None = None
    ) -> TrackingQuery:
        """Read which shipment a tracking question is about."""
        return await self._parse(
            prompts.EXTRACT_TRACKING, TrackingQuery, mail_text(msg), prompt_label
        )
