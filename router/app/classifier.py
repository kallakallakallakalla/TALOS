import logging
from typing import Any

from langfuse import Langfuse, get_client, observe
from langfuse.openai import AsyncOpenAI

from app.config import Settings, configure_langfuse_env
from app.schemas import ClassificationDecision, InputPayload

logger = logging.getLogger(__name__)

DEFAULT_SYSTEM_PROMPT = (
    "You are the TALOS Email Router Decision Engine. "
    "Classify the incoming email into exactly one of the allowed EmailCategory enum values: "
    "RFQ (request for quotation / pricing / offer), "
    "update (status update on an order, project, or process), "
    "bs (spam, irrelevant chatter, or nonsense), "
    "Question (a question that requires an answer), "
    "Answer (a reply answering a previous question), "
    "Tracking (shipment, delivery, or tracking number information). "
    "Provide a confidence score between 0.0 and 1.0 and a concise reasoning."
)


class DecisionClassifier:
    """Classifies an InputPayload into multiple Enums via gpt-6-luna with Langfuse tracing."""

    def __init__(
        self,
        settings: Settings,
        openai_client: Any | None = None,
        langfuse_client: Langfuse | None = None,
    ) -> None:
        self._settings = settings
        self._langfuse = langfuse_client or self._build_langfuse_client(settings)
        self._openai = openai_client or AsyncOpenAI(
            api_key=settings.decision_api_key or "missing-api-key",
            base_url=settings.decision_api_base_url,
        )

    @staticmethod
    def _build_langfuse_client(settings: Settings) -> Langfuse:
        enabled = configure_langfuse_env(settings)
        if not enabled:
            return Langfuse(tracing_enabled=False)
        return get_client()

    def _resolve_system_prompt(self) -> tuple[str, Any | None]:
        """Try loading prompt from Langfuse Prompt Management; fall back to local default."""
        if not (
            self._settings.langfuse_enabled
            and self._settings.langfuse_public_key
            and self._settings.langfuse_secret_key
            and self._settings.langfuse_prompt_name
        ):
            return DEFAULT_SYSTEM_PROMPT, None

        try:
            lf_prompt = self._langfuse.get_prompt(self._settings.langfuse_prompt_name)
            return lf_prompt.compile(), lf_prompt
        except Exception as exc:  # noqa: BLE001 - graceful MVP fallback
            logger.debug("Using fallback system prompt (Langfuse prompt unavailable: %s)", exc)
            return DEFAULT_SYSTEM_PROMPT, None

    @observe(name="gpt-6-luna-classify")
    async def classify(self, payload: InputPayload) -> tuple[ClassificationDecision, str | None]:
        """Classify the input payload into structured Enums and return (decision, trace_id)."""
        system_prompt, lf_prompt = self._resolve_system_prompt()
        user_content = (
            f"Input ID: {payload.id}\n"
            f"Source: {payload.source}\n"
            f"Metadata: {payload.metadata}\n"
            f"Text:\n{payload.text}"
        )

        create_kwargs: dict[str, Any] = {
            "model": self._settings.decision_model,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_content},
            ],
            "response_format": ClassificationDecision,
        }
        if self._settings.decision_temperature is not None:
            create_kwargs["temperature"] = self._settings.decision_temperature
        if lf_prompt is not None:
            create_kwargs["langfuse_prompt"] = lf_prompt

        completion = await self._openai.chat.completions.parse(**create_kwargs)
        message = completion.choices[0].message
        if message.parsed is None:
            raise ValueError(
                f"Model {self._settings.decision_model} did not return a parsed ClassificationDecision."
            )

        trace_id = get_client().get_current_trace_id()
        return message.parsed, trace_id
