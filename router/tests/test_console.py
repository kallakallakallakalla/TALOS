"""The supervisor console: the action queue and its actions, the round ledger, the tuning
verdicts, the decision trace, the record of LLM use, and the guard on the action endpoint."""

import shutil
import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from fastapi.testclient import TestClient

from app import actions, board, insights
from app.config import Settings, get_settings
from app.llm import StructuredLLM, current_mail
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
OTHER_MAIL, REQUEST_MAIL = 3561, 947  # a mail that needed no answer, and a quote request


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


def lever(store: MessageStore, key: str, source: str = "replay", settings: Settings = SETTINGS) -> dict:
    return next(x for x in insights.levers(store, REFERENCE, settings, source=source) if x["key"] == key)


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


# --- tuning ---------------------------------------------------------------------------------


def test_levers_put_the_suggested_change_first_and_say_when_data_is_thin(replay: MessageStore) -> None:
    levers = insights.levers(replay, REFERENCE, SETTINGS, source="replay")
    assert [(x["key"], x["verdict"]["tone"]) for x in levers] == [
        ("llm_usage", "change"), ("trucker_wait", "open"), ("silent_truckers", "open"), ("fallback_trucking", "open")]  # fmt: skip
    mails = levers[0]  # no token is recorded in this data, but the stored mails already say this much
    assert mails["figure"] == "73 of 151" and mails["verdict"]["text"].startswith("48% of all mails needed no answer.")
    assert "68 of them arrived on a shipment that was already booked or closed." in mails["text"]
    estimate = levers[-1]  # two cases: shown, not acted on
    assert estimate["n"] == 2 and estimate["figure"] == "+78%" and "Too few to change the rate" in estimate["verdict"]["text"]
    assert lever(replay, "trucker_wait")["figure"] == "3 of 8"  # the three estimates waited the full time


def test_the_estimate_verdict_follows_the_offers_that_arrived_later(replay: MessageStore) -> None:
    deals = insights.snapshot(replay, REFERENCE, SETTINGS, source="replay")["deals"]
    estimates = {d["id"]: d["truck_eur"] for d in deals if d["truck_mode"] == "estimate"}
    assert sorted(estimates) == [275, 431, 1977]

    def later_offers(factor: float) -> dict:
        for shipment_id, estimate in estimates.items():
            sql(replay, "UPDATE trucker_requests SET answered = 1, price_eur = ? WHERE shipment_id = ?", round(estimate * factor, 2), shipment_id)
        return lever(replay, "fallback_trucking")

    assert later_offers(1.0)["verdict"]["tone"] == "keep"  # three later offers, all as estimated
    half = later_offers(0.5)
    assert half["verdict"]["tone"] == "change" and half["figure"] == "+100%"
    assert half["suggest"] == {"name": "FALLBACK_TRUCK_EUR_PER_KG", "now": "0.28", "to": "0.14"}
    sql(replay, "UPDATE trucker_requests SET answered = 0, price_eur = NULL WHERE shipment_id IN (275, 431, 1977)")
    assert lever(replay, "fallback_trucking")["verdict"]["tone"] == "open"


def test_the_trucker_wait_is_judged_from_live_offer_timings(replay: MessageStore) -> None:
    arrives_after = lambda seconds: sql(  # noqa: E731
        replay,
        """UPDATE trucker_requests SET sent_at = (SELECT strftime('%Y-%m-%dT%H:%M:%S+00:00', datetime(m.received_at, ?))
           FROM processed_messages m WHERE m.message_id = reply_message_id) WHERE price_eur IS NOT NULL""",
        f"-{seconds} seconds",
    )
    arrives_after(20)
    wait = lever(replay, "trucker_wait", "live")
    assert wait["verdict"]["tone"] == "change" and "25 s would have caught all" in wait["verdict"]["text"]
    assert wait["suggest"] == {"name": "TRUCKER_WAIT_SECONDS", "now": "60", "to": "25"}
    assert lever(replay, "trucker_wait", "live", Settings(langfuse_enabled=False, trucker_wait_seconds=25))["verdict"]["tone"] == "keep"
    arrives_after(90)  # every offer later than the 60 s wait: nothing to recommend
    assert lever(replay, "trucker_wait", "live")["verdict"]["tone"] == "open"
    assert lever(replay, "trucker_wait", "replay")["verdict"]["tone"] == "open"  # a dry run has no timings


def test_a_trucker_is_only_judged_after_enough_requests(replay: MessageStore) -> None:
    assert lever(replay, "silent_truckers")["verdict"]["tone"] == "open"  # asked twice each so far
    sql(replay, "UPDATE trucker_requests SET answered = 0, price_eur = NULL WHERE trucker = 'westfalenroute@hackloop.mail'")
    quiet = lever(replay, "silent_truckers")
    assert quiet["verdict"]["tone"] == "change" and {"name": "Westfalenroute", "asked": 5, "answered": 0} in quiet["rows"]
    sql(replay, "UPDATE trucker_requests SET answered = 1, price_eur = 100")
    assert lever(replay, "silent_truckers")["verdict"]["tone"] == "keep"


def test_time_to_quote_is_a_live_lever_only(replay: MessageStore) -> None:
    assert "time_to_quote" not in [x["key"] for x in insights.levers(replay, REFERENCE, SETTINGS, source="replay")]
    late = lever(replay, "time_to_quote", "live")  # the recording's clocks are mixed: every quote looks late
    assert late["verdict"]["tone"] == "change" and late["n"] == 14


def call(prompt: str, mail: int | None, tokens: int) -> dict:
    return {"prompt": prompt, "model": "gpt-6-luna", "message_id": mail, "input_tokens": tokens, "cached_tokens": 0,
            "output_tokens": 0, "reasoning_tokens": 0, "seconds": 0.4}  # fmt: skip


def test_llm_use_is_summed_by_step_and_flags_tokens_spent_on_mails_without_answer(replay: MessageStore) -> None:
    assert "Token counts are recorded from the router's next start on." in lever(replay, "llm_usage")["text"]
    replay.record_llm_call(call("router-classification", OTHER_MAIL, 0))
    assert "reported no token counts" in lever(replay, "llm_usage")["text"]  # a call, but no usage: no division

    replay.record_llm_call(call("router-classification", OTHER_MAIL, 100))
    replay.record_llm_call(call("talos-compose-mail", REQUEST_MAIL, 900))
    usage = replay.llm_usage()
    assert [(row["prompt"], row["calls"]) for row in usage["by_prompt"]] == [("talos-compose-mail", 1), ("router-classification", 2)]
    assert {row["category"]: row["tokens"] for row in usage["by_category"]} == {"rfq": 900, "other": 100}
    kept = lever(replay, "llm_usage")
    assert kept["verdict"]["tone"] == "keep" and kept["figure"] == "125" and kept["rows"][0]["name"] == "writing mail"  # 1000 tokens / 8 accepted

    replay.record_llm_call(call("router-classification", OTHER_MAIL, 400))
    assert lever(replay, "llm_usage")["verdict"]["tone"] == "change"  # 500 of 1400 tokens on mails that needed no answer


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


# --- the record of LLM use ------------------------------------------------------------------


def completion(**usage) -> SimpleNamespace:
    return SimpleNamespace(usage=SimpleNamespace(**usage) if usage else None)


def test_every_call_reports_its_tokens_and_bookkeeping_never_breaks_a_call() -> None:
    llm, seen = StructuredLLM(SETTINGS, openai_client=object()), []
    llm._record("p", "m", completion(prompt_tokens=10, completion_tokens=5), 0.1)  # noqa: SLF001 - no listener: nothing happens
    llm.on_usage = seen.append
    details = {"prompt_tokens_details": SimpleNamespace(cached_tokens=4), "completion_tokens_details": SimpleNamespace(reasoning_tokens=2)}
    for answer in (completion(prompt_tokens=10, completion_tokens=5, **details), completion(), MagicMock()):
        llm._record("talos-compose-mail", "gpt-6-luna", answer, 1.23456)  # noqa: SLF001
    assert seen[0] == {"prompt": "talos-compose-mail", "model": "gpt-6-luna", "message_id": None, "input_tokens": 10,
                       "cached_tokens": 4, "output_tokens": 5, "reasoning_tokens": 2, "seconds": 1.235}  # fmt: skip
    assert [(c["input_tokens"], c["output_tokens"]) for c in seen[1:]] == [(0, 0), (0, 0)]  # no usage, or not a number

    def broken(_: dict) -> None:
        raise sqlite3.OperationalError("database is locked")

    llm.on_usage = broken
    llm._record("p", "m", completion(prompt_tokens=1, completion_tokens=1), 0.1)  # noqa: SLF001 - must not raise


@pytest.mark.anyio
async def test_calls_made_for_a_mail_carry_its_id_and_the_timer_carries_none(tmp_path: Path) -> None:
    h = Harness(tmp_path)
    h.poller._classifier._llm.on_usage = h.store.record_llm_call  # noqa: SLF001 - the one client of this harness
    await h.receive(ROUND_4_RFQ)
    assert current_mail.get() is None  # reset after the mail
    calls = sql(h.store, "SELECT prompt, message_id FROM llm_calls ORDER BY id")
    assert [tuple(c) for c in calls] == [("router-classification", 947), ("talos-extract-rfq", 947)]
    await h.wait(61)  # the trucker deadline passes: the quote is written by the timer
    assert sql(h.store, "SELECT message_id FROM llm_calls ORDER BY id DESC LIMIT 1")[0]["message_id"] is None

    async def fails(_):
        raise RuntimeError("boom")

    h.poller._process = fails  # noqa: SLF001
    with pytest.raises(RuntimeError):
        await h.poller.process_message(MAILS[ROUND_4_RFQ])
    assert current_mail.get() is None  # reset even when the mail fails


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


def test_ledger_and_levers_are_served_for_both_sources(client: TestClient) -> None:
    assert client.get("/crm/api/rounds", params={"source": "replay"}).json()["total"] == 16
    assert [x["key"] for x in client.get("/crm/api/levers", params={"source": "replay"}).json()][0] == "llm_usage"
    assert client.get("/crm/api/rounds").json()["total"] == 0  # the live database of this test is empty
    assert {x["verdict"] and x["verdict"]["tone"] for x in client.get("/crm/api/levers").json()} <= {"open", "keep", None}
    snap = client.get("/crm/api/snapshot", params={"source": "replay"}).json()
    assert snap["can_act"] is False and snap["sending"] is False and snap["poller"] is None and snap["dismissed"] == 0
    assert client.get("/crm/api/snapshot").json()["poller"] == {"enabled": False, "running": False, "last_poll_at": None, "last_error": None}


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
