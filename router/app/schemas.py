from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class EmailCategory(StrEnum):
    """Email classification categories."""

    RFQ = "RFQ"
    UPDATE = "update"
    BS = "bs"
    QUESTION = "Question"
    ANSWER = "Answer"
    TRACKING = "Tracking"


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
