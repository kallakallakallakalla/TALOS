"""The CRM's data: what ultra-talos has written, read and never changed (ADR 58).

Sources, all under the ultra-talos folder:
- `mails/<id>.json`        every inbound mail with its classification and shipment
- `state/board.db`         the desk's black board: outbound mails, decisions, sessions
- `logs/*.jsonl`           the runner's tool calls; the platform's answers to bookings and cancellations
- `shipments/<id>/notes.md` the desk's file per shipment: customer and lane in the first line,
                            the quoted all-in price on its `QUOTE SENT` line
- `data/*.csv`             carriers, pickup zones, tariff lanes
- `state/NEEDS_HUMAN`      what the desk asked a person to do

`load()` turns them into shipments, customers and carriers. It is cached by a token that moves
whenever one of the sources does, so the page can poll cheaply.
"""

import csv
import json
import os
import re
import sqlite3
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from airports import AIRPORTS

ROOT = Path(os.environ.get("ULTRA_TALOS_DIR", Path(__file__).resolve().parent.parent / "ultra-talos"))
STALE = timedelta(minutes=30)  # a desk session without an end this old has died, it is not working
BOOKING_GRACE = timedelta(minutes=10)  # an acceptance not booked after this needs a person
CUSTOMER_RFQ = {"rfq", "RFQ"}  # the second is the old router's label on the oldest mails
ACCEPT, REJECT = {"customer_accept"}, {"customer_reject"}
CLOSED = {"booked", "lost", "cancelled"}

# The desk's own notes, not mail text: the header line it writes for every shipment, and its quote line.
_HEADER = re.compile(r"^#\s*Shipment\s+\d+:\s*(?P<company>.+?),\s*(?P<lane>.+?)\s*$")
_CODE = re.compile(r"\b[A-Z]{3}\b")
# ponytail: three spellings the desk used for the all-in figure; a price it wrote otherwise shows as "–".
_ALL_IN = [re.compile(p) for p in (r"([\d,]+\.\d{2})\s*EUR\s+all-in", r"EUR\s*([\d,]+\.\d{2})\s+all-in", r"all-in\s+(?:of\s+)?(?:EUR\s*)?([\d,]+\.\d{2})")]
_BK = re.compile(r"/v1/portal/bookings/(BK-[A-Z0-9]+)")
_ANSWER = re.compile(r'\{\s*"booking_reference"')
_SENT = re.compile(r'\{\s*"id":\s*\d+,\s*"from":\s*"talos@')
_JSON = json.JSONDecoder()


def _now() -> datetime:
    return datetime.now(UTC)


def _ts(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value else None


def company_from_address(email: str) -> str:
    """`nordlicht-audio@hackloop.mail` -> `Nordlicht Audio`, for a customer whose file names no company."""
    return " ".join(part.capitalize() for part in re.split(r"[-_.]+", email.split("@")[0]) if part)


def token(root: Path = ROOT) -> str:
    """Changes whenever a source changes: the newest board row, the mail folder, the person file."""
    parts = []
    db = root / "state" / "board.db"
    if db.exists():
        con = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=5)
        try:
            parts.append("{}:{}".format(*con.execute("SELECT max(id), count(*) FROM events").fetchone()))
        finally:
            con.close()
    for path in (root / "mails", root / "state" / "NEEDS_HUMAN", root / "shipments"):
        parts.append(str(path.stat().st_mtime_ns) if path.exists() else "-")
    return "|".join(parts)


@dataclass
class Desk:
    """Everything the CRM shows, built from the files at one moment."""

    shipments: dict[str, dict[str, Any]] = field(default_factory=dict)
    mails: dict[int, dict[str, Any]] = field(default_factory=dict)
    events: list[dict[str, Any]] = field(default_factory=list)  # the board, oldest first
    bookings: dict[str, dict[str, Any]] = field(default_factory=dict)  # BK -> the platform's answer
    sent: dict[int, dict[str, Any]] = field(default_factory=dict)  # our mail id -> the mail as the platform stored it
    replies: dict[str, list[dict[str, Any]]] = field(default_factory=dict)  # shipment -> carrier replies to our mails
    carriers: list[dict[str, str]] = field(default_factory=list)
    lanes: list[dict[str, str]] = field(default_factory=list)
    zones: list[dict[str, str]] = field(default_factory=list)
    needs_human: dict[str, Any] | None = None
    since_id: str | None = None
    tracking_requests: int = 0
    dry_until: datetime | None = None  # the desk only recorded drafts until then (outbox/); requests before are left out
    dry_hidden: int = 0
    loaded_at: datetime = field(default_factory=_now)


# --- reading the files -------------------------------------------------------------------------


def _read_mails(root: Path) -> dict[int, dict[str, Any]]:
    mails = {}
    for path in (root / "mails").glob("*.json"):
        try:
            mail = json.loads(path.read_text())
            mails[int(mail["id"])] = mail
        except (ValueError, KeyError, OSError):
            continue  # a file being written right now; the next load has it
    return mails


def _read_board(root: Path) -> list[dict[str, Any]]:
    db = root / "state" / "board.db"
    if not db.exists():
        return []
    con = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=5)  # never writes: the desk owns this file
    con.row_factory = sqlite3.Row
    try:
        rows = [dict(r) for r in con.execute("SELECT id, ts, shipment, mail_id, kind, summary, data FROM events ORDER BY id")]
    finally:
        con.close()
    for row in rows:
        try:
            row["data"] = json.loads(row["data"]) if row["data"] else None
        except ValueError:
            row["data"] = None
    return rows


def _read_platform_answers(root: Path) -> tuple[dict[str, dict[str, Any]], dict[str, datetime], dict[int, dict[str, Any]]]:
    """Bookings, cancellations and sent mails as the platform answered them, from the runner's tool calls.

    The command is the desk's, the answer is the platform's: a booking is the JSON object with a
    booking reference, a sent mail the JSON object with our address as sender and its new id. A
    dry run answers in another shape and is not counted. Each log file belongs to the inbound mail
    it was started for (`logs/<mail id>-<agent>.jsonl`); a sent mail keeps that id as `_for`.
    """
    bookings: dict[str, dict[str, Any]] = {}
    cancelled: dict[str, datetime] = {}
    sent: dict[int, dict[str, Any]] = {}
    for path in sorted((root / "logs").glob("*.jsonl")):
        started_for = path.name.split("-")[0]
        try:
            lines = path.read_text(errors="replace").splitlines()
        except OSError:
            continue
        for line in lines:
            if "portal/bookings" not in line and "talos@" not in line:
                continue
            try:
                entry = json.loads(line)
            except ValueError:
                continue
            part = entry.get("part") or {}
            state = part.get("state") or {}
            command = str((state.get("input") or {}).get("command", ""))
            output = str(state.get("output", ""))
            if part.get("tool") != "shell":
                continue
            at = datetime.fromtimestamp(entry.get("timestamp", 0) / 1000, UTC)
            for found in _ANSWER.finditer(output):
                try:
                    answer, _ = _JSON.raw_decode(output, found.start())
                except ValueError:
                    continue
                if str(answer.get("booking_reference", "")).startswith("BK-") and "POST /v1/portal/bookings" in command:
                    bookings[answer["booking_reference"]] = answer | {"at": at}
            for found in _SENT.finditer(output):
                try:
                    mail, _ = _JSON.raw_decode(output, found.start())
                except ValueError:
                    continue
                if isinstance(mail.get("id"), int) and started_for.isdigit():
                    sent.setdefault(mail["id"], mail | {"_for": int(started_for)})
            if "DELETE /v1/portal/bookings/" in command and re.search(r"HTTP 2\d\d", output):
                for found in _BK.finditer(command):
                    cancelled[found.group(1)] = at
    return bookings, cancelled, sent


def _read_csv(path: Path) -> list[dict[str, str]]:
    if not path.exists():
        return []
    with path.open(newline="") as handle:
        return list(csv.DictReader(handle))


def _read_notes(root: Path, shipment: str) -> str:
    path = root / "shipments" / shipment / "notes.md"
    try:
        return path.read_text()
    except OSError:
        return ""


def _quoted_price(notes: str) -> float | None:
    """The all-in price on the desk's last `QUOTE SENT` line."""
    for line in reversed(notes.splitlines()):
        if "QUOTE SENT" not in line:
            continue
        for pattern in _ALL_IN:
            if found := pattern.search(line):
                return float(found.group(1).replace(",", ""))
        return None
    return None


# --- one shipment ------------------------------------------------------------------------------


def _build_shipment(sid: str, mails: list[dict[str, Any]], out: list[dict[str, Any]], events: list[dict[str, Any]], notes: str,
                    bookings: list[dict[str, Any]], cancelled: dict[str, datetime], now: datetime) -> dict[str, Any]:
    first = mails[0]
    customer = str(first["from"]).strip().lower()
    header = _HEADER.match(notes.splitlines()[0]) if notes else None
    company = header.group("company").strip() if header else company_from_address(customer)
    lane = header.group("lane").strip() if header else ""
    left, arrow, right = lane.partition("->")
    starts, ends = _CODE.findall(left), _CODE.findall(right)
    origin = starts[0] if arrow and len(starts) == 1 else None  # "Wiesbaden -> GRU and TLV" has no departure airport yet
    destination = ends[0] if ends else None
    lane_label = f"{origin or left.strip()} → {', '.join(ends) if ends else right.strip()}" if arrow else lane or None

    customer_mails = [m for m in mails if str(m["from"]).strip().lower() == customer]
    category = lambda m: (m.get("classification") or {}).get("category")  # noqa: E731
    accepted = [m for m in customer_mails if category(m) in ACCEPT]
    rejected = [m for m in customer_mails if category(m) in REJECT]
    to_customer = [m for m in out if str(m.get("to")).strip().lower() == customer]
    quote_price = _quoted_price(notes)
    quote_at = next((_ts(e["ts"]) for e in events if e["kind"] in ("quote", "quote_sent")), None)
    quoted = quote_price is not None or quote_at is not None or bool(accepted) or "QUOTE SENT" in notes

    booking = bookings[-1] if bookings else None
    cancelled_at = cancelled.get(booking["booking_reference"]) if booking else None
    last_customer = _ts(customer_mails[-1]["received_at"]) if customer_mails else None
    last_to_customer = _ts(to_customer[-1]["received_at"]) if to_customer else None

    if booking and cancelled_at:
        status = "cancelled"
    elif booking:
        status = "booked"
    elif rejected and (not accepted or _ts(rejected[-1]["received_at"]) > _ts(accepted[-1]["received_at"])):
        status = "lost"
    elif quoted:
        status = "quoted"
    elif to_customer:
        status = "needs_info"  # we wrote to the customer, and it was not a quote: we wait for what we asked
    else:
        status = "new"

    # the desk at work, and where it stopped
    starts = [e for e in events if e["kind"] == "session_start"]
    ends = [e for e in events if e["kind"] == "session_end"]
    open_since = _ts(starts[-1]["ts"]) if starts and (not ends or ends[-1]["id"] < starts[-1]["id"]) else None
    running = bool(open_since and now - open_since < STALE)
    needs_you = []
    if open_since and not running:
        needs_you.append(f"Desk session started {open_since:%H:%M} UTC and never finished")
    if ends and not running and (code := re.match(r"exit (\d+)", ends[-1]["summary"] or "")) and code.group(1) != "0":
        needs_you.append(f"Desk stopped with an error (exit {code.group(1)})")
    if accepted and not booking and status != "lost" and now - _ts(accepted[-1]["received_at"]) > BOOKING_GRACE:
        needs_you.append("Customer accepted, no booking on record")
    for e in events:
        if e["kind"] == "needs_human":
            needs_you.append(e["summary"] or "The desk asked for a person")

    times = [_ts(m["received_at"]) for m in mails + out] + [_ts(e["ts"]) for e in events]
    times = [t for t in times if t]
    first_answer = to_customer[0]["received_at"] if to_customer else None
    return {
        "id": sid,
        "customer": customer,
        "company": company,
        "lane": lane_label,
        "origin": origin,
        "destination": destination,
        "subject": first.get("subject"),
        "status": status,
        "needs_you": needs_you,
        "running": running,
        "price_eur": quote_price,
        "booking": booking["booking_reference"] if booking else None,
        "airline": booking.get("airline") if booking else None,
        "air_eur": float(booking["price"]) if booking and booking.get("price") else None,
        "booked_at": _iso(booking["at"]) if booking else None,
        "cancelled_at": _iso(cancelled_at),
        "received_at": first.get("received_at"),
        "first_answer_at": first_answer,
        "accepted_at": accepted[-1]["received_at"] if accepted else None,
        "lost_reason": rejected[-1].get("body") if status == "lost" else None,
        "last_at": _iso(max(times)) if times else first.get("received_at"),
        "last_customer_at": _iso(last_customer),
        "last_to_customer_at": _iso(last_to_customer),
        "mails_in": len(mails),
        "mails_out": len(out),
        "has_notes": bool(notes),
    }


# --- everything --------------------------------------------------------------------------------

_cache: dict[str, Any] = {}


def load(root: Path = ROOT, now: datetime | None = None) -> Desk:
    """The desk as the files say now; rebuilt only when `token()` moved (or `now` is given, for tests)."""
    key, fixed = (root, token(root)), now is not None
    if not fixed and _cache.get("key") == key:
        return _cache["desk"]
    now = now or _now()
    desk = Desk(loaded_at=now)
    desk.mails = _read_mails(root)
    desk.events = _read_board(root)
    desk.bookings, cancelled, desk.sent = _read_platform_answers(root)
    desk.carriers = _read_csv(root / "data" / "carriers.csv")
    desk.lanes = _read_csv(root / "data" / "tariff_lanes.csv")
    desk.zones = _read_csv(root / "data" / "pickup_zones.csv")
    person = root / "state" / "NEEDS_HUMAN"
    if person.exists() and (text := person.read_text().strip()):
        stamp, _, what = text.partition(" ")
        found = re.search(r"shipment (\d+)", what)
        desk.needs_human = {"at": stamp if _ts(stamp) else None, "text": what if _ts(stamp) else text, "shipment": found.group(1) if found else None}
    drafts = sorted(p.name.split("-")[0] for p in (root / "outbox").glob("*.json")) if (root / "outbox").exists() else []
    if drafts:
        desk.dry_until = datetime.strptime(drafts[-1], "%Y%m%dT%H%M%S%f").replace(tzinfo=UTC)
    cursor = root / "state" / "since_id"
    desk.since_id = cursor.read_text().strip() if cursor.exists() else None

    by_shipment: dict[str, list[dict[str, Any]]] = {}
    for mail in sorted(desk.mails.values(), key=lambda m: m["id"]):
        if mail.get("shipment"):
            by_shipment.setdefault(str(mail["shipment"]), []).append(mail)
    events_of: dict[str, list[dict[str, Any]]] = {}
    for event in desk.events:
        if event["shipment"]:
            events_of.setdefault(str(event["shipment"]), []).append(event)
    bookings_of: dict[str, list[dict[str, Any]]] = {}
    for answer in sorted(desk.bookings.values(), key=lambda b: b["at"]):
        # the desk sometimes suffixed the reference for one leg of a split shipment: "33304-RUH"
        bookings_of.setdefault(str(answer.get("team_reference")).split("-")[0], []).append(answer)
    # our mails belong to the shipment of the mail whose desk session sent them;
    # a carrier's reply to one of them belongs to that shipment too
    sent_of: dict[str, list[dict[str, Any]]] = {}
    for mail in sorted(desk.sent.values(), key=lambda m: m["id"]):
        cause = desk.mails.get(mail["_for"])
        if cause and cause.get("shipment"):
            mail["_shipment"] = str(cause["shipment"])
            sent_of.setdefault(mail["_shipment"], []).append(mail)
    desk.replies = {}
    for mail in desk.mails.values():
        ours = desk.sent.get(mail.get("in_reply_to"))
        if ours and ours.get("_shipment") and str(mail.get("shipment")) != ours["_shipment"]:
            desk.replies.setdefault(ours["_shipment"], []).append(mail)

    for sid, mails in by_shipment.items():
        first = mails[0].get("classification") or {}
        if first.get("sender_role") != "customer" or first.get("category") not in CUSTOMER_RFQ:
            if first.get("category") == "tracking_request":
                desk.tracking_requests += 1
            continue  # a trucker reply, an airline confirmation, a tracking request: not a shipment of ours
        notes = _read_notes(root, sid)
        if not notes and sid not in events_of:
            continue  # before the desk ran: the old router answered it, and its records are not here
        if desk.dry_until and (_ts(mails[0]["received_at"]) or desk.dry_until) <= desk.dry_until:
            desk.dry_hidden += 1
            continue  # handled while the desk only wrote drafts: nothing it did reached anyone
        desk.shipments[sid] = _build_shipment(sid, mails, sent_of.get(sid, []), events_of.get(sid, []), notes, bookings_of.get(sid, []), cancelled, now)
        desk.shipments[sid]["carrier_replies"] = len(desk.replies.get(sid, []))
    # an airline's booking confirmation names our booking reference; it belongs to that shipment
    by_booking = {s["booking"]: sid for sid, s in desk.shipments.items() if s["booking"]}
    for mail in desk.mails.values():
        if (mail.get("classification") or {}).get("category") == "booking_confirmation" and str(mail.get("shipment")) not in desk.shipments:
            found = re.search(r"BK-[A-Z0-9]+", str(mail.get("subject") or ""))
            if found and found.group(0) in by_booking:
                mail["_shipment"] = by_booking[found.group(0)]
                desk.replies.setdefault(mail["_shipment"], []).append(mail)
    person = desk.needs_human and desk.shipments.get(desk.needs_human["shipment"])
    if person and person["status"] not in CLOSED:
        person["needs_you"].insert(0, desk.needs_human["text"])
    elif desk.needs_human and person:
        desk.needs_human["outdated"] = True  # the shipment it was about is closed; the file was never cleared
    for shipment in desk.shipments.values():
        # a lost or cancelled shipment needs nobody any more, whatever was noted on the way
        shipment["needs_you"] = [] if shipment["status"] in ("lost", "cancelled") else list(dict.fromkeys(shipment["needs_you"]))
    if not fixed:
        _cache.update(key=key, desk=desk)
    return desk


def timeline(desk: Desk, sid: str) -> list[dict[str, Any]]:
    """What happened to one shipment, oldest first: mails in and out, the desk's notes and sessions, bookings."""
    shipment = desk.shipments.get(sid)
    known = {(c.get("email") or "").strip().lower(): (c["name"], c.get("kind")) for c in desk.carriers}
    if shipment:
        known[shipment["customer"]] = (shipment["company"], "customer")
    name = lambda email: known.get(str(email).strip().lower(), (email, None))[0]  # noqa: E731
    kind = lambda email: known.get(str(email).strip().lower(), (email, None))[1]  # noqa: E731
    items = []
    for mail in [m for m in desk.mails.values() if str(m.get("shipment")) == sid] + desk.replies.get(sid, []):
        c = mail.get("classification") or {}
        items.append({"at": mail["received_at"], "type": "mail_in", "title": f"Mail from {name(mail['from'])}", "who": [name(mail["from"])],
                      "kinds": [kind(mail["from"])], "mail_id": mail["id"],
                      "category": c.get("category"), "role": c.get("sender_role"), "subject": mail.get("subject"), "body": mail.get("body"),
                      "attachments": [a.get("filename") or a.get("name") for a in mail.get("attachments") or [] if isinstance(a, dict)]})
    for mail in desk.sent.values():
        if mail.get("_shipment") == sid:
            items.append({"at": mail["received_at"], "type": "mail_out", "title": f"Mail to {name(mail['to'])}", "who": [name(mail["to"])],
                          "kinds": [kind(mail["to"])], "mail_id": mail["id"], "subject": mail.get("subject"), "body": mail.get("body")})
    for e in desk.events:
        if str(e["shipment"]) != sid or e["kind"] in ("mail_in", "mail_out", "booking", "session_start") or e["kind"].endswith("_dry"):
            continue
        if e["kind"] == "session_end":
            items.append({"at": e["ts"], "type": "desk", "title": "Desk finished" if (e["summary"] or "").startswith("exit 0") else "Desk stopped",
                          "text": re.sub(r"^exit \d+:\s*", "", e["summary"] or "")})
        else:
            items.append({"at": e["ts"], "type": "note", "title": e["kind"].replace("_", " ").capitalize(), "text": e["summary"]})
    for ref, answer in desk.bookings.items():
        if str(answer.get("team_reference")).split("-")[0] == sid:
            items.append({"at": _iso(answer["at"]), "type": "booking", "title": f"Booked {ref}",
                          "text": f"{answer.get('airline')} · {answer.get('price')} {answer.get('currency', 'EUR')} air freight · status {answer.get('status')}"})
    if shipment and shipment["cancelled_at"]:
        items.append({"at": shipment["cancelled_at"], "type": "cancel", "title": f"Booking {shipment['booking']} cancelled", "text": ""})
    items.sort(key=lambda i: _ts(i["at"]) or datetime.min.replace(tzinfo=UTC))
    # one line for the same mail to several carriers at once, and one for their replies;
    # each mail stays readable as a part of that line
    merged: list[dict[str, Any]] = []
    for item in items:
        last = merged[-1] if merged else None
        carrier = item.get("kinds") and item["kinds"][0] in ("trucker", "airline")
        if (carrier and last and last["type"] == item["type"] and last.get("kinds", [None])[0] == item["kinds"][0]
                and (last.get("subject") or "").removeprefix("Re: ") == (item.get("subject") or "").removeprefix("Re: ")):
            if "parts" not in last:
                last["parts"] = [{"who": last["who"][0], "body": last.pop("body", None), "at": last["at"]}]
            last["parts"].append({"who": item["who"][0], "body": item.get("body"), "at": item["at"]})
            last["who"] += item["who"]
            group = {"trucker": "truckers", "airline": "airlines"}[item["kinds"][0]]
            last["title"] = f"{'Mail to' if item['type'] == 'mail_out' else 'Replies from'} {len(last['who'])} {group}"
            last["text"] = ", ".join(last["who"])
            continue
        merged.append(item)
    return merged


def notes(sid: str, root: Path = ROOT) -> str:
    return _read_notes(root, sid)


# --- the figures each page shows ---------------------------------------------------------------


def _median(values: list[float]) -> float | None:
    values = sorted(v for v in values if v is not None)
    if not values:
        return None
    mid = len(values) // 2
    return values[mid] if len(values) % 2 else (values[mid - 1] + values[mid]) / 2


def _minutes(start: str | None, end: str | None) -> float | None:
    a, b = _ts(start), _ts(end)
    return (b - a).total_seconds() / 60 if a and b and b >= a else None


def desk_status(desk: Desk) -> dict[str, Any]:
    """Is the desk working? From its newest trace: a session open now, or how long it has been quiet."""
    running = sum(s["running"] for s in desk.shipments.values())
    stamps = [_ts(e["ts"]) for e in desk.events[-1:]] + [_ts(m["received_at"]) for m in desk.mails.values()]
    last = max((t for t in stamps if t), default=None)
    quiet = (desk.loaded_at - last).total_seconds() / 60 if last else None
    if running:
        state, label = "working", f"Desk working on {running} {'shipment' if running == 1 else 'shipments'}"
    elif quiet is not None and quiet < 15:
        state, label = "idle", "Desk idle, waiting for mail"
    else:
        state, label = "quiet", "No desk activity" + (f" since {last:%d %b %H:%M} UTC" if last else "")
    return {"state": state, "label": label, "last_activity_at": _iso(last), "last_mail_id": desk.since_id}


def today(desk: Desk) -> dict[str, Any]:
    day = desk.loaded_at.replace(hour=0, minute=0, second=0, microsecond=0)
    on_day = lambda v: bool(_ts(v) and _ts(v) >= day)  # noqa: E731
    s = list(desk.shipments.values())
    waiting = [x for x in s if x["status"] in ("needs_info", "quoted") and not x["needs_you"] and not x["running"]]
    for x in waiting:
        x["waiting_since"] = x["last_to_customer_at"]
    return {
        "date": day.date().isoformat(),
        "kpis": {
            "new": sum(on_day(x["received_at"]) for x in s),
            "quoted": sum(on_day(x["first_answer_at"]) and x["status"] in ("quoted", "booked", "lost", "cancelled") for x in s),
            "booked": sum(on_day(x["booked_at"]) for x in s),
            "lost": sum(x["status"] == "lost" and on_day(x["last_at"]) for x in s),
        },
        "needs_you": sorted((x for x in s if x["needs_you"]), key=lambda x: x["last_at"] or "", reverse=True),
        "waiting": sorted(waiting, key=lambda x: x["waiting_since"] or ""),
        "running": [x for x in s if x["running"]],
        "desk_note": desk.needs_human if desk.needs_human and not desk.needs_human.get("shipment") else None,
    }


def customers(desk: Desk) -> list[dict[str, Any]]:
    by: dict[str, list[dict[str, Any]]] = {}
    for x in desk.shipments.values():
        by.setdefault(x["customer"], []).append(x)
    rows = []
    for email, items in by.items():
        booked = [x for x in items if x["status"] == "booked"]
        lost = [x for x in items if x["status"] == "lost"]
        named = [x["company"] for x in items if x["company"] != company_from_address(email)]
        rows.append({
            "email": email,
            "company": max(set(named), key=named.count) if named else company_from_address(email),
            "shipments": len(items),
            "open": sum(x["status"] in ("new", "needs_info", "quoted") for x in items),
            "booked": len(booked),
            "lost": len(lost),
            "win_rate_pct": round(100 * len(booked) / (len(booked) + len(lost))) if booked or lost else None,
            "booked_eur": round(sum(x["price_eur"] or 0 for x in booked), 2),
            "destinations": sorted({x["destination"] for x in items if x["destination"]}),
            "last_at": max(x["last_at"] or "" for x in items),
        })
    return sorted(rows, key=lambda r: r["last_at"], reverse=True)


def carriers(desk: Desk) -> dict[str, list[dict[str, Any]]]:
    """Airlines by what was booked with them; truckers by how often they were asked and answered."""
    lanes: dict[str, int] = {}
    for lane in desk.lanes:
        lanes[lane["airline"]] = lanes.get(lane["airline"], 0) + 1
    booked: dict[str, list[dict[str, Any]]] = {}
    for x in desk.shipments.values():
        if x["airline"] and x["status"] in ("booked", "cancelled"):
            booked.setdefault(x["airline"], []).append(x)
    asked: dict[str, int] = {}
    for mail in desk.sent.values():
        to = str(mail.get("to")).strip().lower()
        asked[to] = asked.get(to, 0) + 1
    answers: dict[str, dict[str, int]] = {}
    reply_min: dict[str, list[float]] = {}
    for m in desk.mails.values():
        category = (m.get("classification") or {}).get("category")
        if category in ("trucker_quote", "trucker_decline") and m.get("in_reply_to") in desk.sent:  # a reply to one of our requests
            answers.setdefault(str(m["from"]).strip().lower(), {"trucker_quote": 0, "trucker_decline": 0})[category] += 1
            reply_min.setdefault(str(m["from"]).strip().lower(), []).append(_minutes(desk.sent[m["in_reply_to"]].get("received_at"), m["received_at"]))
    airlines, truckers = [], []
    for c in desk.carriers:
        email = (c.get("email") or "").strip().lower()
        if c.get("kind") == "airline":
            items = booked.get(c["name"], [])
            airlines.append({
                "name": c["name"], "email": email, "active": c.get("active") != "0",
                "destinations": len((c.get("serves") or "").split()), "tariff_lanes": lanes.get(c["name"], 0),
                "bookings": sum(x["status"] == "booked" for x in items), "cancelled": sum(x["status"] == "cancelled" for x in items),
                "booked_air_eur": round(sum(x["air_eur"] or 0 for x in items if x["status"] == "booked"), 2),
                "last_booked_at": max((x["booked_at"] for x in items), default=None),
            })
        elif c.get("kind") == "trucker":
            got = answers.get(email, {"trucker_quote": 0, "trucker_decline": 0})
            n = asked.get(email, 0)
            truckers.append({
                "name": c["name"], "email": email, "active": c.get("active") != "0", "asked": n,
                "offers": got["trucker_quote"], "declines": got["trucker_decline"],
                "reply_rate_pct": round(100 * min(1, (got["trucker_quote"] + got["trucker_decline"]) / n)) if n else None,
                "median_reply_min": _median(reply_min.get(email, [])),
            })
    return {
        "airlines": sorted(airlines, key=lambda a: (-a["bookings"], a["name"])),
        "truckers": sorted(truckers, key=lambda t: (-t["asked"], t["name"])),
    }


def reports(desk: Desk) -> dict[str, Any]:
    s = list(desk.shipments.values())
    quoted = [x for x in s if x["status"] in ("quoted", "booked", "lost", "cancelled")]
    booked = [x for x in s if x["status"] in ("booked", "cancelled")]
    lost = [x for x in s if x["status"] == "lost"]
    lanes: dict[str, int] = {}
    for x in s:
        if x["origin"] and x["destination"]:
            key = f"{x['origin']} → {x['destination']}"
            lanes[key] = lanes.get(key, 0) + 1
    return {
        "funnel": [
            {"stage": "Requests", "count": len(s)},
            {"stage": "Quoted", "count": len(quoted)},
            {"stage": "Booked", "count": len(booked)},
        ],
        "lost": len(lost),
        "cancelled": sum(x["status"] == "cancelled" for x in s),
        "win_rate_pct": round(100 * len(booked) / (len(booked) + len(lost))) if booked or lost else None,
        "median_first_answer_min": _median([_minutes(x["received_at"], x["first_answer_at"]) for x in s]),
        "median_to_booking_min": _median([_minutes(x["accepted_at"], x["booked_at"]) for x in booked]),
        "booked_eur": round(sum(x["price_eur"] or 0 for x in booked if x["status"] == "booked"), 2),
        "losses": [{"id": x["id"], "company": x["company"], "lane": x["lane"], "origin": x["origin"], "destination": x["destination"],
                    "price_eur": x["price_eur"], "words": x["lost_reason"],
                    "at": x["last_at"]} for x in sorted(lost, key=lambda x: x["last_at"] or "", reverse=True)],
        "top_lanes": sorted(({"lane": k, "count": v} for k, v in lanes.items()), key=lambda r: -r["count"])[:10],
        "tracking_requests": desk.tracking_requests,
    }


def search_mails(desk: Desk, query: str, limit: int = 30) -> list[dict[str, Any]]:
    q = query.strip().lower()
    if not q:
        return []
    hits = []
    for m in sorted(desk.mails.values(), key=lambda m: -m["id"]):
        if q in str(m["id"]) or q in str(m.get("from", "")).lower() or q in str(m.get("subject", "")).lower() or q in str(m.get("body", "")).lower():
            sid = m.get("_shipment") or str(m.get("shipment"))
            hits.append({"id": m["id"], "from": m["from"], "subject": m.get("subject"), "at": m.get("received_at"),
                         "category": (m.get("classification") or {}).get("category"), "shipment": sid if sid in desk.shipments else None})
            if len(hits) >= limit:
                break
    return hits


# --- the colour strips and the globe on Today --------------------------------------------------

STRIP = 30  # ticks per strip, oldest on the left
_TICK = {"booked": ("ok", "booked"), "quoted": ("open", "quoted, waiting for the customer"), "needs_info": ("warn", "waiting for information"),
         "new": ("open", "being worked on"), "lost": ("lost", "lost"), "cancelled": ("lost", "booking cancelled")}


def strips(desk: Desk) -> list[dict[str, Any]]:
    """Rows of coloured ticks, one per recent request, booking and mail; each tick says what it is and opens its page."""

    def padded(ticks: list[dict[str, Any]]) -> list[dict[str, Any]]:
        return [{"tone": "none", "tip": ""}] * (STRIP - len(ticks[-STRIP:])) + ticks[-STRIP:]

    ships = sorted(desk.shipments.values(), key=lambda s: s["received_at"] or "")[-STRIP:]
    requests = [{"tone": "bad" if s["needs_you"] else _TICK[s["status"]][0], "href": f"#/shipment/{s['id']}",
                 "tip": f"{s['id']} · {s['company']} · {s['lane'] or ''}: " + (s["needs_you"][0] if s["needs_you"] else _TICK[s["status"]][1])} for s in ships]
    booked = sorted((s for s in desk.shipments.values() if s["booking"]), key=lambda s: s["booked_at"] or "")[-STRIP:]
    bookings = [{"tone": "lost" if s["status"] == "cancelled" else "ok", "href": f"#/shipment/{s['id']}",
                 "tip": f"{s['booking']} · {s['company']} · {s['airline']}" + (" · cancelled" if s["status"] == "cancelled" else "")} for s in booked]
    recent = sorted(desk.mails.values(), key=lambda m: m["id"])[-STRIP:]
    mails = []
    for m in recent:
        c = m.get("classification") or {}
        sid = str(m.get("shipment"))
        tone = "bad" if not c else "warn" if (c.get("confidence") or 0) < 0.8 else "ok"
        mails.append({"tone": tone, "href": f"#/shipment/{sid}" if sid in desk.shipments else None,
                      "tip": f"Mail {m['id']} from {m['from']}: {(c.get('category') or 'not read').replace('_', ' ')}" + (f", {100 * c['confidence']:.0f}% sure" if tone == "warn" else "")})
    good = sum(t["tone"] == "ok" for t in requests)
    return [
        {"name": "Requests", "ticks": padded(requests), "text": f"{good} of {len(requests)} booked"},
        {"name": "Bookings", "ticks": padded(bookings), "text": f"{sum(t['tone'] == 'ok' for t in bookings)} confirmed"},
        {"name": "Mail", "ticks": padded(mails), "text": f"{sum(t['tone'] == 'ok' for t in mails)} of {len(mails)} read surely"},
    ]


def globe(desk: Desk) -> dict[str, Any]:
    """Routes for the globe: per departure and destination airport, how many requests and how they ended."""
    grouped: dict[tuple[str | None, str], list[dict[str, Any]]] = {}
    for s in desk.shipments.values():
        if s["destination"]:
            grouped.setdefault((s["origin"], s["destination"]), []).append(s)
    lanes = [{
        "origin": o, "destination": d, "lane": f"{o or '?'}-{d}", "requests": len(items),
        "accepted": sum(x["status"] == "booked" for x in items), "rejected": sum(x["status"] in ("lost", "cancelled") for x in items),
        "open": sum(x["status"] in ("new", "needs_info", "quoted") for x in items), "in_transit": [],
        "accounts": sorted({x["company"] for x in items}), "shipments": [x["id"] for x in items],
    } for (o, d), items in grouped.items()]
    origins = {o for o, _ in grouped if o} | {z["airport"] for z in desk.zones}
    airlines = [{"name": c["name"], "serves": (c.get("serves") or "").split()} for c in desk.carriers if c.get("kind") == "airline"]
    return {
        "lanes": sorted(lanes, key=lambda r: -r["requests"]),
        "airports": {code: {"city": city, "lat": lat, "lon": lon, "origin": code in origins, "used": False} for code, (city, lat, lon) in AIRPORTS.items()},
        "airlines": airlines,
    }
