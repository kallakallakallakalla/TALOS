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
    reference.py          postal zone -> airport + truckers
    portal_client.py      portal rates (10 reads/min) and bookings
    tariffs.py            local tariffs of the 10 non-portal airlines
    pricing.py            chargeable weight, anti-weight-break, pick_best, all-in
    composer.py           LLM #3: writes the mail; code checks the facts; templates.py fallback
  workflow.tick()         timers: trucker wait 45 s / 90 s, booking reminders
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

## Endpoints

- `GET /shipments[?state=QUOTED]`, `GET /shipments/{id}`: shipments with their trucker
  requests, air options and composed mails (in a dry run: what would have been sent)
- `GET /messages[?category=trucker_quote]`, `GET /messages/{id}`: stored, classified mails
- `GET /poller/status`, `POST /poll`: poller state, one manual poll cycle
- `POST /classify`, `POST /run`: classify a text directly (no context, no workflow)
- `GET /health`, `GET /docs`

## Scripts (run from `router/`)

| Script | What it does |
|---|---|
| `scripts/replay.py` | Replays the recorded mailbox through the whole pipeline as a dry run (real LLM, cached portal reads, virtual clock). Result: `data/replay/result.json` |
| `scripts/export_mails.py` | Refreshes `tests/fixtures/mails.json` from the live mailbox (read-only) |
| `scripts/build_datasets.py` | Builds the Langfuse datasets from the recorded mails + `tests/fixtures/labels.json` |
| `scripts/eval_prompts.py --prompt <name\|all> --label staging` | Runs a prompt version against its dataset, prints the accuracy |
| `scripts/push_prompts.py [--label staging] [--promote <name>]` | Pushes `prompts/*.md` to Langfuse / promotes staging to production |
| `scripts/compose_samples.py` | Writes one sample of each mail kind for a human read |
| `scripts/build_tariffs.py` | Rebuilds `../data/tariffs.json` from the start-kit tariff files |

Changing a prompt: edit `prompts/<name>.md` → `push_prompts.py` (staging) →
`eval_prompts.py --label staging` → `push_prompts.py --promote <name>`.

## Tests

```bash
.venv/bin/pytest
```

No test calls a real API or LLM: the platform is an `httpx.MockTransport` and LLM answers are
scripted (`tests/fakes.py`). `tests/test_workflow.py` replays round 4 from the recorded mails.
