"""Prompt loading: Langfuse Prompt Management first, local `prompts/<name>.md` as fallback.

Both sources use the same `{{var}}` placeholders (ADR 17).
"""

import logging
from pathlib import Path
from typing import Any

from langfuse import Langfuse

from app.config import Settings

logger = logging.getLogger(__name__)

CLASSIFY = "router-classification"
EXTRACT_RFQ = "talos-extract-rfq"
EXTRACT_TRUCKER_QUOTE = "talos-extract-trucker-quote"
EXTRACT_BOOKING = "talos-extract-booking"
COMPOSE_MAIL = "talos-compose-mail"
ALL_PROMPTS = (CLASSIFY, EXTRACT_RFQ, EXTRACT_TRUCKER_QUOTE, EXTRACT_BOOKING, COMPOSE_MAIL)


def local_prompt(settings: Settings, name: str) -> str:
    """Read the local fallback file of a prompt."""
    return (Path(settings.prompts_dir) / f"{name}.md").read_text(encoding="utf-8").strip()


def fill(template: str, variables: dict[str, Any]) -> str:
    """Replace `{{name}}` placeholders. Unknown placeholders stay visible on purpose."""
    for key, value in variables.items():
        template = template.replace("{{" + key + "}}", "" if value is None else str(value))
    return template


class Prompts:
    """Resolves a prompt name to its text and, when it came from Langfuse, its version handle."""

    def __init__(self, settings: Settings, langfuse_client: Langfuse | None = None) -> None:
        self._settings = settings
        self._langfuse = langfuse_client
        self._use_langfuse = bool(
            langfuse_client is not None
            and settings.langfuse_enabled
            and settings.langfuse_public_key
            and settings.langfuse_secret_key
        )

    def get(self, name: str, label: str | None = None, **variables: Any) -> tuple[str, Any | None]:
        """Return `(compiled_text, lf_prompt)`; `lf_prompt` is None when the local file was used.

        Pass `lf_prompt` as `langfuse_prompt=` so the generation is linked to its prompt version.
        """
        local = local_prompt(self._settings, name)
        if not self._use_langfuse:
            return fill(local, variables), None
        try:
            # The SDK caches the prompt for ~60 s and serves `fallback` if Langfuse is down.
            lf_prompt = self._langfuse.get_prompt(
                name, label=label or self._settings.prompt_label, fallback=local
            )
        except Exception as exc:  # noqa: BLE001 - a prompt lookup must never stop a mail
            logger.warning("Langfuse prompt %s unavailable, using local file: %s", name, exc)
            return fill(local, variables), None
        if lf_prompt.is_fallback:
            return fill(local, variables), None
        return fill(lf_prompt.prompt, variables), lf_prompt
