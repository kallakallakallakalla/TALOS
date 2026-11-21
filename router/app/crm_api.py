"""The CRM: a read-only web interface over everything the router stores.

`GET /crm` serves the interface (plain HTML, CSS and JavaScript from `app/crm_ui/`, no build
step, no external requests). The `/crm/api/*` endpoints return the figures of
`app/insights.py`. Nothing here sends mail, books, or calls an LLM.
"""

import json
from pathlib import Path
from typing import Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import FileResponse, HTMLResponse

from app import insights, prompts
from app.config import Settings, get_settings
from app.reference import Reference
from app.replay_data import RECORDED, REPLAY_DIR, replay_labels, replay_store
from app.storage import MessageStore
from app.tariffs import Tariffs

UI_DIR = Path(__file__).parent / "crm_ui"
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


@router.get("/api/version")
async def version(request: Request, source: Source = "live", settings: Settings = Depends(get_settings)) -> dict[str, str]:
    """A token that changes whenever the data does; the page polls it to stay live."""
    return {"version": _store(request, settings, source).change_token()}


@router.get("/api/snapshot")
async def snapshot(request: Request, source: Source = "live", settings: Settings = Depends(get_settings)) -> dict[str, Any]:
    store = _store(request, settings, source)
    return {
        "version": store.change_token(),
        **insights.snapshot(
            store,
            _reference(settings, source),
            settings,
            source=source,
            tariff_airlines=_tariffs(settings, source).airlines,
            labels=replay_labels() if source == "replay" else None,
        ),
    }


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
