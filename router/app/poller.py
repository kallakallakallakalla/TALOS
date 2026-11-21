import asyncio
import logging
from datetime import UTC, datetime

from langfuse import observe

from app.classifier import DecisionClassifier
from app.config import Settings
from app.mailbox_client import MailboxClient, mailbox_message_to_payload
from app.schemas import MailboxMessage, PollSummary
from app.storage import MessageStore

logger = logging.getLogger(__name__)

_MAX_PAGES_PER_TICK = 10


class MailboxPoller:
    """Periodically polls the Hackloop mailbox and classifies unseen messages."""

    def __init__(
        self,
        settings: Settings,
        mailbox_client: MailboxClient,
        classifier: DecisionClassifier,
        store: MessageStore,
    ) -> None:
        self._settings = settings
        self._mailbox_client = mailbox_client
        self._classifier = classifier
        self._store = store
        self._poll_lock = asyncio.Lock()
        self._stop_event = asyncio.Event()
        self.running: bool = False
        self.last_poll_at: str | None = None
        self.last_error: str | None = None

    @observe(name="mailbox-poll-message")
    async def _process_message(self, msg: MailboxMessage) -> None:
        payload = mailbox_message_to_payload(msg)
        decision, trace_id = await self._classifier.classify(payload)
        inserted = self._store.save_processed_message(
            message=msg,
            decision=decision,
            model=self._settings.decision_model,
            trace_id=trace_id,
            advance_cursor=True,
        )
        if inserted:
            logger.info(
                "Classified mailbox message id=%s category=%s confidence=%.2f trace_id=%s",
                msg.id,
                decision.category.value,
                decision.confidence,
                trace_id,
            )

    async def poll_once(self) -> PollSummary:
        """Run one guarded poll cycle (fetch -> deduplicate -> classify -> persist)."""
        async with self._poll_lock:
            fetched = 0
            processed = 0
            skipped_duplicates = 0
            errors = 0
            since_id = self._store.get_since_id()
            limit = max(1, min(200, self._settings.mailbox_poll_limit))

            for _ in range(_MAX_PAGES_PER_TICK):
                poll_resp = await self._mailbox_client.poll_messages(
                    since_id=since_id,
                    limit=limit,
                )
                messages = sorted(poll_resp.messages, key=lambda m: m.id)
                fetched += len(messages)

                if not messages:
                    if poll_resp.next_since_id > since_id:
                        since_id = self._store.set_since_id(poll_resp.next_since_id)
                    break

                batch_failed = False
                for msg in messages:
                    if self._store.is_processed(msg.id):
                        skipped_duplicates += 1
                        since_id = self._store.set_since_id(msg.id)
                        continue
                    try:
                        await self._process_message(msg)
                        processed += 1
                        since_id = max(since_id, msg.id)
                    except Exception as exc:  # noqa: BLE001 - preserve cursor before failed item
                        errors += 1
                        batch_failed = True
                        self.last_error = f"message_id={msg.id}: {exc}"
                        logger.exception("Failed processing mailbox message id=%s", msg.id)
                        break

                if batch_failed:
                    break

                if poll_resp.next_since_id > since_id:
                    since_id = self._store.set_since_id(poll_resp.next_since_id)

                if len(messages) < limit:
                    break

            self.last_poll_at = datetime.now(UTC).isoformat()
            if errors == 0:
                self.last_error = None

            return PollSummary(
                fetched=fetched,
                processed=processed,
                skipped_duplicates=skipped_duplicates,
                errors=errors,
                last_since_id=self._store.get_since_id(),
            )

    async def run_forever(self) -> None:
        """Continuously poll every mailbox_poll_interval_seconds until stopped."""
        self.running = True
        self._stop_event.clear()
        interval = max(0.01, self._settings.mailbox_poll_interval_seconds)
        logger.info("Starting mailbox background poller (interval=%.1fs)", interval)
        try:
            while not self._stop_event.is_set():
                try:
                    await self.poll_once()
                except Exception as exc:  # noqa: BLE001 - keep background poller alive
                    self.last_poll_at = datetime.now(UTC).isoformat()
                    self.last_error = str(exc)
                    logger.exception("Mailbox poll cycle failed")

                try:
                    await asyncio.wait_for(self._stop_event.wait(), timeout=interval)
                except TimeoutError:
                    continue
        finally:
            self.running = False
            logger.info("Mailbox background poller stopped")

    def stop(self) -> None:
        """Signal the background polling loop to exit cleanly."""
        self._stop_event.set()
