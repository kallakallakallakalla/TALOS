"""The one way this app calls an LLM: a named prompt in, a validated Pydantic object out."""

import logging
import time
from collections.abc import Callable
from contextvars import ContextVar
from typing import Any, TypeVar

from langfuse import Langfuse, get_client
from langfuse.openai import AsyncOpenAI
from pydantic import BaseModel

from app.config import Settings, configure_langfuse_env
from app.prompts import Prompts

T = TypeVar("T", bound=BaseModel)

logger = logging.getLogger(__name__)

# The mail a call is made for. The poller sets it while it works on a mail; a call made by
# the timer (reading trucker replies, writing the quote afterwards) has none.
current_mail: ContextVar[int | None] = ContextVar("current_mail", default=None)


def _count(value: Any) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) else 0


def build_langfuse_client(settings: Settings) -> Langfuse:
    """Return the shared Langfuse client, or a no-op one when tracing is switched off."""
    if not configure_langfuse_env(settings):
        return Langfuse(tracing_enabled=False)
    return get_client()


class StructuredLLM:
    """Runs `chat.completions.parse` with a managed prompt. Shared by classifier, extractors
    and composer, so all three trace, version and fail the same way."""

    def __init__(
        self,
        settings: Settings,
        openai_client: Any | None = None,
        langfuse_client: Langfuse | None = None,
    ) -> None:
        self._settings = settings
        self.langfuse = langfuse_client or build_langfuse_client(settings)
        self.prompts = Prompts(settings, self.langfuse)
        self._openai = openai_client or AsyncOpenAI(
            api_key=settings.decision_api_key or "missing-api-key",
            base_url=settings.decision_api_base_url,
        )
        # Called with the token use of every call (MessageStore.record_llm_call): LLM cost
        # is 40 of 100 points per accepted mail, so it is counted where it arises.
        self.on_usage: Callable[[dict[str, Any]], None] | None = None

    def _record(self, prompt_name: str, model: str, completion: Any, seconds: float) -> None:
        """Report what one call used. Bookkeeping must never break the call itself."""
        if self.on_usage is None:
            return
        try:
            usage = getattr(completion, "usage", None)
            self.on_usage(
                {
                    "prompt": prompt_name,
                    "model": model,
                    "message_id": current_mail.get(),
                    "input_tokens": _count(getattr(usage, "prompt_tokens", None)),
                    "cached_tokens": _count(getattr(getattr(usage, "prompt_tokens_details", None), "cached_tokens", None)),
                    "output_tokens": _count(getattr(usage, "completion_tokens", None)),
                    "reasoning_tokens": _count(getattr(getattr(usage, "completion_tokens_details", None), "reasoning_tokens", None)),
                    "seconds": round(seconds, 3),
                }
            )
        except Exception:  # noqa: BLE001 - see docstring
            logger.warning("Could not record the usage of an LLM call", exc_info=True)

    async def transcribe(self, filename: str, data: bytes) -> str:
        """Speech to text for a voice-message attachment."""
        result = await self._openai.audio.transcriptions.create(
            model=self._settings.transcribe_model,
            file=(filename, data),
            prompt="Freight price offer. Write all amounts, weights and times as digits, e.g. 340.60 EUR, 1410 kg, 12 hours.",
        )
        return result.text

    async def parse(
        self,
        prompt_name: str,
        schema: type[T],
        user_content: str | list[dict[str, Any]],
        *,
        model: str,
        reasoning_effort: str = "",
        temperature: float | None = None,
        prompt_label: str | None = None,
        **prompt_vars: Any,
    ) -> T:
        """Send one mail (`user_content`) to `model` under the named system prompt."""
        system_prompt, lf_prompt = self.prompts.get(prompt_name, label=prompt_label, **prompt_vars)
        kwargs: dict[str, Any] = {
            "name": prompt_name,
            "model": model,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_content},
            ],
            "response_format": schema,
        }
        if reasoning_effort:
            kwargs["reasoning_effort"] = reasoning_effort
        if temperature is not None:
            kwargs["temperature"] = temperature
        if lf_prompt is not None:
            kwargs["langfuse_prompt"] = lf_prompt

        started = time.monotonic()
        completion = await self._openai.chat.completions.parse(**kwargs)
        self._record(prompt_name, model, completion, time.monotonic() - started)
        parsed = completion.choices[0].message.parsed
        if parsed is None:
            raise ValueError(f"Model {model} did not return a parsed {schema.__name__}.")
        return parsed
