"""Static export of the CRM for Netlify (the netlify-deploy branch).

Netlify serves files and runs small JavaScript functions; it cannot run this Python server.
The CRM only reads, so every page it shows can be computed once at build time: this script
asks the real FastAPI app (app.py, through its TestClient) for every endpoint and writes the
answers as files next to the interface. The data is the ultra-talos snapshot in this
repository (state/board.db, mails/, logs/, shipments/, data/*.csv, state/NEEDS_HUMAN).

The mail search takes a free query, so it cannot be a file: netlify/functions/search.mjs
answers it from the mail index written here (the same matching as desk.search_mails).

    python crm/netlify_build.py [output folder]     # default: crm/dist

netlify.toml at the repository root runs it and maps the /crm/api/* addresses to the files.
"""

import functools
import json
import os
import shutil
import sys
from datetime import UTC, datetime
from pathlib import Path

from fastapi.testclient import TestClient

import app
import desk

HERE = Path(__file__).resolve().parent
OUT = Path(sys.argv[1]).resolve() if len(sys.argv) > 1 else HERE / "dist"
INDEX = HERE / ".netlify-build" / "mails.json"  # read by netlify/functions/search.mjs, bundled into it


def snapshot_time() -> datetime:
    """The clock the export runs on: the snapshot's newest trace plus desk.STALE, so "today" is the
    snapshot's day whenever Netlify builds, and no desk session shows as still working.
    CRM_SNAPSHOT_AT (ISO time) overrides it."""
    if os.environ.get("CRM_SNAPSHOT_AT"):
        return datetime.fromisoformat(os.environ["CRM_SNAPSHOT_AT"].replace("Z", "+00:00"))
    data = desk.load(desk.ROOT)
    stamps = [desk._ts(e["ts"]) for e in data.events] + [desk._ts(m.get("received_at")) for m in data.mails.values()]
    return max((t for t in stamps if t), default=datetime.now(UTC)) + desk.STALE


# One load of the desk's files for the whole export, at the snapshot's time, instead of one per endpoint.
_load, _AT = desk.load, snapshot_time()
desk.load = functools.cache(lambda root=desk.ROOT, now=None: _load(root, now=now or _AT))


def write(path: Path, content: str | bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content if isinstance(content, bytes) else content.encode())


def get(client: TestClient, url: str) -> str:
    response = client.get(url)
    if response.status_code != 200:
        raise SystemExit(f"{url}: HTTP {response.status_code} {response.text[:300]}")
    return response.text


def main() -> None:
    if not desk.ROOT.exists():
        raise SystemExit(f"No ultra-talos folder at {desk.ROOT}. Set ULTRA_TALOS_DIR.")
    if OUT.exists():
        shutil.rmtree(OUT)
    client = TestClient(app.app)

    # The pages, with their scripts and styles under the build path the page names.
    write(OUT / "crm" / "index.html", get(client, "/crm"))
    write(OUT / "crm" / "slop" / "index.html", get(client, "/crm/slop"))
    write(OUT / "index.html", '<!doctype html><meta http-equiv="refresh" content="0; url=/crm"><a href="/crm">TALOS CRM</a>\n')
    for prefix in (app.ASSETS, "/crm/assets"):
        shutil.copytree(app.UI_DIR, OUT / prefix.lstrip("/"), dirs_exist_ok=True)

    # The read-only API, one file per answer.
    api = OUT / "crm" / "api"
    for name in ("version", "snapshot", "rates", "settings"):
        write(api / f"{name}.json", get(client, f"/crm/api/{name}"))
    data = desk.load(desk.ROOT)
    for sid in data.shipments:
        write(api / "shipments" / f"{sid}.json", get(client, f"/crm/api/shipments/{sid}"))

    # The mail index for the search function: each mail's answer row and the texts it matches on.
    rows = []
    for m in sorted(data.mails.values(), key=lambda m: -m["id"]):
        sid = m.get("_shipment") or str(m.get("shipment"))
        rows.append({
            "hit": {"id": m["id"], "from": m["from"], "subject": m.get("subject"), "at": m.get("received_at"),
                    "category": (m.get("classification") or {}).get("category"), "shipment": sid if sid in data.shipments else None},
            "text": [str(m["id"]), str(m.get("from", "")).lower(), str(m.get("subject", "")).lower(), str(m.get("body", "")).lower()],
        })
    write(INDEX, json.dumps(rows, separators=(",", ":")))

    print(f"CRM exported to {OUT}: {len(data.shipments)} shipments, {len(data.mails)} mails, build {app._BUILD}, as of {_AT:%Y-%m-%d %H:%M} UTC")


if __name__ == "__main__":
    main()
