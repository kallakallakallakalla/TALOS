"""Writes every outgoing mail with an LLM from structured facts, then checks the facts in code.

The workflow decides who gets which mail; this module only produces the words (ADR 13).
"""

import json
import logging
import re
from datetime import UTC, datetime
from typing import Any

from langfuse import get_client, observe

from app import prompts, templates
from app.config import Settings
from app.llm import StructuredLLM
from app.schemas import ComposedMail

logger = logging.getLogger(__name__)

def missing_facts(facts: dict[str, Any], mail: ComposedMail) -> list[str]:
    """Values of `facts["must_appear"]` that do not occur verbatim in the mail body."""
    return [str(item) for item in facts.get("must_appear", []) if str(item) not in mail.body]


def plain_text(body: str) -> str:
    """Take out what the mail style forbids and a model still writes at times: dashes, bold."""
    return re.sub(r"\s*—\s*", ", ", body).replace("–", "-").replace("**", "")


# ponytail: stopword ratio, not a language detector. Catches the failure we saw (English
# reply to a non-English customer); use a real detector if other mix-ups show up.
_ENGLISH_WORDS = set(
    "the and you your we our for is are to of with this that will can please on at it be "
    "have has from by as an if any".split()
)
LANGUAGE_NAMES = {
    "de": "German", "en": "English", "fr": "French", "es": "Spanish", "it": "Italian",
    "nl": "Dutch", "pl": "Polish", "pt": "Portuguese", "tr": "Turkish", "zh": "Chinese",
    "ja": "Japanese", "ar": "Arabic", "ru": "Russian", "cs": "Czech", "sv": "Swedish",
}


def looks_english(body: str) -> bool:
    words = re.findall(r"[a-z']+", body.lower())
    return len(words) >= 10 and sum(w in _ENGLISH_WORDS for w in words) / len(words) >= 0.15


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
        The tone comes from the customer's own mail (`thread`), which the prompt mirrors.
        """
        # The prompt turns dates into "today until 15:34 UTC"; for that the model needs the date.
        instruction = f"Write the mail. Today is {datetime.now(UTC):%Y-%m-%d}."
        to_customer = recipient_role == "customer"
        language = (language or "en").lower()
        language_name = LANGUAGE_NAMES.get(language, language)
        if to_customer:
            # Seen live: told "de" with a German thread, the model still wrote English.
            instruction += (
                f" Write the whole mail, greeting and sign-off included, in {language_name},"
                " the language the customer wrote in (see the thread); if the thread is in"
                " another language, use that one."
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
                    language=language_name,
                    signature=self._settings.mail_signature,
                    thread=thread,
                    facts_json=json.dumps(facts, indent=1, default=str),
                )
            except Exception as exc:  # noqa: BLE001 - a mail must go out even if the LLM is down
                logger.warning("Composer call failed for %s: %s", kind, exc)
                continue
            draft = draft.model_copy(update={"body": plain_text(draft.body)})
            missing = missing_facts(facts, draft)
            wrong_language = to_customer and language != "en" and looks_english(draft.body)
            if not missing and not wrong_language:
                mail = draft
                break
            if missing:
                logger.warning("Composed %s misses %s", kind, missing)
                instruction += (
                    " Your previous draft left out these values; they must appear "
                    f"in the body exactly as written here: {missing}"
                )
            if wrong_language:
                logger.warning("Composed %s in English, customer wrote %s", kind, language)
                instruction += (
                    f" Your previous draft was in English. The customer wrote in"
                    f" {language_name}: write the whole mail in {language_name}."
                )

        langfuse = get_client()
        if mail is None:
            mail, used_template = templates.render(
                kind, facts, self._settings.mail_signature, recipient_name
            ), True
            langfuse.update_current_span(level="WARNING", status_message="template fallback")
        langfuse.score_current_span(name="fact_check", value=0 if used_template else 1)

        if reply_to_subject is not None:
            mail = mail.model_copy(update={"subject": reply_subject(reply_to_subject)})
        return mail, used_template
