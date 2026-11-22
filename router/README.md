# TALOS router

Answers freight-quote mails at `talos@hackloop.mail`: it classifies every incoming mail, prices
trucker pickup + air freight, writes one all-in EUR quote and books when the customer accepts.
FastAPI + a mailbox poller + SQLite + `gpt-6-luna` through the Hackloop LLM gateway + Langfuse.

The plan and every decision are in [`../docs`](../docs/README.md); what was built and how it
performed is in [`../docs/implementation.md`](../docs/implementation.md).

> **`SEND_ENABLED=false` is the default and means dry run:** mails are composed and stored in
> `outbound_mails`, but nothing is sent and nothing is booked. Switch it on only at cutover
> ([`../docs/workflow.md`](../docs/workflow.md) section 12).

## How a mail flows

```text
poller.py        every 2 s: fetch new mail, one Langfuse trace per mail
  workflow.context()      who sent it, which shipment (carrier list + thread ids, no LLM)
  classifier.py           LLM #1: mail type (12 categories)
  workflow.handle()       state machine; calls, depending on the type:
    extractors.py         LLM #2: fields of that one mail type
                          (trucker replies: no classifier call; all replies of a request
                          are read together in ONE call just before quoting)
    reference.py          postal zone -> airport + truckers
    portal_client.py      portal rates (10 reads/min) and bookings
    tariffs.py            local tariffs of the 10 non-portal airlines
    pricing.py            chargeable weight, anti-weight-break, pick_best, all-in
    composer.py           LLM #3: writes mails to customers and airlines; code checks the facts
    templates.py          trucker requests (always) and the fallback texts
                          (also answers tracking questions from the shipment record)
  workflow.tick()         timers: trucker wait (max 60 s), booking reminders
storage.py       SQLite: mails, shipments, trucker requests, air options, outbound mails
```

| Module | Job |
|---|---|
| `app/main.py` | FastAPI app, wiring in `lifespan`, endpoints |
| `app/poller.py` | poll loop, retry cap (a failing mail is skipped after 3 attempts) |
| `app/workflow.py` | shipment state machine: the only place that decides what happens |
| `app/classifier.py`, `app/extractors.py`, `app/composer.py` | the three LLM steps |
| `app/llm.py`, `app/prompts.py` | one structured-output helper; prompts from Langfuse with a local fallback |
| `app/pricing.py`, `app/tariffs.py`, `app/reference.py` | deterministic rules and data, no I/O beyond reading files |
| `app/portal_client.py`, `app/mailbox_client.py` | platform API clients |
| `app/schemas.py`, `app/storage.py`, `app/config.py` | models, SQLite store, settings |
| `prompts/*.md` | local copy of each prompt (fallback and source for `push_prompts.py`) |

## Setup and run

```bash
cd router
python3 -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
cp .env.example .env          # fill in HACKLOOP_API_KEY, DECISION_API_KEY, Langfuse keys
uvicorn app.main:app --port 8000
```

Every setting is listed with its default in `.env.example` and `app/config.py`.

## Modes

| `router/.env` | Sends and books | LLM calls | Database |
|---|---|---|---|
| `TEST_MODE=true` | no | `EVALUATION_KEY`, directly at OpenAI | `data/test.db` |
| neither set (default) | no (dry run) | team key via the scored gateway | `data/router.db` |
| `SEND_ENABLED=true` | **yes** | team key via the scored gateway | `data/router.db` |

Before the first start in a mode, and at cutover, run `scripts/backfill_legacy.py`: it marks all
mail already in the mailbox as handled, so the router starts with the next new mail.

## CRM

`http://localhost:8000/crm` (the root URL redirects there) is the supervisor console. The
router works on its own; the console is for the three things a person still does:

| Page | Question it answers | What you do there |
|---|---|---|
| Now | Does anything need a person? | One line on whether the router is reading the mailbox, then the queue of cases it could not finish: retry a booking, send a reminder, close a shipment, process a failed mail again, or dismiss. Only cases a person can act on are counted; in live data a question or booking is listed once it has waited 2 minutes. A stopped poller is the first case. The route globe sits beside it. |
| Sessions | What is the router doing right now? | Every request as a session: the list on the left, the chosen one on the right as the sequence of what happened (each mail in and out, and between them what the router concluded: what it understood, which airlines it compared, how it priced the pickup). On live data the server pushes each new event the moment it is stored; "Replay" plays a session again, event by event. |
| Performance | Which carriers were used, and what do customers ask for? | Which airlines were quoted and accepted most, which truckers were asked, answered and used, and what the requests contained (pickup, priority, deadlines, dangerous goods, named carriers, insurance). |
| Rounds | How did each request end, and why was it lost? | One row per request: airline, quote, time to answer, outcome, the platform board's verdict and points where it can be read, and the reason of a loss in the customer's own words. Filter by outcome; a click on a route of the globe filters by lane. The "Quote mails" tab shows the quotes as the jury reads them and marks the rules of the compose prompt each one breaks. |
| Lookup | Where is that account, carrier, tariff, mail? | Tabs for accounts, airlines, truckers, tariffs and the mail stream; the search box filters the open tab. |
| System (setup) | How is this process set up? | Mode and poller, classifier accuracy (dry run); configuration without credentials, data inventory and prompts fold out. |

A shipment's page opens with "What happened": the steps that led to its state, each with the
reason (which airline was chosen and why, what was estimated, what a later offer showed). The
actions of its open case are on the page; request, offer and correspondence fold out.

- **Actions** go through the workflow's own operations and only work in the process that
  runs the mailbox poller, under the lock the poll cycle holds, so they never run between two
  steps of a shipment. A second process that only reads the database (see below) can dismiss
  items and nothing else. `SEND_ENABLED` still decides whether mail really leaves.
- **No login.** An action is therefore only accepted from the CRM page itself: the request
  must be JSON, come from the same origin, and be addressed to `localhost` or to a host name
  listed in `CRM_ACTION_HOSTS`. Another website open in the same browser cannot trigger one.
  Anyone who can open the page can act, so keep the port on localhost or behind your own access control.
- **Streaming:** the Sessions page holds a server-sent event stream open (`GET /crm/api/stream`).
  The server looks at the change token once a second and sends the events that are new, so a
  step shows up about a second after the router stored it. It works in any process that can
  read the database, so a second process follows a running router the same way.
- **Notifications:** the bell asks the browser for permission; after that a new case in the
  queue is announced, so nobody has to watch the page. The tab title carries the count.
- **Status bar:** under the header on every page, like a status page: the overall state
  (operational, degraded, down, or read-only view) with the uptime, then one strip of ticks
  per part: the mailbox (one tick per minute of polling, red where a poll failed), the latest
  requests, bookings and mails (one tick each, coloured by how it went; a request's tick opens
  it). It rides on the 3-second version poll.
- **Live** reads the router's database and follows it: the page asks for a change token every
  3 s and reloads the figures when it moves. **Dry run** reads the last replay; on a fresh
  checkout that database is rebuilt from `data/replay/recorded-run.json` and the recorded mails.
- A dry run shows no timings and no win rates: its clocks are mixed, and the recorded customer
  verdicts answered the old process's quotes. The page says so where it applies.
- The globe places a shipment in transit by its planned departure and arrival dates. The
  system has no aircraft positions.
- No build step, and the page itself loads nothing from another host: plain HTML, CSS and
  JavaScript in `app/crm_ui/`. Every table can be downloaded as CSV. `/` jumps to the search box.

To look at it without touching the mailbox (no polling, so no LLM calls), `./crm.sh` (or
`crm.bat` on Windows) installs on the first run, starts the app on port 8010 and opens the
page. To follow a running router, give it that router's database file:
`./crm.sh 8010 /path/to/router/data/router.db`. SQLite lets this second process read while the
router writes, so the router itself is not touched. By hand:

```bash
MAILBOX_POLL_ENABLED=false .venv/bin/uvicorn app.main:app --port 8000
```

## Endpoints

- `GET /shipments[?state=QUOTED]`, `GET /shipments/{id}`: shipments with their trucker
  requests, air options and composed mails (in a dry run: what would have been sent)
- `GET /messages[?category=trucker_quote]`, `GET /messages/{id}`: stored, classified mails
- `GET /data-updates`: every update mail with each change it made or tried (ADR 50); the
  UI shows it in the Tariff updates tab (`/ui#updates`)
- `GET /reference/{carriers|zones|tariffs}`: the reference data as the router uses it now
- `GET /poller/status`, `POST /poll`: poller state, one manual poll cycle
- `POST /classify`, `POST /run`: classify a text directly (no context, no workflow)
- `GET /health`, `GET /docs`

## Scripts (run from `router/`)

| Script | What it does |
|---|---|
| `scripts/apply_pending_updates.py [--dry-run]` | Applies `tariff_update` mails that came in before the update handler existed (ADR 50). Idempotent |
| `scripts/replay.py` | Replays the recorded mailbox through the whole pipeline as a dry run (real LLM, cached portal reads, virtual clock). Result: `data/replay/result.json` |
| `scripts/export_mails.py` | Refreshes `tests/fixtures/mails.json` from the live mailbox (read-only) |
| `scripts/build_datasets.py` | Builds the Langfuse datasets from the recorded mails + `tests/fixtures/labels.json` |
| `scripts/eval_prompts.py --prompt <name\|all> --label staging` | Runs a prompt version against its dataset, prints the accuracy |
| `scripts/push_prompts.py [--label staging] [--promote <name>]` | Pushes `prompts/*.md` to Langfuse / promotes staging to production |
| `scripts/compose_samples.py` | Writes one sample of each mail kind for a human read |
| `scripts/build_tariffs.py` | Rebuilds `../data/tariffs.json` from the start-kit tariff files |

`eval_prompts.py`, `replay.py`, `compose_samples.py` and `build_tariffs.py` call OpenAI directly with
`EVALUATION_KEY` and refuse to run without it. They never use the challenge gateway, because gateway
traffic is scored as production cost. Never register that key at the gateway.

Changing a prompt: edit `prompts/<name>.md` → `push_prompts.py` (staging) →
`eval_prompts.py --label staging` → `push_prompts.py --promote <name>`.

## Tests

```bash
.venv/bin/pytest
```

No test calls a real API or LLM: the platform is an `httpx.MockTransport` and LLM answers are
scripted (`tests/fakes.py`). `tests/test_workflow.py` replays round 4 from the recorded mails.
