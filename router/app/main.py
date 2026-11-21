import asyncio
from contextlib import asynccontextmanager, suppress

from fastapi import Depends, FastAPI, HTTPException, Query, Request
from fastapi.responses import RedirectResponse
from langfuse import get_client, observe

from app.classifier import DecisionClassifier
from app.config import Settings, configure_langfuse_env, get_settings
from app.input_client import InputApiClient
from app.mailbox_client import MailboxClient
from app.poller import MailboxPoller
from app.schemas import (
    ClassifyRequest,
    EmailCategory,
    InputPayload,
    MessageListResponse,
    PollerStatus,
    PollSummary,
    RouteResponse,
    RunRequest,
    StoredMessageRecord,
)
from app.storage import MessageStore


def _resolve_override(app: FastAPI, dep, default_factory):
    provider = app.dependency_overrides.get(dep)
    return provider() if provider is not None else default_factory()


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings: Settings = _resolve_override(app, get_settings, get_settings)
    configure_langfuse_env(settings)

    store: MessageStore = _resolve_override(
        app, get_store, lambda: MessageStore(settings.sqlite_db_path)
    )
    mailbox_client: MailboxClient = _resolve_override(
        app, get_mailbox_client, lambda: MailboxClient(settings)
    )
    classifier: DecisionClassifier = _resolve_override(
        app, get_classifier, lambda: DecisionClassifier(settings)
    )
    poller = MailboxPoller(
        settings=settings,
        mailbox_client=mailbox_client,
        classifier=classifier,
        store=store,
    )

    app.state.store = store
    app.state.mailbox_client = mailbox_client
    app.state.poller = poller

    poller_task: asyncio.Task[None] | None = None
    if settings.mailbox_poll_enabled and settings.hackloop_api_key:
        poller_task = asyncio.create_task(poller.run_forever())

    try:
        yield
    finally:
        if poller_task is not None:
            poller.stop()
            with suppress(asyncio.CancelledError):
                await poller_task
        get_client().flush()


app = FastAPI(
    title="TALOS Router MVP",
    version="0.1.0",
    description=(
        "Polls the Hackloop mailbox every 10s without duplicates, "
        "classifies mails via gpt-6-luna, persists results in SQLite, and traces with Langfuse."
    ),
    docs_url="/docs",
    redoc_url="/redoc",
    openapi_url="/openapi.json",
    lifespan=lifespan,
)


@app.get("/", include_in_schema=False)
async def root_redirect() -> RedirectResponse:
    return RedirectResponse(url="/docs")


def get_input_client(settings: Settings = Depends(get_settings)) -> InputApiClient:
    return InputApiClient(settings)


def get_classifier(settings: Settings = Depends(get_settings)) -> DecisionClassifier:
    return DecisionClassifier(settings)


def get_store(
    request: Request,
    settings: Settings = Depends(get_settings),
) -> MessageStore:
    store = getattr(request.app.state, "store", None)
    if store is None:
        store = MessageStore(settings.sqlite_db_path)
        request.app.state.store = store
    return store


def get_mailbox_client(
    request: Request,
    settings: Settings = Depends(get_settings),
) -> MailboxClient:
    client = getattr(request.app.state, "mailbox_client", None)
    if client is None:
        client = MailboxClient(settings)
        request.app.state.mailbox_client = client
    return client


def get_poller(
    request: Request,
    settings: Settings = Depends(get_settings),
    mailbox_client: MailboxClient = Depends(get_mailbox_client),
    classifier: DecisionClassifier = Depends(get_classifier),
    store: MessageStore = Depends(get_store),
) -> MailboxPoller:
    poller = getattr(request.app.state, "poller", None)
    if poller is None:
        poller = MailboxPoller(
            settings=settings,
            mailbox_client=mailbox_client,
            classifier=classifier,
            store=store,
        )
        request.app.state.poller = poller
    return poller


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}


@observe(name="router-run")
async def _execute_pipeline(
    input_id: str | None,
    input_client: InputApiClient,
    classifier: DecisionClassifier,
    model_name: str,
) -> RouteResponse:
    payload = await input_client.fetch_input(input_id=input_id)
    decision, trace_id = await classifier.classify(payload)
    return RouteResponse(
        input=payload,
        decision=decision,
        model=model_name,
        trace_id=trace_id,
    )


@app.post("/run", response_model=RouteResponse)
async def run_router(
    request: RunRequest | None = None,
    settings: Settings = Depends(get_settings),
    input_client: InputApiClient = Depends(get_input_client),
    classifier: DecisionClassifier = Depends(get_classifier),
) -> RouteResponse:
    """Fetch an input from the (placeholder) Input API and classify it into Enums."""
    input_id = request.input_id if request else None
    try:
        return await _execute_pipeline(
            input_id=input_id,
            input_client=input_client,
            classifier=classifier,
            model_name=settings.decision_model,
        )
    except Exception as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc


@app.post("/classify", response_model=RouteResponse)
async def classify_direct(
    request: ClassifyRequest,
    settings: Settings = Depends(get_settings),
    classifier: DecisionClassifier = Depends(get_classifier),
) -> RouteResponse:
    """Classify a directly provided text payload into multiple Enums."""
    payload = InputPayload(
        id=request.id,
        source=request.source,
        text=request.text,
        metadata=request.metadata,
    )
    try:
        decision, trace_id = await classifier.classify(payload)
        return RouteResponse(
            input=payload,
            decision=decision,
            model=settings.decision_model,
            trace_id=trace_id,
        )
    except Exception as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc


@app.post("/poll", response_model=PollSummary)
async def trigger_poll(
    poller: MailboxPoller = Depends(get_poller),
) -> PollSummary:
    """Trigger one guarded mailbox poll cycle manually."""
    try:
        return await poller.poll_once()
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc


@app.get("/poller/status", response_model=PollerStatus)
async def poller_status(
    settings: Settings = Depends(get_settings),
    store: MessageStore = Depends(get_store),
    poller: MailboxPoller = Depends(get_poller),
) -> PollerStatus:
    """Inspect background poller status, persisted cursor, and processed mail count."""
    return PollerStatus(
        enabled=settings.mailbox_poll_enabled and bool(settings.hackloop_api_key),
        running=poller.running,
        interval_seconds=settings.mailbox_poll_interval_seconds,
        last_since_id=store.get_since_id(),
        processed_count=store.count_messages(),
        last_poll_at=poller.last_poll_at,
        last_error=poller.last_error,
    )


@app.get("/messages", response_model=MessageListResponse)
async def list_messages(
    category: EmailCategory | None = None,
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    store: MessageStore = Depends(get_store),
) -> MessageListResponse:
    """List persisted, categorized mailbox messages (newest first)."""
    items, total = store.list_messages(category=category, limit=limit, offset=offset)
    return MessageListResponse(
        items=items,
        total=total,
        last_since_id=store.get_since_id(),
    )


@app.get("/messages/{message_id}", response_model=StoredMessageRecord)
async def get_message(
    message_id: int,
    store: MessageStore = Depends(get_store),
) -> StoredMessageRecord:
    """Return a single persisted, categorized mailbox message by ID."""
    record = store.get_message(message_id)
    if record is None:
        raise HTTPException(status_code=404, detail=f"Message {message_id} not found")
    return record
