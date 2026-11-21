from datetime import date
from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field


class EmailCategory(StrEnum):
    """Mail types the classifier chooses from (docs/workflow.md section 3)."""

    RFQ = "rfq"
    CUSTOMER_INFO = "customer_info"
    CUSTOMER_ACCEPT = "customer_accept"
    CUSTOMER_REJECT = "customer_reject"
    CUSTOMER_QUESTION = "customer_question"
    TRUCKER_QUOTE = "trucker_quote"
    TRUCKER_DECLINE = "trucker_decline"
    BOOKING_CONFIRMATION = "booking_confirmation"
    RATE_OFFER = "rate_offer"
    TARIFF_UPDATE = "tariff_update"
    TRACKING_REQUEST = "tracking_request"
    OTHER = "other"

    @classmethod
    def _missing_(cls, value: object) -> "EmailCategory":
        # Rows stored by the pre-workflow router carry the six old labels ("Answer", "update"...).
        return cls.OTHER


class InputPayload(BaseModel):
    """Email input fetched from the upstream Input API (or placeholder)."""

    id: str = Field(..., description="Unique identifier of the email/input item")
    source: str = Field(default="placeholder", description="Origin of the input")
    text: str = Field(..., min_length=1, description="Email subject/body text to classify")
    metadata: dict[str, Any] = Field(default_factory=dict)


class ClassificationDecision(BaseModel):
    """Structured output returned by the gpt-6-luna decision API."""

    category: EmailCategory
    confidence: float = Field(..., ge=0.0, le=1.0)
    reasoning: str = Field(..., description="Brief explanation for the classification")


class ClassifyRequest(BaseModel):
    """Direct email classification request payload."""

    id: str = Field(default="direct-email")
    source: str = Field(default="direct")
    text: str = Field(..., min_length=1)
    metadata: dict[str, Any] = Field(default_factory=dict)


class RunRequest(BaseModel):
    """Optional parameters for triggering the Input-API -> Classifier pipeline."""

    input_id: str | None = Field(
        default=None,
        description="Optional input ID to fetch from the Input API (or placeholder sample).",
    )


class RouteResponse(BaseModel):
    """Combined router response with fetched email, enum decision, and Langfuse trace ID."""

    input: InputPayload
    decision: ClassificationDecision
    model: str
    trace_id: str | None = None


class MailboxAttachment(BaseModel):
    """Attachment metadata returned by GET /v1/mailbox/messages."""

    model_config = ConfigDict(extra="allow")

    id: int | str | None = None
    filename: str = ""
    content_type: str | None = None
    size: int | None = None


class MailboxMessage(BaseModel):
    """Normalized mail item returned by GET /v1/mailbox/messages."""

    model_config = ConfigDict(extra="allow")

    id: int
    sender: str | None = None
    to: str | list[str] | None = None
    subject: str = ""
    body: str = ""
    in_reply_to: int | None = None
    references: list[int] = Field(default_factory=list)
    attachments: list[MailboxAttachment] = Field(default_factory=list)
    created_at: str | None = None
    raw: dict[str, Any] = Field(default_factory=dict)


class MailboxPollResponse(BaseModel):
    """Response shape of GET /v1/mailbox/messages."""

    messages: list[MailboxMessage] = Field(default_factory=list)
    next_since_id: int = 0


class StoredMessageRecord(BaseModel):
    """Persisted mailbox message paired with its classification decision."""

    message_id: int
    sender: str | None = None
    to: str | list[str] | None = None
    subject: str
    body: str
    in_reply_to: int | None = None
    references: list[int] = Field(default_factory=list)
    attachments: list[MailboxAttachment] = Field(default_factory=list)
    decision: ClassificationDecision
    model: str
    trace_id: str | None = None
    received_at: str | None = None
    processed_at: str
    raw: dict[str, Any] = Field(default_factory=dict)


class MessageListResponse(BaseModel):
    """Paginated list of processed and categorized mails."""

    items: list[StoredMessageRecord]
    total: int
    last_since_id: int


class PollSummary(BaseModel):
    """Result summary for a single mailbox polling cycle."""

    fetched: int
    processed: int
    skipped_duplicates: int
    errors: int
    last_since_id: int


class PollerStatus(BaseModel):
    """Runtime status of the background mailbox poller."""

    enabled: bool
    running: bool
    interval_seconds: float
    last_since_id: int
    processed_count: int
    last_poll_at: str | None = None
    last_error: str | None = None


# --- Quote workflow -------------------------------------------------------------------------


class ShipmentState(StrEnum):
    """Shipment state machine (docs/workflow.md section 5)."""

    NEW = "NEW"
    AWAITING_INFO = "AWAITING_INFO"
    PRICING = "PRICING"
    QUOTED = "QUOTED"
    BOOKING = "BOOKING"
    BOOKED = "BOOKED"
    CONFIRMED = "CONFIRMED"
    REJECTED = "REJECTED"
    FAILED = "FAILED"


SenderRole = Literal["customer", "trucker", "airline", "ratedesk", "tracking"]
Goal = Literal["fastest", "cheapest"]


class ShipmentRequest(BaseModel):
    """What a customer asks for. Output of the `talos-extract-rfq` prompt."""

    pickup_postal_code: str | None = None
    pickup_city: str | None = None
    origin_airport: str | None = None
    delivers_to_airport: bool = False
    destination_iata: str | None = None
    destination_inferred: bool = False
    ready_date: date | None = None
    pieces: int | None = None
    gross_kg: float | None = None
    weight_computed: bool = False
    volume_cbm: float | None = None
    volume_computed: bool = False
    commodity: str | None = None
    goal: Goal | None = None
    extra_questions: list[str] = Field(default_factory=list)
    contact_name: str | None = None
    company: str | None = None
    language: str | None = None


class TruckerQuote(BaseModel):
    """A trucker's pickup offer. Output of the `talos-extract-trucker-quote` prompt."""

    price_eur: float | None = None
    pickup_hours: float | None = None
    valid_until: str | None = None
    quote_ref: str | None = None
    conditions: str = ""


class BookingConfirmation(BaseModel):
    """An airline's booking confirmation. Output of the `talos-extract-booking` prompt."""

    booking_ref: str | None = None
    our_reference: str | None = None
    airline: str | None = None
    airline_code: str | None = None
    lane: str | None = None
    price_eur: float | None = None
    currency: str | None = None


class ComposedMail(BaseModel):
    """An outgoing mail. Output of the `talos-compose-mail` prompt."""

    subject: str
    body: str


class AirOption(BaseModel):
    """One priced airport-to-airport option, from the portal or from a local tariff."""

    source: Literal["portal", "tariff"]
    airline: str
    airline_code: str | None = None
    origin: str
    destination: str
    billed_kg: float
    total_eur: float
    transit_days: int
    rate_id: str | None = None
    valid_until: str | None = None
    # What was sent to the portal for this rate; needed to refetch an expired rate.
    weight_kg: float | None = None
    volume_cbm: float | None = None


class TariffLane(BaseModel):
    """One lane of an airline's monthly tariff (data/tariffs.json)."""

    airline: str
    origin: str
    destination: str
    transit_days: int
    minimum_eur: float
    breaks_kg: list[float]
    rates_per_kg: list[float]
    fuel_per_kg: float
    security_per_kg: float
    currency: str = "EUR"
    valid_from: date
    valid_to: date
    source_file: str = ""


class TruckerRequest(BaseModel):
    """One pickup request we sent to one trucker, and their answer once it arrives."""

    out_message_id: int
    shipment_id: int
    trucker: str
    sent_at: str
    answered: bool = False
    price_eur: float | None = None
    pickup_hours: float | None = None
    valid_until: str | None = None
    ref: str | None = None
    reply_message_id: int | None = None


class Shipment(BaseModel):
    """One customer request from first mail to booking. `id` is the customer's first mail id."""

    id: int
    customer: str
    subject: str
    language: str | None = None
    state: ShipmentState = ShipmentState.NEW
    request: ShipmentRequest = Field(default_factory=ShipmentRequest)
    goal: Goal | None = None
    origin_airport: str | None = None
    zone: str | None = None
    clarifications: int = 0
    answers_sent: int = 0
    pricing_started_at: str | None = None
    quote: dict[str, Any] | None = None
    quote_message_id: int | None = None
    chosen_option: AirOption | None = None
    booking_ref: str | None = None
    booking_attempts: int = 0
    last_booking_attempt_at: str | None = None
    error: str | None = None
    created_at: str | None = None
    updated_at: str | None = None


class OutboundMail(BaseModel):
    """A mail the workflow composed. In a dry run it is only stored, never sent."""

    id: int
    shipment_id: int | None = None
    kind: str
    to: str
    subject: str
    body: str
    in_reply_to: int | None = None
    sent_message_id: int | None = None
    used_template: bool = False
    created_at: str


class MailContext(BaseModel):
    """What we know about an incoming mail before any LLM reads it (ADR 16)."""

    sender_role: SenderRole
    shipment: Shipment | None = None
    trucker_request: TruckerRequest | None = None
    our_mail: str = ""


class ShipmentDetail(BaseModel):
    """`GET /shipments/{id}`: a shipment with everything attached to it."""

    shipment: Shipment
    trucker_requests: list[TruckerRequest]
    air_options: list[AirOption]
    outbound: list[OutboundMail]
