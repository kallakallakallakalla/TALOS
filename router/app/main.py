import asyncio
import json
import logging
from typing import Literal
from pathlib import Path
from contextlib import asynccontextmanager, suppress

from fastapi import Depends, FastAPI, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from langfuse import get_client, observe

from app.classifier import DecisionClassifier
from app.composer import Composer
from app.config import Settings, configure_langfuse_env, get_settings
from app.extractors import Extractors
from app.input_client import InputApiClient
from app.llm import StructuredLLM
from app.mailbox_client import MailboxClient
from app.poller import MailboxPoller
from app.portal_client import PortalClient
from app.reference import Reference
from app.tariffs import Tariffs
from app.workflow import Workflow
from app.schemas import (
    ClassifyRequest,
    EmailCategory,
    InputPayload,
    MessageListResponse,
    PollerStatus,
    PollSummary,
    RouteResponse,
    RunRequest,
    Shipment,
    ShipmentDetail,
    ShipmentState,
    StoredMessageRecord,
)
from app.storage import MessageStore


def _configure_app_logging() -> None:
    app_logger = logging.getLogger("app")
    if not app_logger.handlers:
        handler = logging.StreamHandler()
        handler.setFormatter(logging.Formatter("%(levelname)s:     %(name)s - %(message)s"))
        app_logger.addHandler(handler)
        app_logger.setLevel(logging.INFO)
        app_logger.propagate = False
    logging.getLogger("httpx").setLevel(logging.WARNING)


def _resolve_override(app: FastAPI, dep, default_factory):
    provider = app.dependency_overrides.get(dep)
    return provider() if provider is not None else default_factory()


def build_workflow(
    settings: Settings, store: MessageStore, mailbox_client: MailboxClient
) -> Workflow:
    """Wire the quote workflow. All its LLM calls share one client and one prompt loader."""
    llm = StructuredLLM(settings)
    return Workflow(
        settings=settings,
        store=store,
        mailbox=mailbox_client,
        portal=PortalClient(settings),
        reference=Reference(settings.start_kit_dir),
        tariffs=Tariffs(settings.tariffs_path),
        extractors=Extractors(settings, llm),
        composer=Composer(settings, llm),
    )


@asynccontextmanager
async def lifespan(app: FastAPI):
    _configure_app_logging()
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
        workflow=build_workflow(settings, store, mailbox_client),
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
    version="0.2.0",
    description=(
        "Polls the Hackloop mailbox every 2 s without duplicates, "
        "classifies every mail, runs the quote workflow (price, quote, book), persists in SQLite "
        "and traces with Langfuse. With SEND_ENABLED=false it never sends or books."
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


def _get_or_create_state(request: Request, attr: str, factory):
    value = getattr(request.app.state, attr, None)
    if value is None:
        value = factory()
        setattr(request.app.state, attr, value)
    return value


def get_store(
    request: Request,
    settings: Settings = Depends(get_settings),
) -> MessageStore:
    return _get_or_create_state(request, "store", lambda: MessageStore(settings.sqlite_db_path))


_REPLAY_DIR = Path(__file__).parent.parent / "data/replay"


def get_view_store(
    source: Literal["live", "replay"] = "live",
    store: MessageStore = Depends(get_store),
) -> MessageStore:
    """Read-only views can show the live DB or the last dry-run replay (scripts/replay.py)."""
    if source == "live":
        return store
    if not (_REPLAY_DIR / "replay.db").exists():
        raise HTTPException(status_code=404, detail="No replay yet: run scripts/replay.py")
    return MessageStore(str(_REPLAY_DIR / "replay.db"))


def get_mailbox_client(
    request: Request,
    settings: Settings = Depends(get_settings),
) -> MailboxClient:
    return _get_or_create_state(request, "mailbox_client", lambda: MailboxClient(settings))


def get_poller(
    request: Request,
    settings: Settings = Depends(get_settings),
    mailbox_client: MailboxClient = Depends(get_mailbox_client),
    classifier: DecisionClassifier = Depends(get_classifier),
    store: MessageStore = Depends(get_store),
) -> MailboxPoller:
    return _get_or_create_state(
        request,
        "poller",
        lambda: MailboxPoller(
            settings=settings,
            mailbox_client=mailbox_client,
            classifier=classifier,
            store=store,
        ),
    )


@app.get("/health")
async def health(settings: Settings = Depends(get_settings)) -> dict[str, str]:
    return {"status": "ok", "sending": "on" if settings.send_enabled else "off"}


async def _run_or_502(coro):
    try:
        return await coro
    except Exception as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc


async def _classify_payload(
    payload: InputPayload,
    classifier: DecisionClassifier,
    model_name: str,
) -> RouteResponse:
    decision, trace_id = await classifier.classify(payload)
    return RouteResponse(
        input=payload,
        decision=decision,
        model=model_name,
        trace_id=trace_id,
    )


@observe(name="router-run")
async def _execute_pipeline(
    input_id: str | None,
    input_client: InputApiClient,
    classifier: DecisionClassifier,
    model_name: str,
) -> RouteResponse:
    payload = await input_client.fetch_input(input_id=input_id)
    return await _classify_payload(payload, classifier, model_name)


@app.post("/run", response_model=RouteResponse)
async def run_router(
    request: RunRequest | None = None,
    settings: Settings = Depends(get_settings),
    input_client: InputApiClient = Depends(get_input_client),
    classifier: DecisionClassifier = Depends(get_classifier),
) -> RouteResponse:
    """Fetch an input from the (placeholder) Input API and classify it into Enums."""
    input_id = request.input_id if request else None
    return await _run_or_502(
        _execute_pipeline(
            input_id=input_id,
            input_client=input_client,
            classifier=classifier,
            model_name=settings.decision_model,
        )
    )


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
    return await _run_or_502(_classify_payload(payload, classifier, settings.decision_model))


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


@app.get("/shipments", response_model=list[Shipment])
async def list_shipments(
    state: ShipmentState | None = None,
    store: MessageStore = Depends(get_view_store),
) -> list[Shipment]:
    """List shipments (newest first), optionally only those in one state."""
    return store.list_shipments(state)


@app.get("/shipments/{shipment_id}", response_model=ShipmentDetail)
async def get_shipment(
    shipment_id: int,
    store: MessageStore = Depends(get_view_store),
) -> ShipmentDetail:
    """One shipment with its trucker requests, air options and composed mails."""
    shipment = store.get_shipment(shipment_id)
    if shipment is None:
        raise HTTPException(status_code=404, detail=f"Shipment {shipment_id} not found")
    return ShipmentDetail(
        shipment=shipment,
        trucker_requests=store.trucker_requests(shipment_id),
        air_options=store.air_options(shipment_id),
        outbound=store.outbound_mails(shipment_id),
        inbound=store.shipment_inbound(shipment_id),
    )


@app.get("/replay/classification", include_in_schema=False)
async def replay_classification() -> list[dict]:
    """Per-mail classifier result vs. the hand label from the last replay."""
    path = _REPLAY_DIR / "result.json"
    if not path.exists():
        raise HTTPException(status_code=404, detail="No replay yet: run scripts/replay.py")
    return json.loads(path.read_text())["classified"]


@app.get("/ui", include_in_schema=False)
async def ui() -> HTMLResponse:
    """Observability page: every shipment thread, inbound and outbound, in one view."""
    return HTMLResponse((Path(__file__).parent / "ui.html").read_text())
