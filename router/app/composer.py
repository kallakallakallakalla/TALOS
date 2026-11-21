"""Writes every outgoing mail with an LLM from structured facts, then checks the facts in code.

The workflow decides who gets which mail; this module only produces the words (ADR 13).
"""

import json
import logging
import random
import re
from datetime import UTC, datetime
from typing import Any

from langfuse import get_client, observe

from app import prompts, templates
from app.config import Settings
from app.llm import StructuredLLM
from app.schemas import ComposedMail

logger = logging.getLogger(__name__)

_DIVIDER = "-" * 40
# The same prompt gives the same phrases every time. Each mail to a customer therefore gets
# its own random pick per slot, named in the request to the model (ADR 48).
_WORDING = {
    "greeting": ("Dear", "Hi", "Hello", "Hey"),
    "opener": (
        "thanks for contacting us",
        "thank you for your request",
        "many thanks for your enquiry",
        "thanks for getting in touch with us",
        "thank you for sending this over",
    ),
    "call to action": (
        "Please confirm if you'd like us to proceed.",
        "Just say the word and I'll book it.",
        "Kindly let us know and we will arrange the booking.",
        "A short confirmation is enough and we go ahead.",
    ),
    "questions": (
        "if you have any questions please feel free to reach, also via phone",
        "for any questions just give us a call or drop me a line",
        "should anything be unclear we are happy to help, also by phone",
        "any open points, feel free to call or reply here",
    ),
    "sign-off": ("Kind regards", "Best regards", "Many thanks and kind regards", "Best"),
    "slip": ("none", "none", "one missing comma", "one dropped article", "one missing apostrophe"),
}


def missing_facts(facts: dict[str, Any], mail: ComposedMail) -> list[str]:
    """Values of `facts["must_appear"]` that do not occur verbatim in the mail body."""
    return [str(item) for item in facts.get("must_appear", []) if str(item) not in mail.body]


def plain_text(body: str) -> str:
    """Take out what the mail style forbids and a model still writes at times: dashes, bold."""
    return re.sub(r"\s*—\s*", ", ", body).replace("–", "-").replace("**", "")


def wording() -> str:
    """One random pick per wording slot, e.g. `greeting "Hi", opener "thanks for ..."`."""
    return ", ".join(f'{slot} "{random.choice(options)}"' for slot, options in _WORDING.items())


def with_footer(body: str, footer: str) -> str:
    """The mail body with the company footer between two divider lines."""
    return f"{body.rstrip()}\n\n{_DIVIDER}\n{footer.strip()}\n{_DIVIDER}"


def reply_subject(original_subject: str) -> str:
    return original_subject if original_subject.lower().startswith("re:") else f"Re: {original_subject}"


class Composer:
    """`compose()` returns a mail that is guaranteed to contain every `must_appear` value."""

    def __init__(self, settings: Settings, llm: StructuredLLM) -> None:
        self._settings = settings
        self._llm = llm

    @observe(name="compose")
    async def compose(
        self,
        kind: str,
        facts: dict[str, Any],
        *,
        recipient_role: str,
        recipient_name: str = "",
        language: str = "en",
        thread: str = "",
        reply_to_subject: str | None = None,
        prompt_label: str | None = None,
    ) -> tuple[ComposedMail, bool]:
        """Write one mail of `kind`. Returns `(mail, used_template)`.

        One LLM draft, one retry that names the missing values, then the plain template.
        When replying, the subject is always "Re: <original subject>", set in code.
        A mail to a customer ends with the footer (`mail_footer`), also set in code.
        """
        # The prompt turns dates into "today until 15:34 UTC"; for that the model needs the date.
        instruction = f"Write the mail. Today is {datetime.now(UTC):%Y-%m-%d}."
        to_customer = recipient_role == "customer"
        if to_customer:
            instruction += f" Wording for this mail: {wording()}."
            if thread:
                # The detected language can be missing or stale; the customer's own words are not.
                instruction += (
                    " Write in the language the customer wrote their mail in (see the thread),"
                    " even if that differs from the Language field."
                )
        mail, used_template = None, False
        for _attempt in range(2):
            try:
                draft = await self._llm.parse(
                    prompts.COMPOSE_MAIL,
                    ComposedMail,
                    instruction,
                    model=self._settings.compose_model,
                    reasoning_effort=self._settings.compose_reasoning_effort,
                    prompt_label=prompt_label,
                    mail_kind=kind,
                    recipient_role=recipient_role,
                    recipient_name=recipient_name,
                    language=language or "en",
                    signature=self._settings.mail_signature,
                    thread=thread,
                    facts_json=json.dumps(facts, indent=1, default=str),
                )
            except Exception as exc:  # noqa: BLE001 - a mail must go out even if the LLM is down
                logger.warning("Composer call failed for %s: %s", kind, exc)
                continue
            draft = draft.model_copy(update={"body": plain_text(draft.body)})
            missing = missing_facts(facts, draft)
            if not missing:
                mail = draft
                break
            logger.warning("Composed %s misses %s", kind, missing)
            instruction += (
                " Your previous draft left out these values; they must appear "
                f"in the body exactly as written here: {missing}"
            )

        langfuse = get_client()
        if mail is None:
            mail, used_template = templates.render(
                kind, facts, self._settings.mail_signature, recipient_name
            ), True
            langfuse.update_current_span(level="WARNING", status_message="template fallback")
        langfuse.score_current_span(name="fact_check", value=0 if used_template else 1)

        if to_customer and self._settings.mail_footer:
            body = with_footer(mail.body, self._settings.mail_footer)
            mail = mail.model_copy(update={"body": body})
        if reply_to_subject is not None:
            mail = mail.model_copy(update={"subject": reply_subject(reply_to_subject)})
        return mail, used_template
