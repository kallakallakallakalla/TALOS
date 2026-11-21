"""The one way this app calls an LLM: a named prompt in, a validated Pydantic object out."""

from typing import Any, TypeVar

from langfuse import Langfuse, get_client
from langfuse.openai import AsyncOpenAI
from pydantic import BaseModel

from app.config import Settings, configure_langfuse_env
from app.prompts import Prompts

T = TypeVar("T", bound=BaseModel)


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

        completion = await self._openai.chat.completions.parse(**kwargs)
        parsed = completion.choices[0].message.parsed
        if parsed is None:
            raise ValueError(f"Model {model} did not return a parsed {schema.__name__}.")
        return parsed
