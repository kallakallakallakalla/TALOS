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
    # Langfuse prompt label the running router reads ("staging" for evals).
    prompt_label: str = "production"
    prompts_dir: str = "prompts"

    # LLM gateway (OpenAI-compatible). One model setting per task (ADR 14).
    decision_api_key: str = ""
    decision_api_base_url: str = "https://api.openai.com/v1"
    decision_model: str = "gpt-6-luna"
    extract_model: str = "gpt-6-luna"
    transcribe_model: str = "gpt-4o-mini-transcribe"
    compose_model: str = "gpt-6-luna"
    decision_temperature: float | None = None
    # Reasoning tokens are billed and slow; "none" | "low" | "medium" | "high" | "" (model default).
    decision_reasoning_effort: str = "none"
    extract_reasoning_effort: str = "low"
    trucker_reasoning_effort: str = "none"  # one batch call per shipment; plain copying
    compose_reasoning_effort: str = "none"

    # Tests, evals and replays call OpenAI directly with this key (ADR 39). It must never be
    # registered at the challenge gateway: everything through the gateway counts as
    # scored production usage.
    evaluation_key: str = ""
    evaluation_api_base_url: str = "https://api.openai.com/v1"
    # TEST_MODE=true runs the whole router as a test: nothing is sent or booked, LLM calls
    # use EVALUATION_KEY directly, shipments go to their own database, and portal reads are
    # throttled so the live process keeps most of the shared limit.
    test_mode: bool = False
    test_sqlite_db_path: str = "data/test.db"
    test_portal_rate_limit_per_minute: int = 3

    # Input API (empty = use built-in placeholder)
    input_api_url: str = ""
    input_api_token: str = ""
    input_api_timeout_seconds: float = 10.0

    # Hackloop Mailbox API & Polling
    hackloop_base_url: str = "https://api.start-hack.5u.ai"
    hackloop_api_key: str = ""
    hackloop_mailbox: str = ""
    mailbox_poll_enabled: bool = True
    mailbox_poll_interval_seconds: float = 5.0  # platform limit: one poll per 5 seconds
    mailbox_poll_limit: int = 100
    mailbox_timeout_seconds: float = 10.0

    # SQLite persistence for since_id cursor, deduplication, and results
    sqlite_db_path: str = "data/router.db"

    # Quote workflow (docs/workflow.md section 9)
    send_enabled: bool = False  # dry run: compose and log, never send or book
    mail_signature: str = "Alex, TALOS Air Freight"
    # Printed between two divider lines below every mail to a customer; empty = no footer.
    # The phone number is from the range the Bundesnetzagentur reserves for fiction
    # (089 99998 000-999): it looks real and reaches nobody. Put a real one in .env.
    mail_footer: str = "TALOS Air Freight\nTheresienstraße 90, 80333 München\nPhone: +49 89 99998 417"
    margin_eur: float = 0.0
    # Quote when every asked trucker has answered, or after this many seconds with whatever
    # came back (nothing came back: fallback trucker price).
    trucker_wait_seconds: float = 60.0
    # A trucker that picks up within this many hours counts as in time for the flight; the
    # cheapest of those is used (ADR 49). Fitted to the board's accuracy points, not documented.
    trucker_pickup_cutoff_hours: float = 14.0
    fallback_truck_eur_per_kg: float = 0.28
    booking_reminder_seconds: float = 600.0  # email-booked airlines: remind after 10 min
    max_clarifications: int = 3
    max_booking_attempts: int = 3
    max_message_attempts: int = 3
    max_answers: int = 3
    start_kit_dir: str = "../data/start-kit"
    tariffs_path: str = "../data/tariffs.json"
    portal_rate_limit_per_minute: int = 10
    portal_timeout_seconds: float = 10.0
    portal_unavailable_retry_seconds: float = 60.0
    portal_lanes_cache_path: str = "data/portal_lanes.json"  # a 503 is retried once after this (ADR 47)
    # Email-only airlines (ADR 47): quote after this wait with whatever prices came back.
    airline_wait_seconds: float = 60.0


def as_test_settings(settings: Settings) -> Settings:
    """The same settings, made safe for a test run (ADR 39, ADR 40).

    - LLM calls go straight to OpenAI with EVALUATION_KEY, never through the scored gateway
    - nothing is sent and nothing is booked
    - a separate database, so test shipments never mix with production ones
    - portal rate reads are throttled

    Raises instead of falling back to the gateway when the evaluation key is missing.
    """
    if not settings.evaluation_key:
        raise RuntimeError("EVALUATION_KEY is not set; refusing to run a test on the gateway.")
    if settings.evaluation_api_base_url.startswith(settings.hackloop_base_url):
        raise RuntimeError("EVALUATION_API_BASE_URL points at the challenge gateway.")
    return settings.model_copy(
        update={
            "decision_api_key": settings.evaluation_key,
            "decision_api_base_url": settings.evaluation_api_base_url,
            "send_enabled": False,
            "sqlite_db_path": settings.test_sqlite_db_path,
            "portal_rate_limit_per_minute": min(
                settings.portal_rate_limit_per_minute, settings.test_portal_rate_limit_per_minute
            ),
        }
    )


def get_settings() -> Settings:
    """Settings from the environment; in TEST_MODE they are the test-safe variant."""
    settings = Settings()
    return as_test_settings(settings) if settings.test_mode else settings


def evaluation_settings() -> Settings:
    """Settings for eval, replay and sample scripts: always the test-safe variant."""
    return as_test_settings(Settings())


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
