"""Our verdicts on the platform's board (`GET /v1/board/me`), for the CRM's round ledger.

The board says, per request, whether the customer accepted, why not, the points per kind and
whether the booking counted. The platform documents the answer only as free-form JSON
("round, sent and answered time, verdict, reason code, points per kind, booked yes/no and
the request total, plus your own bookings"), and it could not be read while this was
written. So it is taken apart tolerantly: the list whose entries carry a verdict, with the
request id, verdict, reason, booked flag and points under their likely names. If the board
cannot be read or understood, the ledger works from our own records alone, and `seen` says
which fields the board did send, so the names can be corrected.
"""

import logging
import time
from typing import Any

import httpx

from app.config import Settings

logger = logging.getLogger(__name__)
_CACHE_SECONDS = 30.0
_cache: dict[str, tuple[float, dict[int, dict[str, Any]] | None]] = {}
_VERDICT_KEYS = ("verdict", "status", "result", "outcome")
# What the last answer looked like: how many entries, how many had a usable request id, and
# the field names of the first one. Shown in the CRM when nothing could be matched.
seen: dict[str, Any] = {"entries": 0, "usable": 0, "fields": []}


def _first(entry: dict[str, Any], *names: str) -> Any:
    return next((entry[name] for name in names if entry.get(name) is not None), None)


def normalise(payload: Any) -> dict[int, dict[str, Any]]:
    """`request id -> {verdict, reason, booked, points}` from whatever the board returned."""
    entries = payload
    if isinstance(payload, dict):
        lists = [v for v in payload.values() if isinstance(v, list) and v and isinstance(v[0], dict)]
        # the answer also lists our bookings: take the list whose entries carry a verdict
        entries = next((v for v in lists if any(key in v[0] for key in _VERDICT_KEYS)), lists[0] if lists else [])
    entries = [entry for entry in entries if isinstance(entry, dict)] if isinstance(entries, list) else []
    seen.update(entries=len(entries), usable=0, fields=sorted(entries[0]) if entries else [])
    board: dict[int, dict[str, Any]] = {}
    for entry in entries:
        request_id = _first(entry, "request_id", "request_message_id", "message_id", "mail_id", "rfq_id", "request", "team_reference", "id")
        try:
            request_id = int(str(request_id).split("-")[1] if str(request_id).startswith("TALOS-") else request_id)
        except (TypeError, ValueError, IndexError):
            continue
        points = _first(entry, "points", "points_per_kind", "scores", "breakdown")
        board[request_id] = {
            "verdict": _first(entry, *_VERDICT_KEYS),
            "reason": _first(entry, "reason", "reason_code", "reject_reason", "detail"),
            "booked": _first(entry, "booked", "is_booked"),
            # the request's total, and the points per kind where the board gives them
            "points": _first(entry, "total", "request_total", "score") if isinstance(points, dict) else points,
            "kinds": {str(kind): value for kind, value in points.items() if isinstance(value, int | float)} if isinstance(points, dict) else None,
        }
    seen["usable"] = len(board)
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
