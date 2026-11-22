"""The CRM: a read-only console over what the ultra-talos desk has done (ADR 58).

Its own small server, independent of the router: `uvicorn app:app` in this folder (see crm.sh).
`GET /crm` serves the interface (plain HTML, CSS and JavaScript from `ui/`, no build step, no
external requests). The `/crm/api/*` endpoints return the figures of `desk.py`. Every endpoint
only reads; there is no action of any kind.
"""

import logging
import re
import subprocess
from pathlib import Path
from typing import Any

from fastapi import APIRouter, FastAPI, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles

import desk

UI_DIR = Path(__file__).parent / "ui"


def _build() -> str:
    """Which version of the code this process runs: the BUILD file of a packed copy, else the git commit."""
    root = Path(__file__).parent
    try:
        if (root / "BUILD").exists():
            return (root / "BUILD").read_text().strip()[:40]
        done = subprocess.run(["git", "log", "-1", "--format=%h %cs"], cwd=root, capture_output=True, text=True, timeout=3, check=True)
        return done.stdout.strip()[:40] or "unknown"
    except Exception:  # noqa: BLE001 - no git, no file: the build is simply not known
        return "unknown"


_BUILD = _build()
# The page's scripts and styles are served under a path that carries the build, so a browser
# that holds another build's files in its cache can never run them against this page.
ASSETS = f"/crm/b/{re.sub(r'[^A-Za-z0-9]', '', _BUILD.split()[0]) or 'dev'}"


def _page_html() -> str:
    return (UI_DIR / "index.html").read_text().replace("/crm/assets/", f"{ASSETS}/")


router = APIRouter(prefix="/crm", include_in_schema=False)


def _desk() -> desk.Desk:
    if not desk.ROOT.exists():
        raise HTTPException(status_code=404, detail=f"No ultra-talos folder at {desk.ROOT}. Set ULTRA_TALOS_DIR.")
    return desk.load(desk.ROOT)


@router.get("")
async def page() -> HTMLResponse:
    return HTMLResponse(_page_html(), headers={"Cache-Control": "no-cache"})


@router.get("/slop")
async def slop_page() -> HTMLResponse:
    """A joke, on request: the same real data in the worst possible taste (slop.css, slop.js)."""
    html = _page_html()
    html = html.replace("</head>", f'  <link rel="stylesheet" href="{ASSETS}/slop.css">\n</head>')
    html = html.replace("</body>", f'  <script type="module" src="{ASSETS}/slop.js"></script>\n</body>')
    return HTMLResponse(html, headers={"Cache-Control": "no-cache"})


@router.get("/api/version")
async def version() -> dict[str, Any]:
    """A token that moves whenever the desk's files do, with the desk's state; the page polls it every 2 s."""
    data = _desk()
    return {"version": desk.token(desk.ROOT), "desk": desk.desk_status(data), "build": _BUILD}


@router.get("/api/snapshot")
async def snapshot() -> dict[str, Any]:
    """Everything the list pages show. Small: one row per shipment, customer and carrier."""
    data = _desk()
    return {
        "version": desk.token(desk.ROOT),
        "loaded_at": data.loaded_at.isoformat(),
        "desk": desk.desk_status(data),
        "today": desk.today(data),
        "shipments": sorted(data.shipments.values(), key=lambda s: s["last_at"] or "", reverse=True),
        "customers": desk.customers(data),
        "carriers": desk.carriers(data),
        "reports": desk.reports(data),
        "strips": desk.strips(data),
        "globe": desk.globe(data),
    }


@router.get("/api/shipments/{shipment_id}")
async def shipment(shipment_id: str) -> dict[str, Any]:
    data = _desk()
    if shipment_id not in data.shipments:
        raise HTTPException(status_code=404, detail=f"No shipment {shipment_id}")
    return data.shipments[shipment_id] | {"timeline": desk.timeline(data, shipment_id), "notes": desk.notes(shipment_id, desk.ROOT)}


@router.get("/api/rates")
async def rates() -> dict[str, Any]:
    data = _desk()
    return {"lanes": data.lanes, "zones": data.zones}


@router.get("/api/search")
async def search(q: str = Query(default="", max_length=200)) -> dict[str, Any]:
    """Mails whose id, sender, subject or text contain the query; shipments and customers are searched in the page."""
    return {"mails": desk.search_mails(_desk(), q)}


@router.get("/api/settings")
async def settings() -> dict[str, Any]:
    """Where the figures come from and how fresh each source is."""
    data = _desk()
    root = desk.ROOT

    def source(name: str, path: Path, rows: int, note: str) -> dict[str, Any]:
        return {"name": name, "path": str(path.relative_to(root.parent)) if path.is_relative_to(root.parent) else str(path), "rows": rows, "note": note}

    return {
        "root": str(root),
        "build": _BUILD,
        "desk": desk.desk_status(data),
        "needs_human": data.needs_human,
        "dry_until": data.dry_until.isoformat() if data.dry_until else None,
        "dry_hidden": data.dry_hidden,
        "sources": [
            source("Inbound mail", root / "mails", len(data.mails), "every mail with its classification and shipment"),
            source("Sent mail", root / "logs", len(data.sent), "every mail we sent, as the platform stored it (from the desk's tool calls)"),
            source("Bookings", root / "logs", len(data.bookings), "bookings and cancellations as the platform answered them"),
            source("Desk board", root / "state" / "board.db", len(data.events), "the desk's decisions and sessions (from 22 Nov 01:56)"),
            source("Shipment notes", root / "shipments", sum(s["has_notes"] for s in data.shipments.values()), "customer, lane and quoted price, as the desk wrote them"),
            source("Carriers", root / "data" / "carriers.csv", len(data.carriers), "airlines and truckers"),
            source("Tariff lanes", root / "data" / "tariff_lanes.csv", len(data.lanes), "airline rates by lane"),
            source("Pickup zones", root / "data" / "pickup_zones.csv", len(data.zones), "postcode zone to airport"),
        ],
    }


# --- the server ----------------------------------------------------------------------------------

app = FastAPI(title="TALOS CRM", docs_url=None, redoc_url=None, openapi_url=None)
app.include_router(router)


@app.exception_handler(Exception)
async def unexpected_error(request: Request, exc: Exception) -> JSONResponse:
    """Any unhandled error: name it in the response, so the page can show the cause (the traceback goes to the log)."""
    logging.getLogger("crm").exception("Unhandled error on %s", request.url.path)
    return JSONResponse(status_code=500, content={"detail": f"{type(exc).__name__}: {exc}"})


class _FreshFiles(StaticFiles):
    """The page's scripts and styles, checked with the server on every load, so that after an
    update no browser keeps running yesterday's script against today's data."""

    def file_response(self, *args, **kwargs):
        response = super().file_response(*args, **kwargs)
        response.headers["Cache-Control"] = "no-cache"
        return response


app.mount(ASSETS, _FreshFiles(directory=UI_DIR), name="crm-build-assets")  # what the page loads
app.mount("/crm/assets", _FreshFiles(directory=UI_DIR), name="crm-assets")  # the same files under their plain address


@app.get("/", include_in_schema=False)
async def root_redirect() -> RedirectResponse:
    return RedirectResponse(url="/crm")
