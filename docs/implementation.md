# Implementation report: quote workflow (steps 0–8)

Written 2026-11-21 at the end of the implementation session. Branch: **`quote-workflow`** (not merged), one commit per build step of [workflow.md](workflow.md) section 11.

**State in one line:** steps 0–8 are built and tested; the router can read every mail type, price, quote and book. It has never sent a mail or booked anything: `SEND_ENABLED=false`. Steps 9 (live dry run) and 10 (cutover) are open.

## What was built

| Step | Commit | What | "Done when" from the plan | Result |
|---|---|---|---|---|
| – | `aa2605d` | `docs/` and refreshed `openapi.json` committed | – | done |
| 0 | `b6a1a7d` | settings, 2 s poll, mail-retry cap, `prompts.py`, `llm.py`, trace attributes | existing tests green; failing mail skipped after 3 attempts; trace shows session and tags | **met** (session, user, prompt version and cost read back from Langfuse; tags checked on the span, see "Limits") |
| 1 | `95fd540` | `reference.py` | 51447 → CGN and exactly 4 truckers; 61018 → FRA | **met** |
| 2 | `fac0f2f` | `scripts/build_tariffs.py`, `data/tariffs.json`, `tariffs.py` | 10 airlines; spot checks; expired lane filtered | **met**: 158 lanes, 10 airlines |
| 3 | `05a5240` | `pricing.py` | minimum, surcharges, 966.93 kg billed at 1000, tie-breaks, fallback | **met** |
| 4 | `b0d8f23` | `portal_client.py` | limiter holds the 11th call; 429 retry; 410 → `RateExpired` | **met** |
| 5 | `b7a0bb6` | tables + store methods, `mailbox_client.send()` / `get_message()` | a shipment round-trips through all states | **met** |
| 6 | `9200ae3` | new categories, `extractors.py`, fixtures, labels, datasets, `eval_prompts.py` | classifier 100%; 7/7 RFQs; all trucker quotes | **met**: 151/151, 16/16, 31/31 (and bookings 12/12) |
| 7 | `39d7535` | `composer.py`, `templates.py` | fact check passes on all 6 kinds; human read | **met** (samples below) |
| 8 | `4a02e45` | `workflow.py`, wiring, `/shipments` | round 4 replay → quote with 1599.41, booking with reference "947" | **met**, in the unit test and in the live-LLM replay |
| 9 | – | live dry run, 2 rounds | – | **not done** (ADR 30) |
| 10 | – | cutover | – | **not done**, needs the user |

The module map, setup and every script are described in [`router/README.md`](../router/README.md).

## How it was tested

1. **Unit tests** (`cd router && .venv/bin/pytest`): 59 tests, no network. The platform is an `httpx.MockTransport`, LLM answers are scripted from the hand labels. `tests/test_workflow.py` covers: the round 4 replay, the dry run (nothing sent, nothing booked), the 45 s / 90 s trucker wait, 3 clarifications then FAILED, unknown zone, expired rate refetch, booking by email with reminders, 3 answers per thread, rejection, and "a carrier mail never opens a shipment".
2. **Prompt evals** (`scripts/eval_prompts.py`): every prompt against its Langfuse dataset, built from all 151 recorded mails and the hand labels. Real `gpt-6-luna` calls.
3. **Full replay** (`scripts/replay.py`): all 151 recorded mails through the whole pipeline in time order, with real LLM calls, real (cached, throttled) portal rates and a virtual clock. Nothing is sent: `SEND_ENABLED` is forced to false and the mailbox stub cannot send.

## Results

### Prompts on all recorded mails (rounds 1–16, 151 mails)

| Prompt | Dataset | Correct |
|---|---|---|
| `router-classification` | every mail → category | **151 / 151** |
| `talos-extract-rfq` | 16 quote requests, 9 fields each | **16 / 16** |
| `talos-extract-trucker-quote` | 31 trucker quotes, 4 fields each | **31 / 31** |
| `talos-extract-booking` | 12 booking confirmations, 5 fields each | **12 / 12** |

The RFQ set includes the three multi-lot requests (one with per-piece weights and dimensions: 1952 kg, 4.68 cbm), the two "our buyer is importing" requests and the two with a forwarded older mail underneath.

### Full replay (all recorded mails, live LLM, live portal rates, nothing sent)

**Round 4, verified end to end:** request 947 → 4 trucker requests composed → 8 portal rate reads → Xenon 1483.43 + Elbkante 115.98 = **1599.41 EUR**, the same price the customer accepted from the old process → dry-run booking. All 7 mails of the round were classified as labelled.

**All 16 rounds: the run was still in progress when this file was committed** (it is throttled to 3 portal reads per minute). Its output is `router/data/replay/result.json` and `replay.log` (git-ignored). The comparison of our prices with the old process's quotes per round is not written up yet.

## Where the implementation differs from the plan

Each of these is recorded as a decision (ADR 31–36 in [decisions.md](decisions.md)).

| Plan | Built | Why |
|---|---|---|
| Data model: 4 new tables | a 5th table `outbound_mails` | In a dry run nothing is sent, so composed mails need a place to be inspected (ADR 33) |
| `tariffs_path = data/tariffs.json` (inside `router/`) | `../data/tariffs.json` (repo root) | `router/data/` is git-ignored and holds the SQLite DB; the tariff DB must be committed |
| Portal: real weight + next break for every airline | next break only if it is at most 50% above the chargeable weight | saves rate reads; a break further away can't win (ADR 34) |
| Price format "EUR 1,599.41" | `1599.41 EUR` | the accepted quotes used it; no ambiguous separator (ADR 32) |
| `get_prompt(name, **vars)` in `prompts.py`, LLM call code in each module | `Prompts.get()` plus one shared `StructuredLLM.parse()` in `llm.py` | classifier, extractors and composer would otherwise repeat the same 30 lines |
| `workflow.context()` sync | async | it reads our previous mail through the mailbox API when it isn't in our own table |
| – | `*_REASONING_EFFORT` settings | `gpt-6-luna` spends billed reasoning tokens by default (ADR 31) |
| – | sender-based trucker matching in dry runs | replies can't thread to mails that were never sent (ADR 36) |
| Booking in a dry run: not specified | no API call; the shipment is marked BOOKED with `booking_ref = "DRY-RUN"` | so the flow after booking can be tested without booking |
| Prompts as drafted in prompts.md | small additions | found by the eval loop (ADR 35); `router/prompts/*.md` is now the source of truth |

## Cutover preparation: old mail is marked as handled

`router/scripts/backfill_legacy.py` was run on `router/data/router.db` (2026-11-21, ~18:05 CET): all 199 mails then in the mailbox are stored as processed, the poll cursor is at the newest one, and each of the 21 customer threads has a closed shipment (`FAILED`, note "legacy: handled by the old process before cutover"). A late reply in an old thread therefore triggers nothing. The script is idempotent and must be run once more right at cutover, so mails that arrived in between are covered too.

Known gap: a request the old process quoted, but whose acceptance arrives only after the switch, is not booked by the new router. Switch right after a round closes.

## Limits and things to know

- **Not built** (out of scope in the plan, or backlog in HANDOFF.md): tracking answers, applying tariff-update mails, rate offers from email-only airlines, the webhook, attachment reading and the dangerous-goods check. These mails are classified, stored and tagged `needs_review`; no mail goes out.
- **Dangerous goods:** rounds 10 and 11 were rejected with `dg_docs_missing`. This router would also quote them without asking for documents.
- **Requests without a pickup address** (rounds 15 and 16: "pickup from our warehouse", no postal code): the router asks the customer for the postal code or an airport. Whether the simulated customer answers such a question has never been observed.
- **Trace tags** can't be read back through the public API of the local Langfuse (v4 "events only" mode has no trace endpoint). Session id, user id, prompt version and cost were read back; tags were verified on the OpenTelemetry span attributes. Check the tags once in the Langfuse UI.
- **The poller handles one mail at a time.** If the portal limiter has to wait (more than 10 rate reads in a minute), later mails wait too. One request needs at most 8 reads, and requests arrive 6 minutes apart, so this only bites while the old process shares the limit.
- **Email-booked (tariff) airlines:** the booking mail and the confirmation path are tested with mocks only. No tariff airline has ever been booked for real, so the airline's real reply format is unknown.
- **`router/.env` is tracked by git** (commit `6c3c19a`, before this work) and contains the team key. Consider `git rm --cached router/.env` and rotating the key if the repo is ever shared.
- **Test LLM calls went through the team gateway** and may count toward the scored LLM cost (ADR 27).

## What is open

1. **Step 9, live dry run:** restart the local router with the new code (`SEND_ENABLED=false`) and watch 2 live rounds: `GET /shipments`, Langfuse sessions. It shares the portal rate limit with the old process.
2. **Step 10, cutover:** [workflow.md](workflow.md) section 12. Stop the old process right after a round closes, set `SEND_ENABLED=true`, restart.
3. **Merge** `quote-workflow` into `main`.
4. Backlog from HANDOFF.md: dangerous goods + attachments, tracking answers (+10 per booked shipment), tariff updates.
