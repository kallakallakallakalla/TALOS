import asyncio
import time
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
from fastapi.testclient import TestClient

from app.classifier import DecisionClassifier
from app.config import Settings, get_settings
from app.input_client import InputApiClient
from app.mailbox_client import MailboxClient, mailbox_message_to_payload
from app.main import app, get_classifier, get_mailbox_client, get_store
from app.poller import MailboxPoller
from app.schemas import ClassificationDecision, EmailCategory
from app.storage import MessageStore


class _FakeCompletions:
    def __init__(self, parsed_result: ClassificationDecision | None) -> None:
        self._parsed_result = parsed_result

    async def parse(self, **kwargs):
        return SimpleNamespace(
            choices=[
                SimpleNamespace(
                    message=SimpleNamespace(parsed=self._parsed_result),
                )
            ]
        )


class _FakeOpenAIClient:
    def __init__(self, parsed_result: ClassificationDecision | None) -> None:
        self.chat = SimpleNamespace(completions=_FakeCompletions(parsed_result))


@pytest.fixture
def test_settings() -> Settings:
    return Settings(
        langfuse_enabled=False,
        decision_api_key="test-key",
        decision_model="gpt-6-luna",
        input_api_url="",
    )


def _sample_decision() -> ClassificationDecision:
    return ClassificationDecision(
        category=EmailCategory.RFQ,
        confidence=0.97,
        reasoning="Email asks for pricing and lead time for 500 units.",
    )


def test_health() -> None:
    client = TestClient(app)
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


@pytest.mark.anyio
async def test_placeholder_input_client(test_settings: Settings) -> None:
    client = InputApiClient(test_settings)
    default_item = await client.fetch_input()
    assert default_item.id == "sample-1"
    assert default_item.source == "placeholder"

    tracking_item = await client.fetch_input("sample-2")
    assert tracking_item.id == "sample-2"
    assert "Tracking" in tracking_item.text


def test_run_endpoint_with_placeholder_and_mock_llm(test_settings: Settings) -> None:
    fake_classifier = DecisionClassifier(
        settings=test_settings,
        openai_client=_FakeOpenAIClient(_sample_decision()),
    )
    app.dependency_overrides[get_classifier] = lambda: fake_classifier
    try:
        client = TestClient(app)
        response = client.post("/run", json={"input_id": "sample-1"})
        assert response.status_code == 200
        data = response.json()
        assert data["input"]["id"] == "sample-1"
        assert data["input"]["source"] == "placeholder"
        assert data["model"] == "gpt-6-luna"
        assert data["decision"]["category"] == "rfq"
    finally:
        app.dependency_overrides.clear()


@pytest.mark.parametrize(
    "category",
    list(EmailCategory),
)
def test_classify_direct_endpoint_all_enums(
    test_settings: Settings, category: EmailCategory
) -> None:
    decision = ClassificationDecision(
        category=category,
        confidence=0.92,
        reasoning=f"Classified as {category.value}.",
    )
    fake_classifier = DecisionClassifier(
        settings=test_settings,
        openai_client=_FakeOpenAIClient(decision),
    )
    app.dependency_overrides[get_classifier] = lambda: fake_classifier
    try:
        client = TestClient(app)
        response = client.post(
            "/classify",
            json={"id": "mail-42", "text": f"Sample email for {category.value}"},
        )
        assert response.status_code == 200
        data = response.json()
        assert data["input"]["id"] == "mail-42"
        assert data["decision"]["category"] == category.value
    finally:
        app.dependency_overrides.clear()


def test_classify_handles_unparsed_response(test_settings: Settings) -> None:
    fake_classifier = DecisionClassifier(
        settings=test_settings,
        openai_client=_FakeOpenAIClient(None),
    )
    app.dependency_overrides[get_classifier] = lambda: fake_classifier
    try:
        client = TestClient(app)
        response = client.post("/run")
        assert response.status_code == 502
    finally:
        app.dependency_overrides.clear()


class _CountingCompletions:
    def __init__(self, category: EmailCategory = EmailCategory.RFQ, fail_on_text: str | None = None) -> None:
        self.calls: list[str] = []
        self._category = category
        self._fail_on_text = fail_on_text

    async def parse(self, **kwargs):
        user_msg = kwargs["messages"][1]["content"]
        self.calls.append(user_msg)
        if self._fail_on_text and self._fail_on_text in user_msg:
            raise RuntimeError("Simulated LLM failure")
        decision = ClassificationDecision(
            category=self._category,
            confidence=0.95,
            reasoning="Automated test classification.",
        )
        return SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(parsed=decision))]
        )


class _CountingOpenAIClient:
    def __init__(self, category: EmailCategory = EmailCategory.RFQ, fail_on_text: str | None = None) -> None:
        self.completions = _CountingCompletions(category=category, fail_on_text=fail_on_text)
        self.chat = SimpleNamespace(completions=self.completions)


@pytest.mark.anyio
async def test_mailbox_client_parses_flexible_payload_and_headers() -> None:
    captured: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        captured["api_key"] = request.headers.get("X-API-Key")
        return httpx.Response(
            200,
            json={
                "messages": [
                    {
                        "id": 42,
                        "from": "customer@shipper.de",
                        "to": "talos@hackloop.mail",
                        "subject": "Quote FRA-JFK 1200kg",
                        "body": "Please quote 1200 kg general cargo FRA to JFK.",
                        "in_reply_to": 40,
                        "references": [38, 40],
                        "attachments": [
                            {
                                "id": 7,
                                "filename": "packing_list.pdf",
                                "content_type": "application/pdf",
                                "size": 2048,
                            }
                        ],
                        "created_at": "2026-11-21T15:00:00Z",
                        "round": 3,
                    }
                ],
                "next_since_id": 42,
            },
        )

    settings = Settings(
        langfuse_enabled=False,
        hackloop_base_url="https://api.start-hack.5u.ai",
        hackloop_api_key="secret-team-key",
    )
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http_client:
        client = MailboxClient(settings, http_client=http_client)
        result = await client.poll_messages(since_id=5, limit=50)

    assert captured["api_key"] == "secret-team-key"
    assert "since_id=5" in str(captured["url"])
    assert "limit=50" in str(captured["url"])
    assert result.next_since_id == 42
    assert len(result.messages) == 1
    msg = result.messages[0]
    assert msg.id == 42
    assert msg.sender == "customer@shipper.de"
    assert msg.references == [38, 40]
    assert msg.attachments[0].filename == "packing_list.pdf"

    payload = mailbox_message_to_payload(msg)
    assert payload.id == "42"
    assert payload.source == "mailbox"
    assert "Quote FRA-JFK 1200kg" in payload.text
    assert payload.metadata["references"] == [38, 40]
    assert payload.metadata["attachments"][0]["filename"] == "packing_list.pdf"
    assert payload.metadata["extra"] == {"round": 3}


def test_sqlite_store_persists_across_restarts_and_deduplicates(tmp_path: Path) -> None:
    db_file = str(tmp_path / "router.db")
    store = MessageStore(db_path=db_file)
    assert store.get_since_id() == 0

    from app.mailbox_client import parse_mailbox_message

    msg = parse_mailbox_message(
        {
            "id": 10,
            "from": "carrier@air.com",
            "subject": "Rate update",
            "body": "Updated tariff attached.",
            "references": [9],
            "attachments": [],
        }
    )
    decision = ClassificationDecision(
        category=EmailCategory.TARIFF_UPDATE,
        confidence=0.91,
        reasoning="Tariff update.",
    )
    assert store.save_processed_message(msg, decision, model="gpt-6-luna", trace_id="tr-1") is True
    assert store.save_processed_message(msg, decision, model="gpt-6-luna", trace_id="tr-1") is False

    # Simulate process restart with new MessageStore instance on same SQLite file
    restarted_store = MessageStore(db_path=db_file)
    assert restarted_store.get_since_id() == 10
    assert restarted_store.is_processed(10) is True
    items, total = restarted_store.list_messages(category=EmailCategory.TARIFF_UPDATE)
    assert total == 1
    assert items[0].message_id == 10
    assert items[0].decision.category == EmailCategory.TARIFF_UPDATE
    assert items[0].trace_id == "tr-1"


@pytest.mark.anyio
async def test_poller_deduplicates_and_recovers_on_partial_batch_failure(tmp_path: Path) -> None:
    db_file = str(tmp_path / "poller.db")
    store = MessageStore(db_path=db_file)
    poll_calls: list[int] = []
    should_fail = {"flag": True}

    def handler(request: httpx.Request) -> httpx.Response:
        since_id = int(request.url.params["since_id"])
        poll_calls.append(since_id)
        all_mails = [
            {"id": 1, "from": "a@test.com", "subject": "Mail 1", "body": "First RFQ"},
            {"id": 2, "from": "b@test.com", "subject": "Mail 2", "body": "FAIL_HERE second"},
            {"id": 3, "from": "c@test.com", "subject": "Mail 3", "body": "Third RFQ"},
        ]
        # Intentionally include already-processed mail 1 on second call to verify DB deduplication
        if since_id == 0:
            batch = all_mails
        else:
            batch = [m for m in all_mails if m["id"] >= since_id]
        return httpx.Response(200, json={"messages": batch, "next_since_id": 3})

    settings = Settings(
        langfuse_enabled=False,
        decision_api_key="test-key",
        decision_model="gpt-6-luna",
        hackloop_base_url="https://api.start-hack.5u.ai",
        hackloop_api_key="team-key",
        sqlite_db_path=db_file,
    )

    class _ToggleOpenAI:
        def __init__(self) -> None:
            self.processed_texts: list[str] = []
            self.chat = SimpleNamespace(completions=self)

        async def parse(self, **kwargs):
            content = kwargs["messages"][1]["content"]
            if should_fail["flag"] and "FAIL_HERE" in content:
                raise RuntimeError("Transient error")
            self.processed_texts.append(content)
            decision = ClassificationDecision(
                category=EmailCategory.RFQ,
                confidence=0.9,
                reasoning="RFQ mail.",
            )
            return SimpleNamespace(
                choices=[SimpleNamespace(message=SimpleNamespace(parsed=decision))]
            )

    fake_openai = _ToggleOpenAI()
    classifier = DecisionClassifier(settings=settings, openai_client=fake_openai)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http_client:
        mb_client = MailboxClient(settings, http_client=http_client)
        poller = MailboxPoller(settings, mb_client, classifier, store)

        # First run: mail 1 succeeds, mail 2 fails -> cursor stops at 1, mail 3 not skipped
        first_summary = await poller.poll_once()
        assert first_summary.processed == 1
        assert first_summary.errors == 1
        assert first_summary.last_since_id == 1

        # Second run: mail 2 recovers; mail 1 (returned again) is skipped as duplicate
        should_fail["flag"] = False
        second_summary = await poller.poll_once()
        assert second_summary.processed == 2
        assert second_summary.skipped_duplicates == 1
        assert second_summary.errors == 0
        assert second_summary.last_since_id == 3

        # Third run: concurrent calls only classify nothing new
        r1, r2 = await asyncio.gather(poller.poll_once(), poller.poll_once())
        assert r1.processed == 0
        assert r2.processed == 0

    assert len(fake_openai.processed_texts) == 3
    assert store.count_messages() == 3


@pytest.mark.anyio
async def test_poller_does_not_log_on_empty_poll(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    db_file = str(tmp_path / "empty_poll.db")
    store = MessageStore(db_path=db_file)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"messages": [], "next_since_id": 0})

    settings = Settings(
        langfuse_enabled=False,
        decision_api_key="test-key",
        decision_model="gpt-6-luna",
        hackloop_base_url="https://api.start-hack.5u.ai",
        hackloop_api_key="team-key",
        sqlite_db_path=db_file,
    )
    classifier = DecisionClassifier(settings=settings, openai_client=_FakeOpenAIClient(_sample_decision()))

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http_client:
        mb_client = MailboxClient(settings, http_client=http_client)
        poller = MailboxPoller(settings, mb_client, classifier, store)
        caplog.clear()
        with caplog.at_level("INFO", logger="app"):
            summary = await poller.poll_once()

    assert summary.fetched == 0
    assert summary.processed == 0
    assert [r for r in caplog.records if r.name.startswith("app")] == []


def test_fastapi_lifespan_background_poller_and_message_endpoints(tmp_path: Path) -> None:
    db_file = str(tmp_path / "api_poller.db")
    fake_openai = _CountingOpenAIClient(category=EmailCategory.TRACKING_REQUEST)

    def handler(request: httpx.Request) -> httpx.Response:
        since_id = int(request.url.params["since_id"])
        mails = [
            {
                "id": 101,
                "from": "tracking@hackloop.mail",
                "subject": "AWB 123-45678901 Update",
                "body": "Shipment departed FRA.",
                "references": [99],
            }
        ]
        filtered = [m for m in mails if m["id"] > since_id]
        return httpx.Response(200, json={"messages": filtered, "next_since_id": 101})

    settings = Settings(
        langfuse_enabled=False,
        decision_api_key="test-key",
        decision_model="gpt-6-luna",
        hackloop_base_url="https://api.start-hack.5u.ai",
        hackloop_api_key="team-key",
        mailbox_poll_enabled=True,
        mailbox_poll_interval_seconds=0.05,
        sqlite_db_path=db_file,
    )
    http_client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    store = MessageStore(db_path=db_file)
    mb_client = MailboxClient(settings, http_client=http_client)
    classifier = DecisionClassifier(settings=settings, openai_client=fake_openai)

    app.dependency_overrides[get_settings] = lambda: settings
    app.dependency_overrides[get_store] = lambda: store
    app.dependency_overrides[get_mailbox_client] = lambda: mb_client
    app.dependency_overrides[get_classifier] = lambda: classifier

    try:
        with TestClient(app) as client:
            deadline = time.monotonic() + 2.0
            while time.monotonic() < deadline:
                resp = client.get("/messages")
                if resp.status_code == 200 and resp.json()["total"] == 1:
                    break
                time.sleep(0.05)

            list_resp = client.get("/messages")
            assert list_resp.status_code == 200
            list_data = list_resp.json()
            assert list_data["total"] == 1
            assert list_data["last_since_id"] == 101
            assert list_data["items"][0]["message_id"] == 101
            assert list_data["items"][0]["decision"]["category"] == "tracking_request"

            detail_resp = client.get("/messages/101")
            assert detail_resp.status_code == 200
            assert detail_resp.json()["sender"] == "tracking@hackloop.mail"

            missing_resp = client.get("/messages/9999")
            assert missing_resp.status_code == 404

            status_resp = client.get("/poller/status")
            assert status_resp.status_code == 200
            status_data = status_resp.json()
            assert status_data["enabled"] is True
            assert status_data["running"] is True
            assert status_data["processed_count"] == 1

            poll_resp = client.post("/poll")
            assert poll_resp.status_code == 200
            assert poll_resp.json()["processed"] == 0
            assert len(fake_openai.completions.calls) == 1
    finally:
        app.dependency_overrides.clear()
        asyncio.run(http_client.aclose())


@pytest.mark.anyio
async def test_poller_skips_a_mail_after_three_failed_attempts(tmp_path: Path) -> None:
    """ADR 20: a mail that keeps failing is stored as `other` and no longer blocks later mail."""
    store = MessageStore(db_path=str(tmp_path / "retry.db"))

    def handler(request: httpx.Request) -> httpx.Response:
        mails = [
            {"id": 1, "from": "a@test.com", "subject": "Mail 1", "body": "POISON"},
            {"id": 2, "from": "b@test.com", "subject": "Mail 2", "body": "fine"},
        ]
        since_id = int(request.url.params["since_id"])
        return httpx.Response(
            200, json={"messages": [m for m in mails if m["id"] > since_id], "next_since_id": 2}
        )

    settings = Settings(langfuse_enabled=False, hackloop_api_key="team-key")
    fake_openai = _CountingOpenAIClient(fail_on_text="POISON")
    classifier = DecisionClassifier(settings=settings, openai_client=fake_openai)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http_client:
        poller = MailboxPoller(settings, MailboxClient(settings, http_client), classifier, store)
        first, second, third = [await poller.poll_once() for _ in range(3)]

    assert (first.processed, second.processed, third.processed) == (0, 0, 1)
    assert third.last_since_id == 2
    failed = store.get_message(1)
    assert failed is not None and failed.decision.category == EmailCategory.OTHER
    assert failed.decision.reasoning.startswith("processing failed")
    assert store.get_message(2).decision.category == EmailCategory.RFQ
