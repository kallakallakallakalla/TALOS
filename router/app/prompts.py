"""Prompt loading: always the repo file `prompts/<name>.md`, with `{{var}}` placeholders."""

from pathlib import Path
from typing import Any


from app.config import Settings


CLASSIFY = "router-classification"
EXTRACT_RFQ = "talos-extract-rfq"
EXTRACT_TRUCKER_QUOTES = "talos-extract-trucker-quotes"
EXTRACT_BOOKING = "talos-extract-booking"
EXTRACT_TRACKING = "talos-extract-tracking"
COMPOSE_MAIL = "talos-compose-mail"
READ_ATTACHMENT = "talos-read-attachment"
EXTRACT_AIRLINE_RATES = "talos-extract-airline-rates"
EXTRACT_DATA_UPDATE = "talos-extract-data-update"
FLAG_DATA_UPDATE = "talos-flag-data-update"
CHOOSE_TRUCKER = "talos-choose-trucker"
ALL_PROMPTS = (
    CLASSIFY,
    EXTRACT_RFQ,
    EXTRACT_TRUCKER_QUOTES,
    EXTRACT_BOOKING,
    EXTRACT_TRACKING,
    COMPOSE_MAIL,
    READ_ATTACHMENT,
    EXTRACT_AIRLINE_RATES,
    EXTRACT_DATA_UPDATE,
    FLAG_DATA_UPDATE,
    CHOOSE_TRUCKER,
)


def local_prompt(settings: Settings, name: str) -> str:
    """Read the local fallback file of a prompt."""
    return (Path(settings.prompts_dir) / f"{name}.md").read_text(encoding="utf-8").strip()


def fill(template: str, variables: dict[str, Any]) -> str:
    """Replace `{{name}}` placeholders. Unknown placeholders stay visible on purpose."""
    for key, value in variables.items():
        template = template.replace("{{" + key + "}}", "" if value is None else str(value))
    return template


class Prompts:
    """Resolves a prompt name to its text from the repo. Langfuse is not used for prompts."""

    def __init__(self, settings: Settings, langfuse_client: Any | None = None) -> None:
        self._settings = settings

    def get(self, name: str, label: str | None = None, **variables: Any) -> tuple[str, None]:
        """Return `(compiled_text, None)`. `label` is ignored; kept so callers need no change."""
        return fill(local_prompt(self._settings, name), variables), None
