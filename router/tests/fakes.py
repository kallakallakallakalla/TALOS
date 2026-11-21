"""Test doubles shared by the workflow tests: a scripted LLM and recorded mails."""

import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from pydantic import BaseModel

from app.mailbox_client import parse_mailbox_message
from app.schemas import MailboxMessage

FIXTURES = Path(__file__).parent / "fixtures"


def recorded_mails() -> dict[int, MailboxMessage]:
    """Every recorded mail (inbound and our own outbound) by id."""
    data = json.loads((FIXTURES / "mails.json").read_text())
    return {m["id"]: parse_mailbox_message(m) for m in data["inbound"] + data["outbound"]}


def labels() -> dict[int, dict[str, Any]]:
    return {int(k): v for k, v in json.loads((FIXTURES / "labels.json").read_text()).items()}


class ScriptedLLM:
    """Stands in for the OpenAI client: answers each structured call from a function.

    `answer(schema, system_prompt, user_content) -> BaseModel | Exception`. Every call is
    recorded in `calls` as `(schema name, system prompt, user content)`.
    """

    def __init__(self, answer) -> None:
        self._answer = answer
        self.calls: list[tuple[str, str, str]] = []
        self.chat = SimpleNamespace(completions=self)

    async def parse(self, **kwargs):
        schema: type[BaseModel] = kwargs["response_format"]
        system, user = (m["content"] for m in kwargs["messages"])
        self.calls.append((schema.__name__, system, user))
        parsed = self._answer(schema, system, user)
        if isinstance(parsed, Exception):
            raise parsed
        return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(parsed=parsed))])
