import asyncio
import logging
from datetime import UTC, datetime
from typing import TYPE_CHECKING

import httpx
from langfuse import get_client, observe, propagate_attributes

from app.classifier import DecisionClassifier
from app.config import Settings
from app.mailbox_client import MailboxClient, mailbox_message_to_payload
from app.schemas import ClassificationDecision, EmailCategory, MailboxMessage, PollSummary
from app.storage import MessageStore

if TYPE_CHECKING:
    from app.workflow import Workflow

logger = logging.getLogger(__name__)

_MAX_PAGES_PER_TICK = 10
_LOW_CONFIDENCE = 0.7
_TRUCKER_REPLY = ClassificationDecision(
    category=EmailCategory.TRUCKER_QUOTE,
    confidence=1.0,
    reasoning="Reply from a trucker we asked; read together with the other replies.",
)
# Understood and stored, but nothing acts on them yet (docs/workflow.md section 13).
_UNHANDLED = {
    EmailCategory.OTHER,
    EmailCategory.RATE_OFFER,
}


class MailboxPoller:
    """Polls the mailbox and runs every unseen mail through classifier and workflow.

    Without a `workflow` it only classifies and stores (the pre-workflow behaviour).
    """

    def __init__(
        self,
        settings: Settings,
        mailbox_client: MailboxClient,
        classifier: DecisionClassifier,
        store: MessageStore,
        workflow: "Workflow | None" = None,
    ) -> None:
        self._settings = settings
        self._mailbox_client = mailbox_client
        self._classifier = classifier
        self._store = store
        self._workflow = workflow
        self._poll_lock = asyncio.Lock()
        self._stop_event = asyncio.Event()
        self.running: bool = False
        self.last_poll_at: str | None = None
        self.last_error: str | None = None
        self._backoff = 0.0

    @observe(name="mail")
    async def process_message(self, msg: MailboxMessage) -> ClassificationDecision:
        """One trace per mail: context lookup -> classify -> store -> workflow."""
        context = await self._workflow.context(msg) if self._workflow else None
        shipment = context.shipment if context else None
        # A new request starts its own shipment, so its trace opens that shipment's session.
        session_id = f"shipment-{shipment.id if shipment else msg.id}"
        role = context.sender_role if context else "unknown"

        with propagate_attributes(
            session_id=session_id,
            user_id=msg.sender,
            tags=[role],
            metadata={"message_id": str(msg.id)},
        ):
            if self._workflow and msg.attachments:  # customers and carriers (trucker offers come as files)
                await self._workflow.read_attachments(msg)  # stored with the mail below
            payload = mailbox_message_to_payload(msg)
            if self._workflow and context and self._workflow.awaited_trucker_reply(context, msg):
                # A mail from a trucker we asked is their answer: no classifier call (ADR 41).
                decision, trace_id = _TRUCKER_REPLY, get_client().get_current_trace_id()
            else:
                decision, trace_id = await self._classifier.classify(payload, context)
            tags = [decision.category.value]
            if shipment:
                tags.append(shipment.state.value)
            if decision.confidence < _LOW_CONFIDENCE or decision.category in _UNHANDLED:
                tags.append("needs_review")
            with propagate_attributes(tags=tags):
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
                if self._workflow and context:
                    await self._workflow.handle(msg, decision, context)
        return decision

    def _give_up(self, msg: MailboxMessage, error: str) -> None:
        """Store a mail that failed too often as `other`, so the cursor can move on (ADR 20)."""
        logger.error("Giving up on mailbox message id=%s: %s", msg.id, error)
        self._store.save_processed_message(
            message=msg,
            decision=ClassificationDecision(
                category=EmailCategory.OTHER,
                confidence=0.0,
                reasoning=f"processing failed: {error}"[:500],
            ),
            model=self._settings.decision_model,
        )

    async def poll_once(self) -> PollSummary:
        """Run one guarded poll cycle (fetch -> deduplicate -> process -> persist)."""
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
                        await self.process_message(msg)
                        processed += 1
                        since_id = max(since_id, msg.id)
                    except Exception as exc:  # noqa: BLE001 - one bad mail must not stop the rest
                        errors += 1
                        self.last_error = f"message_id={msg.id}: {exc}"
                        logger.exception("Failed processing mailbox message id=%s", msg.id)
                        attempts = self._store.record_failure(msg.id, str(exc))
                        if attempts >= self._settings.max_message_attempts:
                            self._give_up(msg, str(exc))
                            since_id = max(since_id, msg.id)
                            continue
                        # Keep the cursor before the failed mail; it is retried on the next tick.
                        batch_failed = True
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

    async def _cycle(self) -> None:
        """One poll, then the workflow's timers (trucker deadlines, booking reminders).

        The timers run even when the poll fails, so quotes still go out. A 429 makes the
        next poll wait for its Retry-After instead of hammering the limit.
        """
        self._backoff = 0.0
        try:
            await self.poll_once()
        except httpx.HTTPStatusError as exc:
            self._note_error(exc)
            if exc.response.status_code == 429:
                self._backoff = float(exc.response.headers.get("Retry-After", "5")) + 1
        except Exception as exc:  # noqa: BLE001 - keep background poller alive
            self._note_error(exc)
        try:
            if self._workflow:
                await self._workflow.tick()
        except Exception as exc:  # noqa: BLE001 - keep background poller alive
            self._note_error(exc)

    def _note_error(self, exc: Exception) -> None:
        self.last_poll_at = datetime.now(UTC).isoformat()
        self.last_error = str(exc)
        logger.warning("Mailbox poll cycle failed: %s", str(exc).splitlines()[0])

    async def run_forever(self) -> None:
        """Continuously poll every mailbox_poll_interval_seconds until stopped."""
        self.running = True
        self._stop_event.clear()
        interval = max(0.01, self._settings.mailbox_poll_interval_seconds)
        logger.info("Starting mailbox background poller (interval=%.1fs)", interval)
        try:
            while not self._stop_event.is_set():
                await self._cycle()
                try:
                    wait = max(interval, self._backoff)
                    await asyncio.wait_for(self._stop_event.wait(), timeout=wait)
                except TimeoutError:
                    continue
        finally:
            self.running = False
            logger.info("Mailbox background poller stopped")

    def stop(self) -> None:
        """Signal the background polling loop to exit cleanly."""
        self._stop_event.set()
