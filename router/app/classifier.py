from typing import Any

from langfuse import Langfuse, get_client, observe

from app import prompts
from app.config import Settings
from app.llm import StructuredLLM
from app.schemas import ClassificationDecision, InputPayload, MailContext

_OUR_MAIL_MAX_CHARS = 1500


class DecisionClassifier:
    """First LLM step: decides the mail type. It never extracts data (ADR 15)."""

    def __init__(
        self,
        settings: Settings,
        openai_client: Any | None = None,
        langfuse_client: Langfuse | None = None,
        llm: StructuredLLM | None = None,
    ) -> None:
        self._settings = settings
        self._llm = llm or StructuredLLM(settings, openai_client, langfuse_client)

    @observe(name="classify")
    async def classify(
        self,
        payload: InputPayload,
        context: MailContext | None = None,
        prompt_label: str | None = None,
    ) -> tuple[ClassificationDecision, str | None]:
        """Classify one mail and return `(decision, trace_id)`.

        `context` is the no-LLM lookup (sender role, our previous mail, shipment state).
        Without it the classifier only sees the mail itself.
        """
        shipment = context.shipment if context else None
        user_content = (
            f"Input ID: {payload.id}\n"
            f"Source: {payload.source}\n"
            f"Metadata: {payload.metadata}\n"
            f"Text:\n{payload.text}"
        )
        decision = await self._llm.parse(
            self._settings.langfuse_prompt_name or prompts.CLASSIFY,
            ClassificationDecision,
            user_content,
            model=self._settings.decision_model,
            reasoning_effort=self._settings.decision_reasoning_effort,
            temperature=self._settings.decision_temperature,
            prompt_label=prompt_label,
            sender_role=context.sender_role if context else "",
            shipment_state=shipment.state.value if shipment else "",
            our_mail=(context.our_mail if context else "")[:_OUR_MAIL_MAX_CHARS],
        )
        return decision, get_client().get_current_trace_id()
