import os

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Application settings loaded from environment variables or .env."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # Langfuse
    langfuse_enabled: bool = True
    langfuse_public_key: str = ""
    langfuse_secret_key: str = ""
    langfuse_host: str = "http://localhost:3000"
    langfuse_base_url: str = ""
    langfuse_prompt_name: str = "router-classification"

    # Decision API (OpenAI-compatible)
    decision_api_key: str = ""
    decision_api_base_url: str = "https://api.openai.com/v1"
    decision_model: str = "gpt-6-luna"
    decision_temperature: float | None = None

    # Input API (empty = use built-in placeholder)
    input_api_url: str = ""
    input_api_token: str = ""
    input_api_timeout_seconds: float = 10.0

    # Hackloop Mailbox API & Polling
    hackloop_base_url: str = "https://api.start-hack.5u.ai"
    hackloop_api_key: str = ""
    hackloop_mailbox: str = ""
    mailbox_poll_enabled: bool = True
    mailbox_poll_interval_seconds: float = 10.0
    mailbox_poll_limit: int = 100
    mailbox_timeout_seconds: float = 10.0

    # SQLite persistence for since_id cursor, deduplication, and results
    sqlite_db_path: str = "data/router.db"


def get_settings() -> Settings:
    """Return a fresh Settings instance."""
    return Settings()


def configure_langfuse_env(settings: Settings) -> bool:
    """Sync Langfuse settings into os.environ for @observe, get_client, and langfuse.openai."""
    has_keys = bool(settings.langfuse_public_key and settings.langfuse_secret_key)
    if not (settings.langfuse_enabled and has_keys):
        os.environ["LANGFUSE_TRACING_ENABLED"] = "false"
        return False

    base_url = settings.langfuse_base_url or settings.langfuse_host
    os.environ["LANGFUSE_TRACING_ENABLED"] = "true"
    os.environ["LANGFUSE_PUBLIC_KEY"] = settings.langfuse_public_key
    os.environ["LANGFUSE_SECRET_KEY"] = settings.langfuse_secret_key
    os.environ["LANGFUSE_HOST"] = base_url
    os.environ["LANGFUSE_BASE_URL"] = base_url
    return True
