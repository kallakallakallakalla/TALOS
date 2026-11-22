"""ultra-talos: poll the mailbox, classify each mail with the existing router classifier,
then hand it to an autonomous opencode runner (gpt-6-luna) that solves it through the API.

  ../router/.venv/bin/python main.py              poll every 5 s, starting after the newest mail
  ../router/.venv/bin/python main.py --once 275   run one existing mail through classify + runner

Everything a runner knows lives on the file system: mails/, shipments/, data/, knowledge/.
"""

import asyncio
import csv
import json
import logging
import os
import sys
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parent
ROUTER = ROOT.parent / "router"
sys.path.insert(0, str(ROUTER))
sys.path.insert(0, str(ROOT / "tools"))

import board  # noqa: E402
from app.classifier import DecisionClassifier  # noqa: E402
from app.config import Settings, as_test_settings  # noqa: E402
from app.mailbox_client import MailboxClient, mailbox_message_to_payload  # noqa: E402
from app.schemas import MailboxMessage, MailContext  # noqa: E402

POLL_SECONDS = 5.2  # platform limit: one poll per 5 s
RUNNER_TIMEOUT_SECONDS = 330  # a round closes 6 minutes after it is sent
MAX_RUNS_PER_SHIPMENT = 10  # loop guard: a thread that keeps triggering runners is cut off

# The classifier is only the doorman: these categories start no session. Carrier replies and
# booking confirmations are answers to a session that is already working (it reads them from
# mails/), and "other" is thanks, acknowledgements and noise. Everything else goes to the one
# desk agent (.opencode/agent/desk.md), which decides for itself what the mail needs.
LIVE_FROM = 28150  # go-live 2026-11-22 00:40 UTC: the newest mail id at that moment
NO_SESSION = {"trucker_quote", "trucker_decline", "rate_offer", "booking_confirmation", "other"}
AGENT = "desk"

log = logging.getLogger("ultra-talos")
# LLM backend for the classifier AND the opencode sessions, from router/.env:
#   gateway (default): DECISION_API_KEY at DECISION_API_BASE_URL, our OpenAI proxy. Scored as production cost.
#   test (ULTRA_LLM=test): EVALUATION_KEY directly at OpenAI. Replays and experiments use this.
settings = Settings(_env_file=ROUTER / ".env")
if os.environ.get("ULTRA_LLM", "gateway") == "test":
    settings = as_test_settings(settings)
settings = settings.model_copy(update={"prompts_dir": str(ROUTER / "prompts")})
LLM_ENV = {"TALOS_LLM_KEY": settings.decision_api_key, "TALOS_LLM_BASE_URL": settings.decision_api_base_url}
mailbox = MailboxClient(settings)
classifier = DecisionClassifier(settings)
locks: dict[str, asyncio.Lock] = defaultdict(asyncio.Lock)  # one runner per shipment at a time


def sender_role(email: str | None) -> str:
    email = (email or "").lower()
    with open(ROOT / "data" / "carriers.csv", newline="", encoding="utf-8") as f:
        kinds = {row["email"].lower(): row["kind"] for row in csv.DictReader(f)}
    return kinds.get(email) or {"ratedesk": "ratedesk", "tracking": "tracking"}.get(email.split("@")[0], "customer")


async def classify(msg: MailboxMessage) -> dict:
    our_mail = ""
    if msg.in_reply_to:
        try:
            parent = await mailbox.get_message(msg.in_reply_to)
            our_mail = f"Subject: {parent.subject}\n{parent.body}"
        except Exception as exc:  # noqa: BLE001 - the classifier also works without context
            log.warning("mail %s: parent %s not readable: %s", msg.id, msg.in_reply_to, exc)
    context = MailContext(sender_role=sender_role(msg.sender), our_mail=our_mail)
    decision, _ = await classifier.classify(mailbox_message_to_payload(msg), context)
    return {"category": decision.category.value, "confidence": decision.confidence,
            "reasoning": decision.reasoning, "sender_role": context.sender_role}


async def run_opencode(agent: str, msg: MailboxMessage, record: dict) -> None:
    shipment = record["shipment"]
    folder = ROOT / "shipments" / shipment
    folder.mkdir(parents=True, exist_ok=True)
    async with locks[shipment]:
        runs = folder / "runs.log"
        if runs.exists() and len(runs.read_text().splitlines()) >= MAX_RUNS_PER_SHIPMENT:
            log.warning("mail %s: shipment %s reached %s runs, not starting another", msg.id, shipment, MAX_RUNS_PER_SHIPMENT)
            return
        now = datetime.now(timezone.utc).isoformat(timespec="seconds")
        with runs.open("a") as f:
            f.write(f"{now} mail {msg.id} {record['classification']['category']} -> {agent}\n")
        prompt = (
            f"New mail {msg.id}. Classifier hint: {record['classification']['category']} "
            f"(sender role: {record['classification']['sender_role']}).\n"
            f"Now: {now} (UTC). Shipment folder: shipments/{shipment}/ . Mail file: mails/{msg.id}.json\n\n"
            f"{json.dumps(msg.raw, indent=1, ensure_ascii=False)}"
        )
        out = ROOT / "logs" / f"{msg.id}-{agent}.jsonl"
        out.parent.mkdir(exist_ok=True)
        log.info("mail %s: starting %s runner (log: %s)", msg.id, agent, out.relative_to(ROOT))
        board.log("session_start", f"{agent} session for mail {msg.id}", shipment, msg.id)
        with out.open("wb") as f:
            # --standalone: a private server, so the {env:TALOS_LLM_*} values in opencode.json resolve here.
            proc = await asyncio.create_subprocess_exec(
                "opencode", "run", "--standalone", "--auto", "--agent", agent, "--format", "json",
                "--title", f"{agent} mail {msg.id}", prompt,
                cwd=ROOT, stdout=f, stderr=f, env=os.environ | LLM_ENV | {"TALOS_SHIPMENT": shipment},
            )
            try:
                code = await asyncio.wait_for(proc.wait(), RUNNER_TIMEOUT_SECONDS)
            except asyncio.TimeoutError:
                proc.kill()
                code = "timeout"
        log.info("mail %s: %s runner finished (%s)", msg.id, agent, code)
        summary = ""
        for line in out.read_text(errors="replace").splitlines():  # the session's last text is its summary
            try:
                event = json.loads(line)
            except ValueError:
                continue
            if event.get("type") == "text":
                summary = event["part"]["text"]
        board.log("session_end", f"exit {code}: {summary}"[:1500], shipment, msg.id)


async def handle(msg: MailboxMessage) -> None:
    """Store, classify and dispatch one mail. One mail must never stop the poller."""
    try:
        record = {**msg.raw, "shipment": str(msg.references[0] if msg.references else msg.id)}
        record["classification"] = await classify(msg)
        (ROOT / "mails").mkdir(exist_ok=True)
        (ROOT / "mails" / f"{msg.id}.json").write_text(json.dumps(record, indent=1, ensure_ascii=False))
        category = record["classification"]["category"]
        board.log("mail_in", f"{category} from {msg.sender}: {msg.subject}", record["shipment"], msg.id, record)
        log.info("mail %s from %s: %s (%.2f) %s", msg.id, msg.sender, category,
                 record["classification"]["confidence"], msg.subject)
        # Threads that began before we went live belong to the system that answered them:
        # acting on them would mean a second quote or a second booking.
        if record["shipment"].isdigit() and int(record["shipment"]) <= LIVE_FROM:
            log.info("mail %s: thread %s began before go-live (%s), not ours", msg.id, record["shipment"], LIVE_FROM)
            return
        if category not in NO_SESSION:
            await run_opencode(AGENT, msg, record)
    except Exception:  # noqa: BLE001
        log.exception("mail %s failed", msg.id)


async def poll(since: int, limit: int):
    """List new mail. The platform allows one poll per 5 s per team key, shared with whoever
    else polls this mailbox: on a 429, try again on the next beat."""
    while True:
        try:
            return await mailbox.poll_messages(since, limit)
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code != 429:
                raise
            await asyncio.sleep(POLL_SECONDS)  # never faster than the poll beat


async def poll_forever() -> None:
    cursor_file = ROOT / "state" / "since_id"
    cursor_file.parent.mkdir(exist_ok=True)
    if cursor_file.exists():
        since = int(cursor_file.read_text())
    else:  # first start: skip the backlog, begin with the next new mail
        since = 0
        while (page := await poll(since, 200)).messages:
            since = page.next_since_id
    log.info("polling every %s s from mail id %s", POLL_SECONDS, since)
    tasks: set[asyncio.Task] = set()
    while True:
        try:
            page = await poll(since, 100)
            for msg in page.messages:
                task = asyncio.create_task(handle(msg))  # runners work in parallel
                tasks.add(task)
                task.add_done_callback(tasks.discard)
            since = max(since, page.next_since_id)
            cursor_file.write_text(str(since))
        except Exception as exc:  # noqa: BLE001
            log.warning("poll failed: %s", exc)
        await asyncio.sleep(POLL_SECONDS)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)
    if len(sys.argv) == 3 and sys.argv[1] == "--once":
        async def once() -> None:
            await handle(await mailbox.get_message(int(sys.argv[2])))
        asyncio.run(once())
    else:
        asyncio.run(poll_forever())
