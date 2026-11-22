"""The supervisor console: the action queue and its actions, the round ledger, the carriers
used, the decision trace, the status bar and the guard on the action endpoint."""

import json
import shutil
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app import actions, board, insights
from app.config import Settings, get_settings
from app.main import app
from app.reference import Reference
from app.replay_data import RECORDED, REPLAY_DIR, ensure_replay_db
from app.schemas import ShipmentState
from app.storage import MessageStore
from tests.test_workflow import MAILS, ROUND_4_RFQ, Harness

S = ShipmentState
SETTINGS = Settings(langfuse_enabled=False)
REFERENCE = Reference(SETTINGS.start_kit_dir)
INCOMPLETE = 3940  # "truck pickup from our warehouse": no postal code, no airport
REQUEST_MAIL = 947  # a quote request


@pytest.fixture(scope="module")
def recorded(tmp_path_factory) -> Path:
    """The recorded run as a database file, built once."""
    folder = tmp_path_factory.mktemp("replay")
    shutil.copy(REPLAY_DIR / RECORDED, folder)
    return ensure_replay_db(folder)


@pytest.fixture
def replay(recorded: Path, tmp_path: Path) -> MessageStore:
    """A private copy of the recorded run, so a test may change it."""
    return MessageStore(shutil.copy(recorded, tmp_path / "replay.db"))


def sql(store: MessageStore, statement: str, *args) -> list[sqlite3.Row]:
    with store._connect() as conn:  # noqa: SLF001 - tests shape the data directly
        return conn.execute(statement, args).fetchall()


def queue(store: MessageStore, source: str = "replay", settings: Settings = SETTINGS, now: datetime | None = None, poller: dict | None = None) -> list[dict]:
    now = now or datetime(2026, 11, 21, 18, 0, tzinfo=UTC)
    return insights.snapshot(store, REFERENCE, settings, source=source, labels=None, now=now, poller=poller)["attention"]


# --- the queue ------------------------------------------------------------------------------


def test_every_queue_item_names_its_actions_and_can_be_dismissed_and_restored(replay: MessageStore) -> None:
    items = queue(replay)  # an estimated trucking price or an unsure classification is no case: nobody can act on it
    assert [i["key"] for i in items] == ["awaiting_information:3940:1", "awaiting_information:3637:1"]
    assert all(i["actions"][-1] == "dismiss" and set(i["actions"]) <= set(actions._ACTIONS) | {"dismiss"} for i in items)  # noqa: SLF001
    assert items[0]["actions"] == ["remind_customer", "close_shipment", "dismiss"] and items[0]["needs_person"]
    assert items[0]["title"] == "Nordlicht Audio has not answered our question"

    before = replay.change_token()
    replay.dismiss(items[0]["key"])
    assert replay.change_token() != before  # the page reloads
    assert items[0]["key"] not in [i["key"] for i in queue(replay)]
    assert replay.restore_dismissed() == 1 and len(queue(replay)) == 2


def test_live_cases_are_listed_only_after_a_wait_and_say_how_long(replay: MessageStore) -> None:
    asked_at = datetime.fromisoformat(sql(replay, "SELECT updated_at FROM shipments WHERE id = ?", INCOMPLETE)[0]["updated_at"])
    just_asked = queue(replay, "live", now=asked_at.replace(second=0) + (datetime(1, 1, 1, 0, 1) - datetime(1, 1, 1)))
    assert INCOMPLETE not in [i["shipment_id"] for i in just_asked]  # a question that has only just gone out is work in progress
    later = next(i for i in queue(replay, "live") if i["shipment_id"] == INCOMPLETE)
    assert later["waiting_minutes"] > 30 and later["title"] == "Nordlicht Audio has not answered our question"


def test_a_stopped_poller_is_the_first_case(replay: MessageStore) -> None:
    down = {"enabled": True, "running": False, "last_poll_at": None, "last_error": "401 Unauthorized"}
    first = queue(replay, "live", poller=down)[0]
    assert (first["kind"], first["level"], first["needs_person"]) == ("poller_stopped", "critical", True)
    assert first["detail"] == "401 Unauthorized Restart the router." and first["actions"] == ["dismiss"]
    viewer = {"enabled": False, "running": False, "last_poll_at": None, "last_error": None}  # a process that only reads
    assert "poller_stopped" not in [i["kind"] for i in queue(replay, "live", poller=viewer)]
    assert "poller_stopped" not in [i["kind"] for i in queue(replay, "replay", poller=down)]


def test_no_reminder_is_offered_once_the_customer_was_asked_the_maximum(replay: MessageStore) -> None:
    sql(replay, "UPDATE shipments SET clarifications = 3 WHERE id = ?", INCOMPLETE)
    item = next(i for i in queue(replay) if i["shipment_id"] == INCOMPLETE)
    assert item["actions"] == ["close_shipment", "dismiss"]


def test_a_mail_that_failed_once_and_then_went_through_is_not_in_the_queue(replay: MessageStore) -> None:
    replay.record_failure(REQUEST_MAIL, "timeout")  # stored with its real category afterwards
    replay.record_failure(999_999, "never stored")
    assert [i["mail_id"] for i in queue(replay) if i["kind"] == "mail_failed"] == [999_999]


def test_a_shipment_closed_by_the_operator_leaves_the_queue_and_says_so_in_its_trace(replay: MessageStore) -> None:
    sql(replay, "UPDATE shipments SET state = 'FAILED', error = ? WHERE id = ?", actions.CLOSED_BY_OPERATOR, INCOMPLETE)
    assert INCOMPLETE not in [i["shipment_id"] for i in queue(replay)]
    trace = insights.shipment_detail(replay, REFERENCE, INCOMPLETE, source="replay")["trace"]
    assert (trace[-1]["label"], trace[-1]["tone"]) == ("Closed by an operator", "bad")


# --- rounds ---------------------------------------------------------------------------------


def test_the_ledger_adds_up_and_gives_every_loss_one_cause(replay: MessageStore) -> None:
    ledger = insights.rounds(replay, REFERENCE, SETTINGS, source="replay")
    assert (ledger["total"], ledger["quoted"], ledger["accepted"], ledger["declined"], ledger["no_quote"]) == (16, 14, 8, 5, 0)
    assert (ledger["decided"], ledger["clean"], ledger["lost"], ledger["timed"]) == (13, 8, 5, 0)  # no timings in a dry run
    assert [(c["count"], c["cause"]) for c in ledger["causes"]] == [  # a decline is told in the customer's words
        (3, "That seems too low to be right, so we will not go ahead with it."),
        (2, "These goods are dangerous goods and you quoted without asking us for the dangerous goods documents, so we will not go ahead with it."),
    ]
    for row in ledger["rounds"]:
        assert (row["quote_mail"] is None) == (row["all_in_eur"] is None) == (row["mail_flags"] is None)
        assert (row["cause"] is None) == (row["outcome"] in ("accepted", "awaiting decision", "waiting for customer"))
    assert [r["outcome"] for r in ledger["rounds"] if r["id"] == INCOMPLETE] == ["waiting for customer"]  # an open question is not a loss


def test_the_boards_verdict_wins_over_our_own_record(replay: MessageStore) -> None:
    said = {947: {"verdict": "rejected", "reason": "too_low", "booked": False, "points": None}}
    ledger = insights.rounds(replay, REFERENCE, SETTINGS, source="live", board=said)
    row = next(r for r in ledger["rounds"] if r["id"] == 947)
    assert ledger["board_available"] and row["cause"] == "Board: rejected (too_low)" and row["board"] == said[947]


def test_the_recorded_quotes_break_the_style_rules_and_the_count_says_so(replay: MessageStore) -> None:
    ledger = insights.rounds(replay, REFERENCE, SETTINGS, source="replay")
    assert (ledger["quote_mails"], ledger["mails_clean"]) == (14, 0)
    assert {c["check"]: c["count"] for c in ledger["mail_checks"]}["raw dates"] == 14


GOOD_MAIL = """Hi Klaus,

1599.41 EUR all-in gets your furniture samples from CGN to ORD, and that's the cheapest of the 4 airlines I compared: Xenon Cargo Airlines, 3 days. We pick up at your site on 28 Nov, it flies on 30 Nov and lands on 3 Dec. I can hold the price today until 15:34 UTC. Shall I book it?

Cheers
Alex
TALOS Air Freight"""


@pytest.mark.parametrize(
    ("change", "flag"),
    [
        (("1599.41 EUR all-in gets", "Thank you for your request. 1599.41 EUR all-in gets"), "opens with thanks"),
        (("Shall I book it?", "Please do not hesitate to ask."), "stock phrase"),
        (("Shall I book it?", "Shall I book it?\n\nAll-in price: 1599.41 EUR"), "label lines"),
        (("on 28 Nov", "on 2026-11-28"), "raw dates"),
        (("Shall I book it?", "It is a good rate. It will not last. Shall I book it?"), "more than 5 sentences"),
        (("1599.41 EUR", "**1599.41 EUR**"), "markup"),
        (("TALOS Air Freight", "TALOS Air Freight\n+49 89 1234 5678"), "phone number or footer"),
    ],
)
def test_each_style_rule_fires_on_its_breach_and_only_then(change: tuple[str, str], flag: str) -> None:
    assert insights.mail_checks(GOOD_MAIL) == []
    assert insights.mail_checks(GOOD_MAIL.replace(*change)) == [flag]


# --- performance ------------------------------------------------------------------------------


def test_the_scorecard_counts_the_carriers_used_and_what_customers_asked_for(replay: MessageStore) -> None:
    card = insights.scorecard(replay, REFERENCE, SETTINGS, source="replay")
    assert (card["requests"], card["quoted"], card["pickups"], card["top_airline_share_pct"]) == (16, 14, 8, 28.6)
    assert (len(card["airlines"]), len(card["truckers"])) == (20, 16)  # every airline priced, every trucker asked
    assert card["airlines"][0] == {"name": "Fjordline Air Cargo", "quoted": 4, "accepted": 4, "priced": 4}
    assert sum(a["quoted"] for a in card["airlines"]) == 14 and sum(a["accepted"] for a in card["airlines"]) == 8
    assert sum(t["selected"] for t in card["truckers"]) == 5  # 8 pickups, 3 of them quoted with an estimate
    asks = {a["name"]: a["count"] for a in card["asks"]}
    assert asks["Pickup at the customer's site"] == 8 and asks["Fastest option"] + asks["Cheapest option"] == 16
    assert asks["Dangerous goods"] == 0


def test_insurance_is_found_by_a_word_search_in_the_request_mail(replay: MessageStore) -> None:
    count = lambda: {a["name"]: a["count"] for a in insights.scorecard(replay, REFERENCE, SETTINGS, source="replay")["asks"]}["Insurance"]  # noqa: E731
    assert count() == 0  # nobody asked for it in the recorded run
    sql(replay, "UPDATE processed_messages SET body = body || ' Please include cargo insurance.' WHERE message_id = 947")
    sql(replay, "UPDATE processed_messages SET body = body || ' Bitte mit Transportversicherung anbieten.' WHERE message_id = 275")
    assert count() == 2


# --- the decision trace ---------------------------------------------------------------------


def test_the_trace_tells_why_a_shipment_stands_where_it_stands(replay: MessageStore) -> None:
    trace = insights.shipment_detail(replay, REFERENCE, 275, source="replay")["trace"]
    assert [(t["label"], t["tone"]) for t in trace] == [
        ("Request received", "ok"), ("Trucking estimated", "warn"), ("Air freight priced", "ok"),
        ("Quote sent", "ok"), ("Declined by the customer", "bad"),
    ]  # fmt: skip
    assert "A later offer from Elbkante Logistik was 135 EUR: the estimate was 55 EUR higher." in trace[1]["detail"]
    assert "because the customer asked for the fastest option" in trace[2]["detail"]
    steps = {t["label"]: t["detail"] for t in insights.shipment_detail(replay, REFERENCE, 1977, source="replay")["trace"]}
    assert "no offer had arrived when the quote went out (3 answered without a price)" in steps["Trucking estimated"]
    assert steps["Assumptions made"] == "Weight computed from the pieces; volume computed from the dimensions."
    waiting = insights.shipment_detail(replay, REFERENCE, INCOMPLETE, source="replay")["trace"]
    assert (waiting[-1]["label"], waiting[-1]["tone"]) == ("Details incomplete", "warn")
    assert waiting[-1]["detail"].startswith("Asked the customer once. Still missing: pickup address")
    assert "Recorded reply: it answered the earlier process's quote" in trace[-1]["detail"]  # only said in a dry run
    assert "Recorded reply" not in insights.shipment_detail(replay, REFERENCE, 275, source="live")["trace"][-1]["detail"]


# --- the actions ----------------------------------------------------------------------------


def running(tmp_path: Path, **settings) -> Harness:
    harness = Harness(tmp_path, **settings)
    harness.poller.running = True  # as in the process whose poller reads the mailbox
    return harness


async def act(h: Harness, name: str, **params) -> str:
    return await actions.run(name, store=h.store, poller=h.poller, params=params)


@pytest.mark.anyio
async def test_only_the_process_that_runs_the_poller_may_act(tmp_path: Path) -> None:
    h = Harness(tmp_path)  # the poller exists but is not running: a read-only viewer
    await h.receive(INCOMPLETE)
    assert not actions.may_act(h.poller) and not actions.may_act(None)
    with pytest.raises(actions.ActionError, match="read-only view"):
        await act(h, "close_shipment", shipment_id=INCOMPLETE)
    assert await actions.run("dismiss", store=h.store, poller=None, params={"key": "k"}) == "Item dismissed."
    assert h.store.dismissed() == {"k"}
    assert await actions.run("restore", store=h.store, poller=None, params={}) == "1 dismissed items restored."
    with pytest.raises(actions.ActionError, match="Unknown action"):
        await act(h, "delete_everything")


@pytest.mark.anyio
async def test_a_reminder_asks_again_under_the_work_lock_and_stops_at_the_limit(tmp_path: Path) -> None:
    h = running(tmp_path, send_enabled=True)
    await h.receive(INCOMPLETE)
    ask, held = h.workflow._clarify, []  # noqa: SLF001

    async def watched(*args):
        held.append(h.poller.work_lock.locked())
        await ask(*args)

    h.workflow._clarify = watched  # noqa: SLF001
    assert await act(h, "remind_customer", shipment_id=INCOMPLETE) == f"Question 2 of 3 written to {h.shipment(INCOMPLETE).customer}."
    assert held == [True] and not h.poller.work_lock.locked() and len(h.outbound("clarification")) == 2
    await act(h, "remind_customer", shipment_id=INCOMPLETE)
    with pytest.raises(actions.ActionError, match="already asked 3 times"):
        await act(h, "remind_customer", shipment_id=INCOMPLETE)
    assert h.shipment(INCOMPLETE).state == S.AWAITING_INFO and len(h.outbound("clarification")) == 3  # not closed behind the operator's back


@pytest.mark.anyio
async def test_closing_ends_a_shipment_without_mail_and_only_once(tmp_path: Path) -> None:
    h = running(tmp_path, send_enabled=True)
    await h.receive(INCOMPLETE)
    sent = len(h.platform.sent)
    assert await act(h, "close_shipment", shipment_id=INCOMPLETE) == f"Shipment {INCOMPLETE} closed. No mail was sent."
    assert (h.shipment(INCOMPLETE).state, h.shipment(INCOMPLETE).error) == (S.FAILED, actions.CLOSED_BY_OPERATOR)
    assert len(h.platform.sent) == sent
    for params in ({"shipment_id": INCOMPLETE}, {"shipment_id": 1}, {"shipment_id": "x"}, {}):
        with pytest.raises(actions.ActionError):
            await act(h, "close_shipment", **params)


@pytest.mark.anyio
async def test_a_booking_is_retried_only_when_one_is_open_or_was_given_up(tmp_path: Path) -> None:
    h = running(tmp_path, send_enabled=True)
    await h.receive(ROUND_4_RFQ)
    await h.truckers_reply()
    with pytest.raises(actions.ActionError, match="no open booking"):
        await act(h, "retry_booking", shipment_id=ROUND_4_RFQ)  # QUOTED: the customer has not accepted

    shipment = h.shipment()
    shipment.state, shipment.booking_attempts, shipment.error = S.FAILED, 3, "not booked after 3 attempts"
    h.store.save_shipment(shipment)
    assert await act(h, "retry_booking", shipment_id=ROUND_4_RFQ) == "Booking attempted; shipment 947 is now BOOKED (BK-ERC5OOW3)."
    assert len(h.platform.bookings) == 1 and h.shipment().error is None
    with pytest.raises(actions.ActionError, match="no open booking"):
        await act(h, "retry_booking", shipment_id=ROUND_4_RFQ)  # booked: never twice


@pytest.mark.anyio
async def test_a_given_up_mail_is_processed_again_and_never_lost(tmp_path: Path) -> None:
    h = running(tmp_path)
    mail = MAILS[ROUND_4_RFQ]

    async def from_mailbox(mail_id: int):
        return MAILS[mail_id]

    h.poller._mailbox_client.get_message = from_mailbox  # noqa: SLF001
    for _ in range(3):
        h.store.record_failure(mail.id, "gateway timeout")
    h.poller._give_up(mail, "gateway timeout")  # noqa: SLF001 - what the poller does after the third failure
    assert [i["actions"] for i in queue(h.store, "live", h.settings) if i["kind"] == "mail_failed"] == [["retry_mail", "dismiss"]]

    classify = h.poller._classifier.classify  # noqa: SLF001

    async def down(*args):
        raise RuntimeError("gateway still down")

    h.poller._classifier.classify = down  # noqa: SLF001
    with pytest.raises(actions.ActionError, match="failed again: gateway still down"):
        await act(h, "retry_mail", mail_id=mail.id)
    assert h.store.get_message(mail.id).decision.reasoning.startswith(actions.GAVE_UP)  # still stored, still in the queue
    assert h.store.message_failures()[0]["attempts"] == 4 and not h.poller.work_lock.locked()

    h.poller._classifier.classify = classify  # noqa: SLF001
    assert await act(h, "retry_mail", mail_id=mail.id) == f"Mail {mail.id} processed again, classified as rfq."
    assert h.store.message_failures() == [] and h.shipment().state == S.PRICING
    with pytest.raises(actions.ActionError, match="not among the failed mails"):
        await act(h, "retry_mail", mail_id=mail.id)


# --- sessions -------------------------------------------------------------------------------


def test_a_session_is_the_request_as_it_happened(replay: MessageStore) -> None:
    events = insights.session_events(replay, REFERENCE, 947, source="replay")
    assert [(e["actor"], e["title"]) for e in events][:10] == [
        ("customer", "Request from Rheintal Elektronik GmbH"), ("talos", "Understood the request"),
        ("talos", "Asked 4 truckers for a pickup offer"),
        ("trucker", "Offer from Maintal Transport Logistik: 192.97 EUR, pickup within 13 h"),
        ("trucker", "Offer from Elbkante Logistik: 115.98 EUR, pickup within 29 h"),
        ("trucker", "Offer from Westfalenroute: 187.97 EUR, pickup within 11 h"),
        ("trucker", "Offer from Bodenseefracht: 226.96 EUR, pickup within 12 h"),
        ("talos", "Compared the airlines"), ("talos", "Chose the pickup"),
        ("talos", "Quote written to Rheintal Elektronik GmbH: 1,599.41 EUR all-in"),
    ]  # fmt: skip
    assert len({e["id"] for e in events}) == len(events) and all(e["session"] == 947 for e in events)
    assert events[0]["detail"].startswith("Read as a quote request, 99% sure") and "679 kg" in events[0]["body"]
    assert [e["title"] for e in events if e["id"] in ("947:in:1156", "947:booked")] == ["Rheintal Elektronik GmbH accepted the quote", "Booked"]
    waiting = insights.session_events(replay, REFERENCE, INCOMPLETE, source="replay")
    assert [(e["title"], e["tone"]) for e in waiting][2:] == [("Details are missing", "warn"), ("Asked Nordlicht Audio for the missing details", "info")]
    assert insights.session_events(replay, REFERENCE, 1, source="replay") is None


def test_sessions_are_listed_by_latest_activity_and_new_events_are_found(replay: MessageStore) -> None:
    listed = insights.sessions(replay, REFERENCE, source="replay")
    assert len(listed) == 16 and listed[0]["id"] == INCOMPLETE and listed[0]["open"] and not listed[-1]["open"]
    before = {e["id"] for e in insights.recent_events(replay, REFERENCE, source="replay")}
    replay.add_outbound(kind="clarification", to="mira@nordlicht-audio.example", subject="Re: Air freight quote to PVG", body="Which airport?", shipment_id=INCOMPLETE)
    new = [e for e in insights.recent_events(replay, REFERENCE, source="replay") if e["id"] not in before]
    assert [e["title"] for e in new] == ["Asked Mira for the missing details"]  # what the live stream would push


@pytest.mark.anyio
async def test_the_live_stream_carries_what_the_router_does_as_it_does_it(tmp_path: Path) -> None:
    h = Harness(tmp_path, send_enabled=True)
    reference = Reference(h.settings.start_kit_dir, h.settings.sqlite_db_path)
    titles = lambda: [e["title"] for e in insights.session_events(h.store, reference, ROUND_4_RFQ, source="live") or []]  # noqa: E731
    await h.receive(ROUND_4_RFQ)
    assert titles() == ["Request from Rheintal Elektronik", "Understood the request", "Asked 4 truckers for a pickup offer", "Compared the airlines"]
    await h.truckers_reply()
    assert titles()[-2:] == ["Chose the pickup", "Quote written to Rheintal Elektronik: 1,671.40 EUR all-in"]
    assert "Offer from Westfalenroute: 187.97 EUR, pickup within 11 h" in titles()  # the price once the offer has been read


# --- the board ------------------------------------------------------------------------------


def test_the_board_is_read_tolerantly() -> None:
    entry = {"request_id": 947, "verdict": "rejected", "reason": "too_low", "booked": False, "points": 0}
    expected = {947: {"verdict": "rejected", "reason": "too_low", "booked": False, "points": 0, "kinds": None}}
    assert board.normalise([entry]) == expected
    assert board.normalise({"team": "TALOS", "requests": [entry]}) == expected
    assert board.normalise({"requests": [{"team_reference": "TALOS-947-1", "status": "accepted"}]})[947]["verdict"] == "accepted"
    # the answer also lists our bookings; the requests are the list with verdicts, and points come per kind
    both = {"bookings": [{"id": 1, "booking_reference": "BK-1"}],
            "requests": [{"message_id": 947, "verdict": "accepted", "points": {"accuracy": 45.0, "time": 12.5, "note": "x"}, "total": 57.5}]}  # fmt: skip
    assert board.normalise(both) == {947: {"verdict": "accepted", "reason": None, "booked": None, "points": 57.5, "kinds": {"accuracy": 45.0, "time": 12.5}}}
    for garbage in (None, "down", 3, {}, {"requests": []}, [1, "x"], [{"verdict": "accepted"}]):
        assert board.normalise(garbage) == {}
    assert board.seen == {"entries": 1, "usable": 0, "fields": ["verdict"]}  # what to look at when nothing matches


# --- the endpoints --------------------------------------------------------------------------


@pytest.fixture
def client(tmp_path: Path, replay: MessageStore, monkeypatch: pytest.MonkeyPatch):
    """The app over an empty live database and a private copy of the dry run."""
    settings = Settings(langfuse_enabled=False, mailbox_poll_enabled=False, sqlite_db_path=str(tmp_path / "live.db"))
    monkeypatch.setattr("app.crm_api.replay_store", lambda: replay)
    app.dependency_overrides[get_settings] = lambda: settings
    previous = (getattr(app.state, "store", None), getattr(app.state, "poller", None))
    app.state.store, app.state.poller = MessageStore(settings.sqlite_db_path), None
    yield TestClient(app, base_url="http://localhost:8000")
    app.dependency_overrides.pop(get_settings, None)
    app.state.store, app.state.poller = previous


def test_ledger_and_carriers_are_served_for_both_sources(client: TestClient) -> None:
    assert client.get("/crm/api/rounds", params={"source": "replay"}).json()["total"] == 16
    assert client.get("/crm/api/levers").status_code == 404  # the tuning page and its figures are gone
    assert client.get("/crm/api/rounds").json()["total"] == 0  # the live database of this test is empty
    assert client.get("/crm/api/scorecard", params={"source": "replay"}).json()["airlines"][0]["name"] == "Fjordline Air Cargo"
    empty = client.get("/crm/api/scorecard").json()
    assert (empty["requests"], empty["airlines"], empty["truckers"], empty["top_airline_share_pct"]) == (0, [], [], None)
    snap = client.get("/crm/api/snapshot", params={"source": "replay"}).json()
    assert snap["can_act"] is False and snap["sending"] is False and snap["poller"] is None and snap["dismissed"] == 0
    assert client.get("/crm/api/snapshot").json()["poller"] == {"enabled": False, "running": False, "last_poll_at": None, "last_error": None}


def test_sessions_are_served_and_a_replay_is_streamed_event_by_event(client: TestClient) -> None:
    listed = client.get("/crm/api/sessions", params={"source": "replay"}).json()
    assert len(listed) == 16 and client.get("/crm/api/sessions").json() == []
    stored = client.get("/crm/api/sessions/947", params={"source": "replay"}).json()["events"]
    assert client.get("/crm/api/sessions/1", params={"source": "replay"}).status_code == 404
    with client.stream("GET", "/crm/api/stream", params={"source": "replay", "session": 947, "replay": "true", "pause": 0}) as response:
        assert response.headers["content-type"].startswith("text/event-stream")
        lines = [line for line in response.iter_lines() if line]
    kinds = [line.removeprefix("event: ") for line in lines if line.startswith("event: ")]
    sent = [json.loads(line.removeprefix("data: ")) for line in lines if line.startswith("data: ")]
    assert kinds == ["step"] * len(stored) + ["done"] and sent[:-1] == stored and sent[-1] == {"events": len(stored)}


def test_the_version_poll_carries_the_status_bar(client: TestClient) -> None:
    live = client.get("/crm/api/version").json()
    shown = client.get("/crm/api/version", params={"source": "replay"}).json()
    assert live["version"] != shown["version"]
    health = shown["health"]
    assert (health["state"], health["label"], health["mode"]) == ("viewer", "Read-only view", "dry_run")  # this process reads no mailbox
    assert health["uptime_s"] >= 0 and health["started_at"].endswith("+00:00")
    strips = {strip["key"]: strip for strip in health["strips"]}
    assert list(strips) == ["requests", "bookings", "mail"]  # no mailbox strip where the mailbox is not read
    assert strips["requests"]["text"] == "14/16" and [t["tone"] for t in strips["requests"]["ticks"]][-3:] == ["ok", "warn", "warn"]
    assert strips["requests"]["ticks"][-1] == {"tone": "warn", "tip": "Request 3940: waiting for the customer", "href": "#/shipment/3940"}
    assert all(t["tone"] == "none" for t in {s["key"]: s for s in live["health"]["strips"]}["requests"]["ticks"])  # the live database is empty

    app.state.store = None
    broken = Settings(langfuse_enabled=False, mailbox_poll_enabled=False, sqlite_db_path="/proc/nope/live.db")
    app.dependency_overrides[get_settings] = lambda: broken
    assert client.get("/crm/api/version", params={"source": "replay"}).json()["health"]["state"] == "viewer"  # the dry run is still readable


def test_the_status_strips_show_failed_polls_bookings_and_mails(replay: MessageStore) -> None:
    now = datetime(2026, 11, 21, 18, 0, tzinfo=UTC)
    polls = [((now - timedelta(seconds=5 * i)).isoformat(), i not in (40, 41)) for i in range(200)]  # two failures, 3 minutes ago
    sql(replay, "UPDATE shipments SET state = 'FAILED', error = 'not booked after 3 attempts' WHERE id = 947")
    sql(replay, "UPDATE shipments SET state = 'FAILED', error = ? WHERE id = ?", actions.CLOSED_BY_OPERATOR, INCOMPLETE)
    sql(replay, "UPDATE processed_messages SET reasoning = 'processing failed: timeout', confidence = 0 WHERE message_id = 3940")
    running = {"enabled": True, "running": True, "last_error": None}
    strips = {s["key"]: s for s in insights.health(replay.recent(shipments=30, mails=200), polls, running, now=now)}
    mailbox = strips["mailbox"]["ticks"]
    assert len(mailbox) == insights.HEALTH_MINUTES and [t["tone"] for t in mailbox].count("bad") == 1
    assert next(t["tip"] for t in mailbox if t["tone"] == "bad") == "17:56 UTC · 2 of 12 polls failed" and mailbox[0]["tone"] == "none"
    tips = {t["tip"]: t["tone"] for strip in strips.values() for t in strip["ticks"]}
    assert tips["Request 947: failed, not booked after 3 attempts"] == "bad" and tips["Request 947: not booked after 3 attempts"] == "bad"
    assert tips[f"Request {INCOMPLETE}: closed by an operator"] == "none" and tips["Mail 3940: given up after repeated failures"] == "bad"
    assert strips["mailbox"]["state"] == "ok" and strips["requests"]["state"] == "ok"  # the newest request is not the failed one
    stopped = insights.health(replay.recent(shipments=5, mails=5), [], {"enabled": True, "running": False, "last_error": "401"}, now=now)[0]
    assert (stopped["state"], stopped["text"]) == ("bad", "stopped")


def test_dismissing_applies_to_the_data_set_that_is_shown(client: TestClient, replay: MessageStore) -> None:
    key = client.get("/crm/api/snapshot", params={"source": "replay"}).json()["attention"][0]["key"]
    done = client.post("/crm/api/actions/dismiss", params={"source": "replay"}, json={"key": key})
    assert done.status_code == 200 and replay.dismissed() == {key} and app.state.store.dismissed() == set()
    assert key not in [i["key"] for i in client.get("/crm/api/snapshot", params={"source": "replay"}).json()["attention"]]
    assert client.post("/crm/api/actions/restore", params={"source": "replay"}, json={}).json()["message"] == "1 dismissed items restored."


def test_actions_are_refused_where_they_do_not_apply(client: TestClient) -> None:
    close = {"shipment_id": INCOMPLETE}
    dry = client.post("/crm/api/actions/close_shipment", params={"source": "replay"}, json=close)
    assert dry.status_code == 409 and "only exists on the live router" in dry.json()["detail"]
    viewer = client.post("/crm/api/actions/close_shipment", json=close)  # live data, but no poller in this process
    assert viewer.status_code == 409 and "read-only view" in viewer.json()["detail"]
    assert client.post("/crm/api/actions/nonsense", json={}).status_code == 409
    assert client.post("/crm/api/actions/dismiss", json={}).status_code == 409  # no item named


def test_an_action_is_only_taken_from_the_crm_page_itself(client: TestClient) -> None:
    send = lambda **kw: client.post("/crm/api/actions/dismiss", **kw).status_code  # noqa: E731
    body = '{"key": "x"}'
    json_type = {"Content-Type": "application/json"}
    assert send(content=body, headers={"Content-Type": "text/plain"}) == 415  # what a foreign page can send unasked
    assert send(content=body, headers=json_type | {"Origin": "http://evil.example"}) == 403
    assert send(content=body, headers=json_type | {"Sec-Fetch-Site": "cross-site"}) == 403
    assert send(content=body, headers=json_type | {"Host": "evil.example:8000", "Origin": "http://evil.example:8000"}) == 403  # a foreign name pointed here
    assert send(content="{oops", headers=json_type) == 400 and send(content="[1]", headers=json_type) == 400
    assert app.state.store.dismissed() == set()
    assert send(content=body, headers=json_type | {"Origin": "http://localhost:8000", "Sec-Fetch-Site": "same-origin"}) == 200

    named = Settings(langfuse_enabled=False, mailbox_poll_enabled=False, sqlite_db_path=app.state.store._db_path, crm_action_hosts="talos.internal")  # noqa: SLF001
    app.dependency_overrides[get_settings] = lambda: named
    assert send(content=body, headers=json_type | {"Host": "talos.internal:8000"}) == 200
