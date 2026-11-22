"""The CRM over a small ultra-talos folder built here: statuses, needs-you rules, timeline, figures, endpoints (ADR 58)."""

import json
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import desk
from app import app

NOW = datetime(2026, 11, 22, 12, 0, tzinfo=UTC)
CUSTOMER, TRUCKER = "acme-tools@hackloop.mail", "fast-truck@hackloop.mail"


def at(minutes: float) -> str:
    return (NOW - timedelta(minutes=minutes)).isoformat()


class Folder:
    """An ultra-talos folder with only what the CRM reads."""

    def __init__(self, root: Path) -> None:
        self.root = root
        for sub in ("mails", "logs", "state", "shipments", "data"):
            (root / sub).mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(root / "state" / "board.db")
        self.db.execute("CREATE TABLE events (id INTEGER PRIMARY KEY, ts TEXT, shipment TEXT, mail_id INTEGER, kind TEXT, summary TEXT, data TEXT)")
        (root / "data" / "carriers.csv").write_text(
            "name,kind,email,serves,aliases,active,source,source_message_id,extra\n"
            f"Fast Truck,trucker,{TRUCKER},20 21,,1,start_kit,,\n"
            "Blue Air,airline,blue-air@hackloop.mail,JFK SGN,,1,start_kit,,\n")
        (root / "data" / "tariff_lanes.csv").write_text("airline,origin,destination,valid_from,valid_to,transit_days,minimum_eur,breaks_kg,rates_per_kg,fuel_per_kg,security_per_kg,currency,source,source_message_id,extra\n"
                                                         "Blue Air,HAM,JFK,2026-11-01,2026-11-30,2,70,0 45,4.1 3.2,0.7,0.2,EUR,start_kit,,\n")
        (root / "data" / "pickup_zones.csv").write_text("zone,area,airport,source,source_message_id,extra\n20,Hamburg,HAM,start_kit,,\n")
        self.lines: dict[str, list[str]] = {}

    def mail(self, mid: int, sender: str, category: str, role: str, shipment: int, minutes: float, body: str = "", reply_to: int | None = None) -> None:
        (self.root / "mails" / f"{mid}.json").write_text(json.dumps({
            "id": mid, "from": sender, "to": "talos@hackloop.mail", "subject": f"Request {shipment}", "body": body,
            "in_reply_to": reply_to, "references": [], "received_at": at(minutes), "attachments": [], "shipment": str(shipment),
            "classification": {"category": category, "confidence": 0.99, "sender_role": role}}))

    def sent(self, for_mail: int, mid: int, to: str, minutes: float, subject: str = "Re: Request") -> None:
        answer = {"id": mid, "from": "talos@hackloop.mail", "to": to, "subject": subject, "body": "Hello", "in_reply_to": for_mail, "received_at": at(minutes)}
        self._tool(for_mail, "python3 tools/api.py POST /v1/mailbox/messages '{...}'", f"HTTP 201\n{json.dumps(answer, indent=1)}", minutes)

    def booked(self, for_mail: int, shipment: int, ref: str, minutes: float) -> None:
        answer = {"booking_reference": ref, "team_reference": str(shipment), "airline": "Blue Air", "price": "1000.00", "currency": "EUR", "status": "booked"}
        self._tool(for_mail, "python3 tools/api.py POST /v1/portal/bookings '{...}'", f"noted\nHTTP 201\n{json.dumps(answer, indent=1)}", minutes)

    def cancelled(self, for_mail: int, ref: str, minutes: float) -> None:
        self._tool(for_mail, f"python3 tools/api.py DELETE /v1/portal/bookings/{ref}", "HTTP 200\n{}", minutes)

    def _tool(self, for_mail: int, command: str, output: str, minutes: float) -> None:
        stamp = int((NOW - timedelta(minutes=minutes)).timestamp() * 1000)
        line = json.dumps({"type": "tool_use", "timestamp": stamp, "part": {"tool": "shell", "state": {"input": {"command": command}, "output": output}}})
        path = self.root / "logs" / f"{for_mail}-desk.jsonl"
        with path.open("a") as handle:
            handle.write(line + "\n")

    def event(self, shipment: int, kind: str, summary: str, minutes: float) -> None:
        self.db.execute("INSERT INTO events (ts, shipment, kind, summary) VALUES (?, ?, ?, ?)", (at(minutes), str(shipment), kind, summary))
        self.db.commit()

    def notes(self, shipment: int, text: str) -> None:
        (self.root / "shipments" / str(shipment)).mkdir(exist_ok=True)
        (self.root / "shipments" / str(shipment) / "notes.md").write_text(text)


@pytest.fixture
def folder(tmp_path: Path) -> Folder:
    f = Folder(tmp_path / "ultra-talos")
    # 100: quoted, accepted, booked; trucker asked and answered
    f.mail(100, CUSTOMER, "rfq", "customer", 100, 60)
    f.notes(100, "# Shipment 100: Acme Tools GmbH, HAM -> JFK\n- 11:10 UTC QUOTE SENT mail 103: 1,234.56 EUR all-in = air 1000 + truck 234.56\n")
    f.sent(100, 101, TRUCKER, 59, subject="Pickup quote 20095 Hamburg to HAM")
    f.mail(102, TRUCKER, "trucker_quote", "trucker", 101, 58, reply_to=101)
    f.sent(100, 103, CUSTOMER, 57)
    f.event(100, "session_start", "desk session for mail 100", 60)
    f.event(100, "session_end", "exit 0: quoted", 57)
    f.mail(104, CUSTOMER, "customer_accept", "customer", 100, 50)
    f.booked(104, 100, "BK-AAAA1111", 49)
    f.mail(105, "blue-air@hackloop.mail", "booking_confirmation", "airline", 105, 48)
    (f.root / "mails" / "105.json").write_text((f.root / "mails" / "105.json").read_text().replace("Request 105", "Booking BK-AAAA1111 confirmed"))
    # 200: asked for information, nothing quoted
    f.mail(200, CUSTOMER, "rfq", "customer", 200, 40)
    f.sent(200, 201, CUSTOMER, 39)
    f.event(200, "session_end", "exit 0: asked for the DG documents", 39)
    # 300: quoted, then declined
    f.mail(300, CUSTOMER, "rfq", "customer", 300, 30)
    f.notes(300, "# Shipment 300: Acme Tools GmbH, STR -> SGN\n- QUOTE SENT mail 301: EUR 2,000.00 all-in\n")
    f.sent(300, 301, CUSTOMER, 29)
    f.mail(302, CUSTOMER, "customer_reject", "customer", 300, 20, body="Hi there,\n\nYour price is too high for us, so we will not go ahead.")
    # 400: booked, then the booking was cancelled
    f.mail(400, CUSTOMER, "rfq", "customer", 400, 25)
    f.notes(400, "# Shipment 400: Acme Tools GmbH, HAM -> SGN\n- QUOTE SENT mail 401: all-in 900.00 EUR\n")
    f.booked(400, 400, "BK-BBBB2222", 24)
    f.cancelled(400, "BK-BBBB2222", 10)
    # 500: accepted long ago, never booked; 600: desk crashed; 700: desk working now
    f.mail(500, CUSTOMER, "rfq", "customer", 500, 90)
    f.notes(500, "# Shipment 500: Acme Tools GmbH, HAM -> JFK\n- QUOTE SENT mail 501: 500.00 EUR all-in\n")
    f.mail(502, CUSTOMER, "customer_accept", "customer", 500, 80)
    f.mail(600, CUSTOMER, "rfq", "customer", 600, 15)
    f.event(600, "session_start", "desk session for mail 600", 15)
    f.event(600, "session_end", "exit 1: ", 14)
    f.mail(700, CUSTOMER, "rfq", "customer", 700, 2)
    f.event(700, "session_start", "desk session for mail 700", 2)
    # not shipments of ours: a tracking request, and a request from before the desk ran
    f.mail(800, "tracking@hackloop.mail", "tracking_request", "tracking", 800, 5)
    f.mail(900, CUSTOMER, "rfq", "customer", 900, 900)
    # 950: handled while the desk only wrote drafts (its last draft in outbox/ is newer)
    f.mail(950, CUSTOMER, "rfq", "customer", 950, 120)
    f.notes(950, "# Shipment 950: Acme Tools GmbH, HAM -> JFK\n- QUOTE SENT (dry run): 700.00 EUR all-in\n")
    (f.root / "outbox").mkdir()
    (f.root / "outbox" / f"{(NOW - timedelta(minutes=100)):%Y%m%dT%H%M%S%f}-POST-v1_mailbox_messages.json").write_text("{}")
    return f


def test_statuses_come_from_the_desks_files(folder: Folder) -> None:
    d = desk.load(folder.root, now=NOW)
    status = {sid: s["status"] for sid, s in d.shipments.items()}
    assert status == {"100": "booked", "200": "needs_info", "300": "lost", "400": "cancelled", "500": "quoted", "600": "new", "700": "new"}
    s = d.shipments["100"]
    assert (s["company"], s["origin"], s["destination"], s["price_eur"]) == ("Acme Tools GmbH", "HAM", "JFK", 1234.56)
    assert (s["booking"], s["airline"], s["air_eur"]) == ("BK-AAAA1111", "Blue Air", 1000.0)
    assert d.shipments["300"]["price_eur"] == 2000.0 and d.shipments["400"]["price_eur"] == 900.0
    assert d.tracking_requests == 1 and "900" not in d.shipments
    assert "950" not in d.shipments and d.dry_hidden == 1


def test_needs_you_names_what_a_person_has_to_look_at(folder: Folder) -> None:
    d = desk.load(folder.root, now=NOW)
    needs = {sid: s["needs_you"] for sid, s in d.shipments.items() if s["needs_you"]}
    assert needs == {"500": ["Customer accepted, no booking on record"], "600": ["Desk stopped with an error (exit 1)"]}
    assert d.shipments["700"]["running"] and not d.shipments["600"]["running"]
    later = desk.load(folder.root, now=NOW + desk.STALE)  # a session open for longer has died
    assert later.shipments["700"]["needs_you"] == ["Desk session started 11:58 UTC and never finished"]


def test_the_timeline_tells_the_story_with_names(folder: Folder) -> None:
    d = desk.load(folder.root, now=NOW)
    steps = [(i["type"], i["title"]) for i in desk.timeline(d, "100")]
    assert steps == [
        ("mail_in", "Mail from Acme Tools GmbH"), ("mail_out", "Mail to Fast Truck"), ("mail_in", "Mail from Fast Truck"),
        ("mail_out", "Mail to Acme Tools GmbH"), ("desk", "Desk finished"), ("mail_in", "Mail from Acme Tools GmbH"), ("booking", "Booked BK-AAAA1111"), ("mail_in", "Mail from Blue Air"),
    ]


def test_mails_to_several_carriers_are_one_line(folder: Folder) -> None:
    second = "slow-truck@hackloop.mail"
    with (folder.root / "data" / "carriers.csv").open("a") as handle:
        handle.write(f"Slow Truck,trucker,{second},20,,1,start_kit,,\n")
    folder.sent(100, 110, second, 59, subject="Pickup quote 20095 Hamburg to HAM")
    d = desk.load(folder.root, now=NOW)
    line = next(i for i in desk.timeline(d, "100") if i["type"] == "mail_out")
    assert line["title"] == "Mail to 2 truckers" and [p["who"] for p in line["parts"]] == ["Fast Truck", "Slow Truck"]


def test_figures_for_customers_carriers_and_reports(folder: Folder) -> None:
    d = desk.load(folder.root, now=NOW)
    (c,) = desk.customers(d)
    assert (c["company"], c["shipments"], c["booked"], c["lost"], c["win_rate_pct"]) == ("Acme Tools GmbH", 7, 1, 1, 50)
    truck = desk.carriers(d)["truckers"][0]
    assert (truck["asked"], truck["offers"], truck["reply_rate_pct"], truck["median_reply_min"]) == (1, 1, 100, 1.0)
    air = desk.carriers(d)["airlines"][0]
    assert (air["name"], air["bookings"], air["cancelled"]) == ("Blue Air", 1, 1)
    r = desk.reports(d)
    assert [f["count"] for f in r["funnel"]] == [7, 4, 2] and r["lost"] == 1 and r["win_rate_pct"] == 67
    assert r["losses"][0]["words"].startswith("Hi there")
    requests, bookings, mails = desk.strips(d)
    assert [t["tone"] for t in requests["ticks"] if t["tone"] != "none"] == ["bad", "ok", "warn", "lost", "lost", "bad", "open"]
    assert [t["tone"] for t in bookings["ticks"] if t["tone"] != "none"] == ["ok", "lost"]
    (route,) = [r for r in desk.globe(d)["lanes"] if r["origin"] == "HAM" and r["destination"] == "JFK"]
    assert (route["requests"], route["accepted"], route["open"]) == (2, 1, 1)
    today = desk.today(d)
    assert [s["id"] for s in today["running"]] == ["700"] and {s["id"] for s in today["waiting"]} == {"200"}


@pytest.fixture
def client(folder: Folder, monkeypatch) -> TestClient:
    monkeypatch.setattr(desk, "ROOT", folder.root)
    return TestClient(app)


def test_endpoints_only_read(client: TestClient, folder: Folder) -> None:
    for path in ("/crm", "/crm/api/version", "/crm/api/snapshot", "/crm/api/shipments/100", "/crm/api/rates", "/crm/api/search?q=price", "/crm/api/settings"):
        assert client.get(path).status_code == 200, path
    assert client.get("/crm/api/shipments/999").status_code == 404
    assert client.get("/crm/api/search", params={"q": "too high"}).json()["mails"][0]["shipment"] == "300"
    assert client.get("/crm/api/search", params={"q": "BK-AAAA1111"}).json()["mails"][0]["shipment"] == "100"  # the airline's confirmation
    assert all(client.post(path, json={}).status_code == 405 for path in ("/crm/api/snapshot", "/crm/api/shipments/100"))
    before = sorted(p.stat().st_mtime_ns for p in folder.root.rglob("*") if p.is_file())
    client.get("/crm/api/snapshot")
    assert sorted(p.stat().st_mtime_ns for p in folder.root.rglob("*") if p.is_file()) == before  # nothing written


def test_the_version_moves_when_the_desk_writes(client: TestClient, folder: Folder) -> None:
    first = client.get("/crm/api/version").json()["version"]
    folder.event(700, "decision", "quoted", 1)
    assert client.get("/crm/api/version").json()["version"] != first
