"""Our verdicts on the platform's board (`GET /v1/board/me`), for the CRM's round ledger.

The board says, per request, whether the customer accepted, why not, and whether the booking
counted. The shape of the answer is not documented and could not be read while this was
written, so it is taken apart tolerantly: a list of entries, or an object holding one, with
the request id, verdict, reason and booked flag under their likely names. If the board cannot
be read or understood, the ledger works from our own records alone.
"""

import logging
import time
from typing import Any

import httpx

from app.config import Settings

logger = logging.getLogger(__name__)
_CACHE_SECONDS = 30.0
_cache: dict[str, tuple[float, dict[int, dict[str, Any]] | None]] = {}


def _first(entry: dict[str, Any], *names: str) -> Any:
    return next((entry[name] for name in names if entry.get(name) is not None), None)


def normalise(payload: Any) -> dict[int, dict[str, Any]]:
    """`request id -> {verdict, reason, booked, points}` from whatever the board returned."""
    entries = payload
    if isinstance(payload, dict):
        entries = next((v for v in payload.values() if isinstance(v, list) and v and isinstance(v[0], dict)), [])
    board: dict[int, dict[str, Any]] = {}
    for entry in entries if isinstance(entries, list) else []:
        if not isinstance(entry, dict):
            continue
        request_id = _first(entry, "request_id", "request", "rfq_id", "message_id", "team_reference", "id")
        try:
            request_id = int(str(request_id).split("-")[1] if str(request_id).startswith("TALOS-") else request_id)
        except (TypeError, ValueError, IndexError):
            continue
        board[request_id] = {
            "verdict": _first(entry, "verdict", "status", "result", "outcome"),
            "reason": _first(entry, "reason", "reject_reason", "detail"),
            "booked": _first(entry, "booked", "is_booked"),
            "points": _first(entry, "points", "score", "total"),
        }
    return board


async def my_board(settings: Settings, transport: httpx.AsyncBaseTransport | None = None) -> dict[int, dict[str, Any]] | None:
    """The board entries by request id, cached for half a minute; None if not available."""
    if not settings.hackloop_api_key:
        return None
    key = settings.hackloop_base_url
    cached = _cache.get(key)
    if cached and time.monotonic() - cached[0] < _CACHE_SECONDS and transport is None:
        return cached[1]
    board: dict[int, dict[str, Any]] | None
    try:
        async with httpx.AsyncClient(timeout=settings.mailbox_timeout_seconds, transport=transport) as client:
            response = await client.get(f"{settings.hackloop_base_url}/v1/board/me", headers={"X-API-Key": settings.hackloop_api_key})
            response.raise_for_status()
            board = normalise(response.json()) or None
    except Exception as exc:  # noqa: BLE001 - the ledger must work without the board
        logger.info("Board not available: %s", str(exc).splitlines()[0])
        board = None
    _cache[key] = (time.monotonic(), board)
    return board
