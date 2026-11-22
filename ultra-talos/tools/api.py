#!/usr/bin/env python3
"""The one door to the Hackloop platform API (mailbox, portal, board, anything new).

  tools/api.py GET  /v1/mailbox/messages/275
  tools/api.py GET  "/v1/portal/rates?origin=DUS&destination=JFK&airline=FQ&weight_kg=1540&volume_cbm=5.9"
  tools/api.py GET  /v1/mailbox/messages/275/attachments/3 --out shipments/275/packing.pdf
  tools/api.py POST /v1/mailbox/messages '{"to":"x@hackloop.mail","subject":"..","body":"..","in_reply_to":275}'
  tools/api.py POST /v1/mailbox/messages @shipments/275/quote.json     (body from a file)

Prints the response body; exit code 0 for 2xx, 1 otherwise ("HTTP <status>" goes to stderr).
The key is added here. A 429 is retried after its Retry-After (max 5 times).

DRY RUN (default, SEND_ENABLED is not "true" in ultra-talos/.env): every call that is not a
GET is recorded in outbox/ and NOT sent. A second system is live on the same mailbox; a
second sender would mean duplicate quotes.
"""

import json
import os
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(Path(__file__).resolve().parent))
import board  # noqa: E402


def _env_file(file: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    if file.exists():
        for line in file.read_text().splitlines():
            key, sep, value = line.partition("=")
            if sep and not key.lstrip().startswith("#"):
                values[key.strip()] = value.strip().strip("\"'")
    return values


def env() -> dict[str, str]:
    """Keys from router/.env; ultra-talos/.env overrides. SEND_ENABLED counts only from
    ultra-talos/.env: the live router's switch must not switch this system live."""
    values = _env_file(ROOT.parent / "router" / ".env")
    values.pop("SEND_ENABLED", None)
    return values | _env_file(ROOT / ".env")


def dry_run() -> bool:
    return env().get("SEND_ENABLED", "false").lower() != "true"


def _dry_response(method: str, path: str, body: object) -> dict:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%f")
    record = ROOT / "outbox" / f"{stamp}-{method}-{path.strip('/').replace('/', '_')[:40]}.json"
    record.parent.mkdir(exist_ok=True)
    record.write_text(json.dumps({"method": method, "path": path, "body": body}, indent=2, ensure_ascii=False))
    answer = {"dry_run": True, "recorded": str(record.relative_to(ROOT)), "sent": False}
    if "bookings" in path:
        answer |= {"booking_reference": "BK-DRYRUN", "note": "DRY RUN: nothing was booked. Record this as a dry-run booking."}
    elif "mailbox/messages" in path:
        answer |= {"id": None, "note": (
            "DRY RUN: this mail was recorded, not sent. Another system answers this mailbox for "
            "real and has most likely sent the same request; the recipient's reply to IT will "
            "land in mails/ (about 40 s after the customer's mail arrived). Treat such a reply "
            "from this recipient, received after the customer's mail, as the reply to your request."
        )}
    return answer


def call(method: str, path: str, body: object = None, raw: bool = False) -> tuple[int, object]:
    """One API call: `(status, parsed JSON or text)`; with `raw` the body comes back as bytes."""
    settings = env()
    method = method.upper()
    if method != "GET":
        b = body if isinstance(body, dict) else {}
        what = f"to {b.get('to')}: {b.get('subject')}" if "mailbox/messages" in path else json.dumps(b)[:200]
        kind = "booking" if "bookings" in path else "mail_out" if "mailbox/messages" in path else "api_write"
        board.log(kind + ("_dry" if dry_run() else ""), f"{method} {path} {what}", data=body)
        if dry_run():
            return 201, _dry_response(method, path, body)
    base = settings.get("HACKLOOP_BASE_URL", "https://api.start-hack.5u.ai").rstrip("/")
    url = path if path.startswith("http") else base + path
    headers = {"Accept": "application/json"}
    if url.startswith(base):  # the key never leaves the platform
        headers["X-API-Key"] = settings["HACKLOOP_API_KEY"]
    data = None
    if body is not None:
        data = json.dumps(body).encode()
        headers["Content-Type"] = "application/json"
    for attempt in range(6):
        try:
            with urllib.request.urlopen(urllib.request.Request(url, data, headers, method=method), timeout=30) as r:
                status, payload = r.status, r.read()
        except urllib.error.HTTPError as e:
            status, payload = e.code, e.read()
            if status == 429 and attempt < (1 if "/portal/rates" in path else 5):
                # Mailbox list: one poll per 5 s per team, shared with the live system, which
                # polls on a fixed beat. Go in the moment the window opens, or it wins every time.
                try:
                    wait = float(json.loads(payload).get("retry_after"))
                except (ValueError, TypeError, AttributeError):
                    wait = float(e.headers.get("Retry-After") or 5)
                time.sleep(min(wait, 12) + 0.05)
                continue
        break
    if raw:
        return status, payload
    try:
        return status, json.loads(payload)
    except ValueError:
        return status, payload.decode("utf-8", errors="replace")


def main(argv: list[str]) -> int:
    if len(argv) < 2 or argv[0] in ("-h", "--help"):
        print(__doc__)
        return 2
    out = None
    if "--out" in argv:
        i = argv.index("--out")
        out = argv[i + 1]
        argv = argv[:i] + argv[i + 2:]
    method, path, *rest = argv
    body = None
    if rest:
        text = Path(rest[0][1:]).read_text() if rest[0].startswith("@") else rest[0]
        try:
            body = json.loads(text)
        except ValueError as e:
            print(f"The body is not valid JSON: {e}", file=sys.stderr)
            return 2
    status, payload = call(method, path, body, raw=bool(out))
    print(f"HTTP {status}", file=sys.stderr)
    if out and status < 300:
        Path(out).parent.mkdir(parents=True, exist_ok=True)
        Path(out).write_bytes(payload)
        print(f"saved {len(payload)} bytes to {out}")
    elif isinstance(payload, bytes):
        print(payload.decode("utf-8", errors="replace"))
    else:
        print(payload if isinstance(payload, str) else json.dumps(payload, indent=1, ensure_ascii=False))
    return 0 if status < 300 else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
