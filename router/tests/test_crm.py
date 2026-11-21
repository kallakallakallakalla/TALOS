"""The CRM figures and endpoints, checked against the committed dry-run replay."""

import shutil
from datetime import UTC, datetime
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app import insights
from app.airports import AIRPORTS, distance_km
from app.config import Settings, get_settings
from app.main import app
from app.reference import Reference
from app.replay_data import REPLAY_DIR, ensure_replay_db, replay_labels
from app.storage import MessageStore

SETTINGS = Settings(langfuse_enabled=False)
REFERENCE = Reference(SETTINGS.start_kit_dir)


@pytest.fixture(scope="module")
def replay(tmp_path_factory) -> MessageStore:
    """The replay database, rebuilt from result.json as on a fresh checkout."""
    folder = tmp_path_factory.mktemp("replay")
    shutil.copy(REPLAY_DIR / "result.json", folder)
    return MessageStore(str(ensure_replay_db(folder)))


def _snapshot(store: MessageStore, source: str) -> dict:
    return insights.snapshot(
        store, REFERENCE, SETTINGS, source=source, labels=replay_labels(),
        now=datetime(2026, 11, 21, 18, 0, tzinfo=UTC),
    )  # fmt: skip


def test_replay_database_is_rebuilt_from_the_committed_result(replay: MessageStore) -> None:
    assert len(replay.list_shipments()) == 16
    assert len(replay.all_trucker_requests()) == 35
    assert sum(len(options) for options in replay.all_air_options().values()) == 43
    assert len(replay.outbound_mails()) == 59 and len(replay.mail_index()) == 151
    assert replay.find_outbound(-18).kind == "quote"  # dry-run ids keep their meaning


def test_snapshot_counts_and_money_add_up(replay: MessageStore) -> None:
    snap = _snapshot(replay, "replay")
    k, deals = snap["kpis"], snap["deals"]
    assert (k["requests"], k["quoted"], k["accepted"], k["rejected"], k["open"]) == (16, 14, 8, 5, 3)
    assert k["quoted_eur"] == pytest.approx(sum(d["all_in_eur"] for d in deals if d["all_in_eur"]))
    assert sum(stage["count"] for stage in snap["pipeline"]) == 16
    assert sum(a["requests"] for a in snap["accounts"]) == 16
    assert sum(lane["requests"] for lane in snap["lanes"]) == 16
    # every quote is air freight plus pickup, to the cent (margin is 0)
    for d in deals:
        if d["all_in_eur"] is not None:
            assert d["all_in_eur"] == pytest.approx(d["air_eur"] + d["truck_eur"], abs=0.01)


def test_a_dry_run_shows_no_win_rate_and_no_timings(replay: MessageStore) -> None:
    snap = _snapshot(replay, "replay")
    assert snap["limits"] and snap["kpis"]["win_rate_pct"] is None
    assert snap["kpis"]["median_minutes_to_quote"] is None
    assert all(a["win_rate_pct"] is None for a in snap["accounts"])
    assert not {"win_rate", "time_to_quote"} & {f["key"] for f in snap["findings"]}
    assert all(t["median_response_s"] is None for t in snap["truckers"])

    live = _snapshot(replay, "live")  # the same rows read as live data: both figures appear
    assert live["limits"] == [] and live["kpis"]["win_rate_pct"] == pytest.approx(61.5)
    assert {"win_rate", "time_to_quote"} <= {f["key"] for f in live["findings"]}


def test_deal_figures_follow_the_pricing_rules(replay: MessageStore) -> None:
    deals = {d["id"]: d for d in _snapshot(replay, "replay")["deals"]}
    xenon = deals[947]  # 679 kg, 4.19 cbm: volume weight 699.73 kg is billed
    assert xenon["chargeable_kg"] == 699.73 and xenon["volumetric"] and not xenon["weight_break"]
    assert xenon["truck_mode"] == "quoted" and xenon["truck"]["chosen_is_cheapest"]
    assert xenon["air"]["chosen_rank"] == 1 and xenon["air"]["premium_eur"] == 0
    assert xenon["air"]["speed_days"] == 2 and xenon["air"]["speed_eur"] == pytest.approx(755.7)
    assert xenon["lane"] == "CGN-ORD" and xenon["distance_km"] == distance_km("CGN", "ORD")

    medtech = deals[275]  # 966.93 kg chargeable, billed at the 1000 kg break; no trucker in time
    assert medtech["weight_break"] and medtech["billed_kg"] == 1000 and medtech["truck_mode"] == "estimate"
    assert medtech["goal"] == "fastest" and medtech["air"]["premium_eur"] > 0

    waiting = deals[3940]
    assert waiting["state"] == "AWAITING_INFO" and waiting["missing"] and waiting["all_in_eur"] is None
    assert waiting["pickup"] is False and waiting["truck_mode"] is None


def test_findings_name_their_cases(replay: MessageStore) -> None:
    findings = {f["key"]: f for f in _snapshot(replay, "replay")["findings"]}
    assert findings["truck_estimates"]["value"] == 3 and findings["truck_estimates"]["n"] == 8
    assert findings["weight_breaks"]["value"] == 4 and findings["volumetric"]["value"] == 5
    assert findings["clarifications"]["value"] == 2 and findings["pickup_share"]["value"] == 50.0
    assert all(f["n"] > 0 and f["text"] for f in findings.values())


def test_attention_lists_what_a_person_has_to_do(replay: MessageStore) -> None:
    items = _snapshot(replay, "replay")["attention"]
    assert [(i["shipment_id"], i["level"]) for i in items if i["title"] == "Quoted with an estimated trucker price"] == [(1977, "warning")]
    waiting = [i for i in items if i["title"] == "Waiting for the customer"]
    assert {i["shipment_id"] for i in waiting} == {3637, 3940} and "pickup address" in waiting[0]["detail"]


def test_carriers_lanes_and_mail(replay: MessageStore) -> None:
    snap = _snapshot(replay, "replay")
    airlines = {a["name"]: a for a in snap["airlines"]}
    assert len(airlines) == 29 and len(snap["truckers"]) == 22 and len(snap["zones"]) == 32
    assert sum(a["chosen"] for a in airlines.values()) == 14
    assert airlines["Xenon Cargo Airlines"]["cheapest_in"] == 2
    asked = sum(t["asked"] for t in snap["truckers"])
    assert asked == 35 and sum(t["answered"] for t in snap["truckers"]) == 29  # 3 of 32 answers were declines
    lane = next(lane for lane in snap["lanes"] if lane["lane"] == "CGN-ORD")
    assert lane["requests"] == 2 and lane["accepted"] == 1 and 6700 < lane["distance_km"] < 6900
    assert snap["mail"]["total"] == 151 and snap["mail"]["classification"] == {"labelled": 151, "correct": 151, "wrong": []}
    assert sum(b["count"] for b in snap["mail"]["per_10_minutes"]) == 151


def test_the_conversation_is_in_the_order_it_happened(replay: MessageStore) -> None:
    detail = insights.shipment_detail(replay, REFERENCE, 947, source="replay")
    steps = [m.get("kind") or m["category"] for m in detail["timeline"]]
    assert steps[0] == "rfq" and steps[1:5] == ["trucker_request"] * 4
    assert steps.index("quote") > max(i for i, s in enumerate(steps) if s == "trucker_quote")
    assert steps.index("customer_accept") < steps.index("booking_note")
    assert [o["airline"] for o in detail["options"] if o["chosen"]] == ["Xenon Cargo Airlines"]
    assert [t["name"] for t in detail["truckers"] if t["chosen"]] == ["Elbkante Logistik"]
    assert insights.shipment_detail(replay, REFERENCE, 1, source="replay") is None


def test_every_airport_of_the_start_kit_has_a_place() -> None:
    wanted = {row["airport"] for row in REFERENCE.zones} | {
        code for carrier in REFERENCE.carriers if carrier.kind == "airline" for code in carrier.serves
    }
    assert wanted <= set(AIRPORTS)
    assert all(-90 <= lat <= 90 and -180 <= lon <= 180 for _, lat, lon in AIRPORTS.values())
    assert distance_km("FRA", "JFK") == pytest.approx(6190, abs=40)  # published great-circle distance
    assert distance_km("FRA", None) is None


@pytest.fixture
def client(tmp_path: Path):
    """The app over an empty live database; the replay comes from the committed files."""
    settings = Settings(langfuse_enabled=False, sqlite_db_path=str(tmp_path / "live.db"))
    app.dependency_overrides[get_settings] = lambda: settings
    previous = getattr(app.state, "store", None)
    app.state.store = MessageStore(settings.sqlite_db_path)
    yield TestClient(app)
    app.dependency_overrides.pop(get_settings, None)
    app.state.store = previous


def test_crm_endpoints(client: TestClient) -> None:
    page = client.get("/crm")
    assert page.status_code == 200 and "/crm/assets/app.js" in page.text
    for asset in ("app.js", "views.js", "lib.js", "globe.js", "land.js", "crm.css"):
        assert client.get(f"/crm/assets/{asset}").status_code == 200

    sources = client.get("/crm/api/sources").json()
    assert sources["live"] == {"shipments": 0, "legacy": 0} and sources["default"] == "replay"

    empty = client.get("/crm/api/snapshot").json()  # the live database of this test is empty
    assert empty["kpis"]["requests"] == 0 and empty["findings"] == [] and empty["attention"] == []

    snap = client.get("/crm/api/snapshot", params={"source": "replay"}).json()
    assert snap["kpis"]["requests"] == 16
    assert client.get("/crm/api/version", params={"source": "replay"}).json()["version"] == snap["version"]

    detail = client.get("/crm/api/shipments/947", params={"source": "replay"}).json()
    assert detail["deal"]["all_in_eur"] == 1599.41 and len(detail["timeline"]) == 15
    assert client.get("/crm/api/shipments/947").status_code == 404  # not in the live database

    mails = client.get("/crm/api/mails", params={"source": "replay", "q": "chicago", "limit": 5}).json()
    assert 0 < mails["total"] and len(mails["items"]) <= 5 and all("body" in m for m in mails["items"])
    trucker = client.get("/crm/api/mails", params={"source": "replay", "role": "trucker"}).json()
    assert trucker["total"] == 39 and {m["role"] for m in trucker["items"]} == {"trucker"}

    assert len(client.get("/crm/api/tariffs").json()) == 158


def test_the_page_follows_the_database(client: TestClient) -> None:
    before = client.get("/crm/api/version").json()["version"]
    assert client.get("/crm/api/version").json()["version"] == before  # nothing changed: same token
    app.state.store.add_outbound(kind="quote", to="x@hackloop.mail", subject="s", body="b", shipment_id=1)
    assert client.get("/crm/api/version").json()["version"] != before


def test_system_page_shows_no_secret(client: TestClient) -> None:
    system = client.get("/crm/api/system")
    assert system.status_code == 200 and system.json()["mode"] == "dry_run"
    assert len(system.json()["prompts"]) == 6
    real = Settings()  # whatever keys the local .env holds must never reach the page
    secrets = [str(v) for k, v in real.model_dump().items() if any(w in k for w in ("key", "token", "secret")) and v]
    assert not [s for s in secrets if s in system.text]
