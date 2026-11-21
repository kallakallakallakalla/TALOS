from typing import Any

import httpx

from app.config import Settings
from app.schemas import (
    InputPayload,
    MailboxAttachment,
    MailboxMessage,
    MailboxPollResponse,
)

_KNOWN_MESSAGE_KEYS = {
    "id",
    "from",
    "sender",
    "from_address",
    "author",
    "to",
    "recipient",
    "recipients",
    "subject",
    "body",
    "text",
    "content",
    "html",
    "in_reply_to",
    "references",
    "attachments",
    "created_at",
    "sent_at",
    "received_at",
    "timestamp",
}


def _first_nonempty_str(source: dict[str, Any], keys: tuple[str, ...]) -> str | None:
    for key in keys:
        value = source.get(key)
        if isinstance(value, str) and value.strip():
            return value
    return None


def _extract_sender(raw: dict[str, Any]) -> str | None:
    for key in ("from", "sender", "from_address", "author"):
        value = raw.get(key)
        if isinstance(value, str) and value.strip():
            return value
        if isinstance(value, dict):
            nested = _first_nonempty_str(value, ("email", "address", "name"))
            if nested is not None:
                return nested
    return None


def _extract_body(raw: dict[str, Any]) -> str:
    for key in ("body", "text", "content", "html"):
        value = raw.get(key)
        if isinstance(value, str):
            return value
    return ""


def _extract_timestamp(raw: dict[str, Any]) -> str | None:
    return _first_nonempty_str(raw, ("created_at", "sent_at", "received_at", "timestamp"))


def _parse_attachments(raw_attachments: Any) -> list[MailboxAttachment]:
    if not isinstance(raw_attachments, list):
        return []
    parsed: list[MailboxAttachment] = []
    for item in raw_attachments:
        if not isinstance(item, dict):
            continue
        normalized = dict(item)
        if "content_type" not in normalized:
            normalized["content_type"] = normalized.get("contentType") or normalized.get(
                "mime_type"
            )
        parsed.append(MailboxAttachment.model_validate(normalized))
    return parsed


def parse_mailbox_message(raw: dict[str, Any]) -> MailboxMessage:
    """Normalize a raw JSON item from GET /v1/mailbox/messages into MailboxMessage."""
    references_raw = raw.get("references")
    references = [int(ref) for ref in references_raw] if isinstance(references_raw, list) else []

    to_val = raw.get("to")
    if to_val is None:
        to_val = raw.get("recipient") or raw.get("recipients")

    in_reply_to_raw = raw.get("in_reply_to")
    in_reply_to = int(in_reply_to_raw) if in_reply_to_raw is not None else None

    return MailboxMessage(
        id=int(raw["id"]),
        sender=_extract_sender(raw),
        to=to_val,
        subject=str(raw.get("subject") or ""),
        body=_extract_body(raw),
        in_reply_to=in_reply_to,
        references=references,
        attachments=_parse_attachments(raw.get("attachments")),
        created_at=_extract_timestamp(raw),
        raw=dict(raw),
    )


def mailbox_message_to_payload(msg: MailboxMessage) -> InputPayload:
    """Convert a MailboxMessage into an InputPayload for DecisionClassifier."""
    parts: list[str] = []
    if msg.subject.strip():
        parts.append(f"Subject: {msg.subject.strip()}")
    if msg.body.strip():
        parts.append(msg.body.strip())
    text = "\n\n".join(parts) if parts else "(empty email)"

    metadata: dict[str, Any] = {
        "message_id": msg.id,
        "from": msg.sender,
        "to": msg.to,
        "in_reply_to": msg.in_reply_to,
        "references": msg.references,
        "created_at": msg.created_at,
        "attachments": [
            {
                "id": att.id,
                "filename": att.filename,
                "content_type": att.content_type,
                "size": att.size,
            }
            for att in msg.attachments
        ],
    }
    extra_fields = {k: v for k, v in msg.raw.items() if k not in _KNOWN_MESSAGE_KEYS}
    if extra_fields:
        metadata["extra"] = extra_fields

    return InputPayload(
        id=str(msg.id),
        source="mailbox",
        text=text,
        metadata=metadata,
    )


class MailboxClient:
    """Client for the Hackloop mailbox API (poll, read one, send) per openapi.json."""

    def __init__(
        self,
        settings: Settings,
        http_client: httpx.AsyncClient | None = None,
    ) -> None:
        self._settings = settings
        self._http_client = http_client

    async def _request(self, method: str, path: str, **kwargs: Any) -> httpx.Response:
        if not self._settings.hackloop_api_key:
            raise ValueError("HACKLOOP_API_KEY is required to call the mailbox API.")
        url = f"{self._settings.hackloop_base_url.rstrip('/')}{path}"
        headers = {"X-API-Key": self._settings.hackloop_api_key}
        if self._http_client is not None:
            response = await self._http_client.request(method, url, headers=headers, **kwargs)
        else:
            async with httpx.AsyncClient(timeout=self._settings.mailbox_timeout_seconds) as client:
                response = await client.request(method, url, headers=headers, **kwargs)
        response.raise_for_status()
        return response

    async def poll_messages(self, since_id: int = 0, limit: int = 100) -> MailboxPollResponse:
        """Fetch messages with id > since_id (oldest first, max 200 per call)."""
        bounded_since_id = max(0, since_id)
        bounded_limit = max(1, min(200, limit))
        response = await self._request(
            "GET",
            "/v1/mailbox/messages",
            params={"since_id": bounded_since_id, "limit": bounded_limit},
        )
        data = response.json()
        raw_messages = data.get("messages") or []
        messages = [parse_mailbox_message(item) for item in raw_messages]
        default_next = max([bounded_since_id, *(msg.id for msg in messages)])
        next_since_id = int(data.get("next_since_id", default_next))
        return MailboxPollResponse(messages=messages, next_since_id=next_since_id)

    async def get_message(self, message_id: int) -> MailboxMessage:
        """Read one mail by id. Also returns mail WE sent, e.g. our own quote."""
        response = await self._request("GET", f"/v1/mailbox/messages/{message_id}")
        return parse_mailbox_message(response.json())

    async def download_attachment(self, message_id: int, attachment_id: int | str) -> bytes:
        """The raw bytes of one attachment."""
        path = f"/v1/mailbox/messages/{message_id}/attachments/{attachment_id}"
        return (await self._request("GET", path)).content

    async def send(
        self, to: str, subject: str, body: str, in_reply_to: int | None = None
    ) -> MailboxMessage:
        """Send a mail from our address and return it as stored (with its new id).

        Callers must respect `settings.send_enabled`; this method always sends.
        """
        payload: dict[str, Any] = {"to": to, "subject": subject, "body": body}
        if in_reply_to is not None:
            payload["in_reply_to"] = in_reply_to
        response = await self._request("POST", "/v1/mailbox/messages", json=payload)
        return parse_mailbox_message(response.json())
