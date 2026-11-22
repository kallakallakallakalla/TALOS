"""The CRM: the supervisor console over everything the router stores.

`GET /crm` serves the interface (plain HTML, CSS and JavaScript from `app/crm_ui/`, no build
step, no external requests). The `/crm/api/*` endpoints return the figures of
`app/insights.py`; they only read. The one exception is `POST /crm/api/actions/{name}`,
which carries out an operator action (`app/actions.py`) and is guarded below.
"""

import asyncio
import json
import subprocess
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal
from urllib.parse import urlsplit

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import FileResponse, HTMLResponse, StreamingResponse

from app import actions, board, insights, prompts
from app.config import Settings, get_settings
from app.reference import Reference
from app.replay_data import RECORDED, REPLAY_DIR, replay_labels, replay_store
from app.storage import MessageStore
from app.tariffs import Tariffs

UI_DIR = Path(__file__).parent / "crm_ui"
_STARTED_AT = datetime.now(UTC)  # when this process loaded the app: the start of its uptime


def _build() -> str:
    """Which version of the code this process runs: the BUILD file of a packed copy, else the git commit."""
    root = Path(__file__).parent.parent
    try:
        if (root / "BUILD").exists():
            return (root / "BUILD").read_text().strip()[:40]
        done = subprocess.run(["git", "log", "-1", "--format=%h %cs"], cwd=root, capture_output=True, text=True, timeout=3, check=True)
        return done.stdout.strip()[:40] or "unknown"
    except Exception:  # noqa: BLE001 - no git, no file: the build is simply not known
        return "unknown"


_BUILD = _build()
router = APIRouter(prefix="/crm", include_in_schema=False)
Source = Literal["live", "replay"]


def _live_store(request: Request, settings: Settings) -> MessageStore:
    store = getattr(request.app.state, "store", None)
    if store is None:
        store = request.app.state.store = MessageStore(settings.sqlite_db_path)
    return store


def _store(request: Request, settings: Settings, source: Source) -> MessageStore:
    if source == "live":
        return _live_store(request, settings)
    store = replay_store()
    if store is None:
        raise HTTPException(status_code=404, detail="No dry run recorded: run scripts/replay.py")
    return store


def _reference(settings: Settings, source: Source = "live") -> Reference:
    """Carriers and zones: the live ones from the router's database, which update mails
    change (ADR 50); for the dry run, the start kit as it was."""
    return Reference(settings.start_kit_dir, settings.sqlite_db_path if source == "live" else None)


def _tariffs(settings: Settings, source: Source = "live") -> Tariffs:
    return Tariffs(settings.tariffs_path, settings.sqlite_db_path if source == "live" else None)


@router.get("")
async def page() -> FileResponse:
    return FileResponse(UI_DIR / "index.html", headers={"Cache-Control": "no-cache"})


@router.get("/slop")
async def slop_page() -> HTMLResponse:
    """A joke, on request: the same real data in the worst possible taste (slop.css, slop.js)."""
    html = (UI_DIR / "index.html").read_text(encoding="utf-8")
    html = html.replace("</head>", '  <link rel="stylesheet" href="/crm/assets/slop.css">\n</head>')
    html = html.replace("</body>", '  <script type="module" src="/crm/assets/slop.js"></script>\n</body>')
    return HTMLResponse(html, headers={"Cache-Control": "no-cache"})


@router.get("/api/sources")
async def sources(request: Request, settings: Settings = Depends(get_settings)) -> dict[str, Any]:
    """Which data can be shown, and which to open first: live once it has priced shipments."""
    live = _live_store(request, settings).list_shipments()
    live_real = [s for s in live if not insights.is_legacy(s)]
    replay = replay_store()
    return {
        "live": {"shipments": len(live_real), "legacy": len(live) - len(live_real)},
        "replay": {"available": replay is not None, "shipments": len(replay.list_shipments()) if replay else 0},
        "default": "live" if live_real or not replay else "replay",
    }


def _poller_state(request: Request, settings: Settings) -> dict[str, Any]:
    poller = getattr(request.app.state, "poller", None)
    return {
        "enabled": settings.mailbox_poll_enabled and bool(settings.hackloop_api_key),
        "running": bool(poller and poller.running),
        "last_poll_at": poller.last_poll_at if poller else None,
        "last_error": poller.last_error if poller else None,
    }


def _health(request: Request, settings: Settings, source: Source) -> dict[str, Any]:
    """The status bar: this process (uptime, mailbox polling) and the data set shown (requests, bookings, mail)."""
    poller, state = getattr(request.app.state, "poller", None), _poller_state(request, settings)
    base = {
        "build": _BUILD,
        "started_at": _STARTED_AT.isoformat(),
        "uptime_s": round((datetime.now(UTC) - _STARTED_AT).total_seconds()),
        "mode": "test" if settings.test_mode else "sending" if settings.send_enabled else "dry_run",
    }
    try:
        recent = _store(request, settings, source).recent(shipments=insights.HEALTH_REQUESTS, mails=insights.HEALTH_MAILS)
    except Exception as error:  # noqa: BLE001 - a broken database is exactly what the bar has to show
        return base | {"state": "down", "label": f"Database not readable: {type(error).__name__}", "strips": []}
    strips = insights.health(recent, list(poller.polls) if poller else [], state, now=datetime.now(UTC))
    if not state["enabled"]:
        overall = ("viewer", "Read-only view")  # this process does not read the mailbox
    elif not state["running"]:
        overall = ("down", "Mailbox poller stopped")
    elif state["last_error"]:
        overall = ("degraded", "Last mailbox poll failed")
    else:
        overall = ("operational", "Operational")
    return base | {"state": overall[0], "label": overall[1], "detail": state["last_error"], "strips": strips}


@router.get("/api/version")
async def version(request: Request, source: Source = "live", settings: Settings = Depends(get_settings)) -> dict[str, Any]:
    """A token that changes whenever the data does; the page polls it to stay live.

    The same answer carries the health of this process, so the status bar costs no request of its own.
    """
    return {"version": _store(request, settings, source).change_token(), "health": _health(request, settings, source)}


@router.get("/api/snapshot")
async def snapshot(request: Request, source: Source = "live", settings: Settings = Depends(get_settings)) -> dict[str, Any]:
    store = _store(request, settings, source)
    return {
        "version": store.change_token(),
        # buttons that change something are only offered where they are allowed (app/actions.py)
        "can_act": source == "live" and actions.may_act(getattr(request.app.state, "poller", None)),
        "sending": settings.send_enabled,
        **insights.snapshot(
            store,
            _reference(settings, source),
            settings,
            source=source,
            tariff_airlines=_tariffs(settings, source).airlines,
            labels=replay_labels() if source == "replay" else None,
            poller=_poller_state(request, settings),
        ),
    }


@router.get("/api/rounds")
async def rounds(request: Request, source: Source = "live", settings: Settings = Depends(get_settings)) -> dict[str, Any]:
    """The round ledger; with the platform's verdicts where the board can be read (live only)."""
    entries = await board.my_board(settings) if source == "live" else None
    ledger = insights.rounds(_store(request, settings, source), _reference(settings, source), settings, source=source, board=entries)
    matched = sum(row["board"] is not None for row in ledger["rounds"])
    if entries is not None and ledger["total"] and not matched:
        # the board answered, but under names this code does not know: say which, so they can be added
        ledger["board_note"] = (f"The board listed {board.seen['entries']} entries, none of which could be matched to our requests. "
                                f"Fields of an entry: {', '.join(board.seen['fields']) or 'none'}.")  # fmt: skip
    return ledger


@router.get("/api/sessions")
async def sessions(request: Request, source: Source = "live", settings: Settings = Depends(get_settings)) -> list[dict[str, Any]]:
    """Every request as a session, latest activity first."""
    return insights.sessions(_store(request, settings, source), _reference(settings, source), source=source)


@router.get("/api/sessions/{session_id}")
async def session(request: Request, session_id: int, source: Source = "live", settings: Settings = Depends(get_settings)) -> dict[str, Any]:
    events = insights.session_events(_store(request, settings, source), _reference(settings, source), session_id, source=source)
    if events is None:
        raise HTTPException(status_code=404, detail="No such session")
    return {"id": session_id, "events": events}


def _sse(kind: str, payload: Any) -> str:
    return f"event: {kind}\ndata: {json.dumps(payload)}\n\n"


@router.get("/api/stream")
async def stream(
    request: Request,
    source: Source = "live",
    session_id: int | None = Query(default=None, alias="session"),
    replay: bool = False,
    pause: float = Query(default=0.6, ge=0, le=3),
    settings: Settings = Depends(get_settings),
) -> StreamingResponse:
    """Session events pushed to the page as server-sent events (`event: step`).

    Without `replay` the stream follows the data set: whenever something is stored, the events
    that are new since the connection opened are sent. This works in any process that can read
    the database, so a second process follows a running router the same way. With `replay` and
    a session, that session's events are sent one by one, `pause` seconds apart, then `done`.
    """
    store, reference = _store(request, settings, source), _reference(settings, source)

    async def replayed() -> AsyncIterator[str]:
        events = insights.session_events(store, reference, session_id, source=source) or []
        for position, event in enumerate(events):
            if position:
                await asyncio.sleep(pause)
            if await request.is_disconnected():
                return
            yield _sse("step", event)
        yield _sse("done", {"events": len(events)})

    async def followed() -> AsyncIterator[str]:
        seen = {event["id"]: event["title"] for event in insights.recent_events(store, reference, source=source)}
        token, quiet = store.change_token(), 0
        yield _sse("ready", {"watching": len(seen)})
        while not await request.is_disconnected():
            await asyncio.sleep(1.0)
            current = store.change_token()
            if current == token:
                quiet += 1
                if quiet % 15 == 0:
                    yield ": keep-alive\n\n"  # a comment line, so proxies keep the connection open
                continue
            token = current
            for event in insights.recent_events(store, reference, source=source):
                # new, or known and now saying more (an offer whose price has been read since it arrived)
                if seen.get(event["id"]) != event["title"]:
                    seen[event["id"]] = event["title"]
                    yield _sse("step", event)

    return StreamingResponse(
        replayed() if replay and session_id is not None else followed(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@router.get("/api/scorecard")
async def scorecard(request: Request, source: Source = "live", settings: Settings = Depends(get_settings)) -> dict[str, Any]:
    return insights.scorecard(
        _store(request, settings, source), _reference(settings, source), settings,
        source=source, tariff_airlines=_tariffs(settings, source).airlines,
    )  # fmt: skip


_LOCAL_HOSTS = {"localhost", "127.0.0.1", "::1"}


def _only_from_this_page(request: Request, settings: Settings) -> None:
    """Refuse an action that does not come from the CRM page itself.

    There is no login, and an action can book and send. Without these checks any web page
    open in the same browser could post one to localhost:
    - the body must be JSON, which a foreign page cannot send without a preflight this app
      never answers;
    - a stated origin must be the host the request was sent to;
    - that host must be localhost or one named in CRM_ACTION_HOSTS, so a foreign domain
      pointed at this machine is refused too.
    """
    if request.headers.get("content-type", "").split(";")[0].strip().lower() != "application/json":
        raise HTTPException(status_code=415, detail="Actions are sent as application/json.")
    host = request.headers.get("host", "")
    origin = request.headers.get("origin")
    if (origin and urlsplit(origin).netloc != host) or request.headers.get("sec-fetch-site", "same-origin") not in ("same-origin", "none"):
        raise HTTPException(status_code=403, detail="Actions are only accepted from the CRM page itself.")
    allowed = _LOCAL_HOSTS | {name.strip().lower() for name in settings.crm_action_hosts.split(",") if name.strip()}
    if (urlsplit(f"//{host}").hostname or "") not in allowed:
        raise HTTPException(status_code=403, detail=f"Actions are not enabled for the address {host}. Open the CRM on localhost, or add the host name to CRM_ACTION_HOSTS.")


@router.post("/api/actions/{name}")
async def act(name: str, request: Request, source: Source = "live", settings: Settings = Depends(get_settings)) -> dict[str, Any]:
    """Carry out an operator action (app/actions.py).

    Dismissing and restoring apply to the data set that is shown; everything else changes
    the live system and is refused for the dry run.
    """
    _only_from_this_page(request, settings)
    try:
        params = await request.json() if await request.body() else {}
    except ValueError as error:
        raise HTTPException(status_code=400, detail="The request body is not valid JSON.") from error
    if not isinstance(params, dict):
        raise HTTPException(status_code=400, detail="The request body must be a JSON object.")
    try:
        if source != "live" and name not in actions.ANYWHERE:
            raise actions.ActionError("The dry run is a recording: this action only exists on the live router.")
        message = await actions.run(
            name, store=_store(request, settings, source), poller=getattr(request.app.state, "poller", None), params=params
        )
    except actions.ActionError as error:
        raise HTTPException(status_code=409, detail=str(error)) from error
    return {"ok": True, "message": message}


@router.get("/api/shipments/{shipment_id}")
async def shipment(
    request: Request, shipment_id: int, source: Source = "live", settings: Settings = Depends(get_settings)
) -> dict[str, Any]:
    detail = insights.shipment_detail(
        _store(request, settings, source), _reference(settings, source), shipment_id, source=source
    )
    if detail is None:
        raise HTTPException(status_code=404, detail=f"Shipment {shipment_id} not found")
    return detail


@router.get("/api/mails")
async def mails(
    request: Request,
    source: Source = "live",
    q: str = "",
    category: str = "",
    role: str = "",
    sender: str = "",
    offset: int = Query(default=0, ge=0),
    limit: int = Query(default=100, ge=1, le=500),
    settings: Settings = Depends(get_settings),
) -> dict[str, Any]:
    return insights.mails(
        _store(request, settings, source), _reference(settings, source),
        query=q, category=category, role=role, sender=sender, offset=offset, limit=limit,
    )  # fmt: skip


@router.get("/api/tariffs")
async def tariffs(settings: Settings = Depends(get_settings)) -> list[dict[str, Any]]:
    """Every tariff lane in force: the start-kit files plus what tariff mails changed (ADR 50)."""
    return [lane.model_dump(mode="json") for lane in _tariffs(settings).all_lanes()]


@router.get("/api/system")
async def system(request: Request, settings: Settings = Depends(get_settings)) -> dict[str, Any]:
    """How the router is configured and what it holds. No keys, no secrets."""
    store = _live_store(request, settings)
    poller = getattr(request.app.state, "poller", None)
    data = insights.Dataset.load(store)
    tariff_lanes = len(_tariffs(settings).all_lanes())
    reference = _reference(settings)
    replay_path = REPLAY_DIR / RECORDED
    return {
        "mode": "test" if settings.test_mode else "sending" if settings.send_enabled else "dry_run",
        "poller": {
            "enabled": settings.mailbox_poll_enabled and bool(settings.hackloop_api_key),
            "running": bool(poller and poller.running),
            "interval_seconds": settings.mailbox_poll_interval_seconds,
            "last_poll_at": poller.last_poll_at if poller else None,
            "last_error": poller.last_error if poller else None,
            "cursor": store.get_since_id(),
        },
        "settings": [
            {"name": "Mailbox", "value": settings.hackloop_mailbox or "not set"},
            {"name": "Database", "value": settings.sqlite_db_path},
            {"name": "Classifier model", "value": f"{settings.decision_model} (reasoning: {settings.decision_reasoning_effort or 'default'})"},
            {"name": "Extractor model", "value": f"{settings.extract_model} (reasoning: {settings.extract_reasoning_effort or 'default'})"},
            {"name": "Trucker reply reader", "value": f"{settings.extract_model} (reasoning: {settings.trucker_reasoning_effort or 'default'})"},
            {"name": "Composer model", "value": f"{settings.compose_model} (reasoning: {settings.compose_reasoning_effort or 'default'})"},
            {"name": "Prompt label", "value": settings.prompt_label},
            {"name": "Prompt source", "value": "Langfuse, local files as fallback" if settings.langfuse_enabled and settings.langfuse_public_key else "local files"},
            {"name": "Margin", "value": f"{settings.margin_eur:.2f} EUR"},
            {"name": "Trucker wait", "value": f"{settings.trucker_wait_seconds:.0f} s"},
            {"name": "Fallback trucker price", "value": f"{settings.fallback_truck_eur_per_kg:.2f} EUR per kg"},
            {"name": "Portal rate reads", "value": f"{settings.portal_rate_limit_per_minute} per minute"},
            {"name": "Booking reminder", "value": f"{settings.booking_reminder_seconds:.0f} s"},
            {"name": "Limits", "value": f"{settings.max_clarifications} clarifications, {settings.max_booking_attempts} booking attempts, {settings.max_message_attempts} tries per mail, {settings.max_answers} answers"},
            {"name": "Mail signature", "value": settings.mail_signature},
        ],
        "inventory": [
            {"name": "Mails received", "rows": len(data.mails), "where": "live database"},
            {"name": "Shipments", "rows": len(data.shipments), "where": "live database"},
            {"name": "Air options priced", "rows": sum(len(v) for v in data.options.values()), "where": "live database"},
            {"name": "Trucker requests", "rows": sum(len(v) for v in data.truckers.values()), "where": "live database"},
            {"name": "Mails composed", "rows": sum(len(v) for v in data.outbound.values()), "where": "live database"},
            {"name": "Mails that failed processing", "rows": len(data.failures), "where": "live database"},
            {"name": "Airlines", "rows": sum(c.kind == "airline" for c in reference.carriers), "where": "live database, seeded from the start kit"},
            {"name": "Truckers", "rows": sum(c.kind == "trucker" for c in reference.carriers), "where": "live database, seeded from the start kit"},
            {"name": "Pickup zones", "rows": len(reference.zones), "where": "live database, seeded from the start kit"},
            {"name": "Tariff lanes", "rows": tariff_lanes, "where": "live database, seeded from data/tariffs.json"},
            {"name": "Dry-run shipments", "rows": len(json.loads(replay_path.read_text())["shipments"]) if replay_path.exists() else 0, "where": f"data/replay/{RECORDED}"},
        ],
        "prompts": [{"name": name, "text": prompts.local_prompt(settings, name)} for name in prompts.ALL_PROMPTS],
    }
