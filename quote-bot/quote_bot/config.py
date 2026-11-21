"""Config from environment variables (optional .env file)."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

try:  # optional dependency
    from dotenv import load_dotenv
except ImportError:  # pragma: no cover
    load_dotenv = None


@dataclass(frozen=True)
class Config:
    base_url: str
    api_key: str
    llm_model: str
    poll_interval_s: float
    db_path: Path
    ports_refresh_s: float
    brackets_ttl_s: float
    quote_window_s: float
    safety_margin_s: float
    rate_workers: int
    http_timeout_s: float
    llm_max_chars: int


def _env(name: str, default: str) -> str:
    value = os.getenv(name)
    return value.strip() if value and value.strip() else default


def load_config(require_key: bool = True) -> Config:
    if load_dotenv is not None:
        load_dotenv()  # searches upward from the package directory
        load_dotenv(Path.cwd() / ".env")

    api_key = _env("FIVEU_API_KEY", "")
    if require_key and not api_key:
        raise SystemExit("FIVEU_API_KEY missing: set the env var or put it in .env")

    return Config(
        base_url=_env("FIVEU_BASE_URL", "https://api.start-hack.5u.ai").rstrip("/"),
        api_key=api_key,
        llm_model=_env("LLM_MODEL", "gpt-4.1-nano"),
        poll_interval_s=float(_env("POLL_INTERVAL_S", "3")),
        db_path=Path(_env("DB_PATH", "quote_bot.sqlite3")),
        ports_refresh_s=float(_env("PORTS_REFRESH_S", "300")),
        brackets_ttl_s=float(_env("BRACKETS_TTL_S", "300")),
        quote_window_s=float(_env("QUOTE_WINDOW_S", "360")),
        safety_margin_s=float(_env("SAFETY_MARGIN_S", "60")),
        rate_workers=int(_env("RATE_WORKERS", "8")),
        http_timeout_s=float(_env("HTTP_TIMEOUT_S", "15")),
        llm_max_chars=int(_env("LLM_MAX_CHARS", "4000")),
    )
