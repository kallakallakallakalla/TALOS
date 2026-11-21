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

1. **Unit tests** (`cd router && .venv/bin/pytest`): 63 tests, no network. The platform is an `httpx.MockTransport`, LLM answers are scripted from the hand labels. `tests/test_workflow.py` covers: the round 4 replay, the dry run (nothing sent, nothing booked), the 45 s / 90 s trucker wait, 3 clarifications then FAILED, unknown zone, expired rate refetch, booking by email with reminders, 3 answers per thread, rejection, and "a carrier mail never opens a shipment".
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

### Full replay (all 151 recorded mails, rounds 1–16; live LLM, live portal rates, nothing sent)

Run with `scripts/replay.py` on 2026-11-21. Output: `router/data/replay/result.json` and `replay.log`.

- **Classification inside the real pipeline: 151 / 151** as labelled (here the context comes from the workflow's own state, not from the dataset).
- **Fact check: 38 composed mails, 0 template fallbacks.**
- **Portal: 46 live rate reads** for 14 priced requests (3.3 per request).
- **No mail was sent, nothing was booked.**

Our quote next to the old process's quote for the same request ("verdict" is the customer's answer to the **old** quote, from `GET /v1/board/me`):

| Round | Mail | Request | Old process | Verdict on old quote | This router | Same price? |
|---|---|---|---|---|---|---|
| 1 | 275 | fastest, pickup 51447 → ORD | 2494.80, no trucking | rejected, too_low | Meridian (1 day) 3010.00 + trucker | no: trucking included, faster airline (see note A) |
| 2 | 431 | fastest, pickup 61018 → JFK | 4491.28, no trucking | rejected, too_low | Solace (2 days) 5100.00 + trucker | no: trucking included, faster airline (note A) |
| 3 | 741 | fastest DUS → JFK | 3696.00 | accepted | 3696.00 Fjordline | **yes** |
| 4 | 947 | cheapest, pickup 51896 → ORD | 1599.41 | accepted | 1599.41 Xenon + Elbkante | **yes** |
| 5 | 1233 | fastest STR → EZE | 4408.52 | accepted | 4408.52 Fjordline | **yes** |
| 6 | 1470 | cheapest DUS → KUL | 3949.95 | accepted | 3949.95 Fjordline | **yes** |
| 7 | 1638 | fastest MUC → LAX, 2 lots | 2780.00 | accepted | 2780.00 Fjordline | **yes** |
| 8 | 1844 | cheapest MUC → CAI, 2 lots | 1475.00 | accepted | 1475.00 Borealis | **yes** |
| 9 | 1977 | cheapest, pickup 36627 → LOS, 3 lots | no answer | – | Lumen 4840.96 + trucker | new: old process failed on the lots (note B) |
| 10 | 2267 | cheapest, pickup 61391 → GRU | 2685.35 | rejected, dg_docs_missing | 2685.35 | yes: **same mistake** (note C) |
| 11 | 2540 | cheapest, pickup 21663 → YYZ | 2085.80 | rejected, dg_docs_missing | 2085.80 | yes: **same mistake** (note C) |
| 12 | 2829 | cheapest FRA → DOH | 324.36 | accepted | 324.36 Ultraviolet | **yes** |
| 13 | 2977 | cheapest, pickup 24983 → DEL | 2875.06 | accepted | 2875.06 Borealis + Alpenblick | **yes** |
| 14 | 3327 | cheapest, pickup 42401 → PEK | 3116.91 | rejected, too_low | 3116.91 | yes: **same rejected price** (note D) |
| 15 | 3637 | cheapest → SGN, "pickup from our warehouse" | no answer | – | asks for the postal code or airport | new behaviour (note E) |
| 16 | 3940 | fastest → PVG, "pickup from our warehouse" | quoted | rejected, too_high | asks for the postal code or airport | new behaviour (note E) |

**Reading the table**

- **All 8 quotes that customers accepted are reproduced to the cent** (rounds 3–8, 12, 13), including both two-lot requests and three "fastest" requests where a cheaper but slower tariff airline existed and was correctly not chosen.
- **The state machine followed every recorded verdict:** 8 accepts → dry-run booking and a booking note; round 6's real airline confirmation (reference "1470") → CONFIRMED; rejections → REJECTED; 73 thank-you mails → no reply.

**Notes**

- **A. Rounds 1 and 2 (the two `too_low` rejections for missing trucking):** this router includes trucking and picks the airline with the fewest transit days (ADR 08). Whether that matches the scoring reference is unproven: no "fastest with pickup" quote has ever been accepted. In the replay the trucker part is a fallback price (189.28 and 403.45), because the old process asked the truckers late and their recorded replies arrive after our 90 s limit. Live, truckers answer about 30 s after our own request.
- **B. Round 9:** the lots are summed correctly (1952 kg, 4.68 cbm). The recorded trucker replies say "we need the weight" (the old process had sent "n/a"), so the replay used the fallback trucker price: 547.57. That is 0.28 EUR/kg, which is far above any real trucker quote seen (75–380 EUR whatever the weight). **The fallback price is too high for heavy shipments.**
- **C. Rounds 10 and 11, dangerous goods:** the router repeats the old process's mistake. Not built (backlog).
- **D. Round 14:** the same price was rejected as `too_low`, and the reason is not understood. Air (Gannet 3003.86) and trucker (Spessart 113.05; the other three asked 322–380) are both the cheapest offers received.
- **E. Rounds 15 and 16:** the request has no pickup postal code. The router asks instead of guessing. No reply from a simulated customer to such a question has been observed.

### LLM cost and speed (from Langfuse, replay traffic)

| Prompt | Calls | Avg. cost per call |
|---|---|---|
| `router-classification` | 157 | $0.00011 |
| `talos-extract-rfq` | 16 | $0.00011 |
| `talos-extract-trucker-quote` | 33 | $0.00010 |
| `talos-extract-booking` | 12 | $0.00008 |
| `talos-compose-mail` | 38 | $0.00017 |

- A complete round with pickup (request, 4–5 trucker quotes, quote, accept, booking note) is about **15 calls and $0.002**. Costs are Langfuse's estimate from token counts; the gateway's own accounting is what is scored.
- LLM latency: median 1.5 s, 90th percentile 2.6 s per call.
- **Response time was not measured live.** Expected: under 10 s for requests without pickup (3 LLM calls + portal reads + up to 2 s poll delay), and about 35–40 s with pickup, bounded by the truckers' ~30 s.

## Tracking answers (added after step 8, ADR 37)

**What the old process did, and why it scored:** from about 16:49 UTC the old process answered tracking mails. It replied in the thread to `tracking@hackloop.mail` with route, airline, booking reference, status, booking time and transit time (e.g. mail 4633). `GET /v1/board/me` shows `tracking: 10.0` for rounds 17–22 and `0.0` for the earlier ones, where it either did not answer (mail 1814) or answered "we could not find a booking under your address" (mail 3286). So an answer in that thread with the booking facts is what earns the 10 points.

**How this router answers a tracking question**

1. The classifier labels the mail `tracking_request`. They come from `tracking@hackloop.mail`, unthreaded, on behalf of a customer.
2. `talos-extract-tracking` (LLM) reads which shipment is meant: company, origin (postal code or airport), destination, gross weight, ready date, and a booking reference if one is stated.
3. `workflow.match_shipment()` (plain Python) finds the shipment in our database: a stated booking reference wins; otherwise destination and gross weight (±1 kg) must match, and among several the one with the same ready date and origin, then the newest.
4. The answer is built **only from our own records**: status text for the shipment's state (`TRACKING_STATUS` in `workflow.py`), route, airline, booking reference, booking time, transit days, estimated pickup / departure / arrival, and who brings the goods to the airport. No airline or trucker is asked.
5. The composer writes it (`tracking_answer` kind) in the language of the question, addressed to the person who signed it; the booking reference must appear verbatim. It is sent as a reply in the tracking thread.
6. No match: the answer says so and asks for the booking reference. A thread gets at most 3 answers from us.

**Tested on the recorded tracking questions** (dry-run replay, real LLM): all three that belong to shipments in the recording were matched to the right shipment and answered.

| Tracking mail | Asked about | Matched shipment | Answer |
|---|---|---|---|
| 1814 | Kessler, DUS to KUL, 1549 kg | 1470 (CONFIRMED) | "booked and confirmed by the airline", BK-8RCF17DZ, Fjordline, departure 2026-11-26, arrival 2026-12-01 |
| 3279 | Hofmann, FRA to DOH, 102 kg | 2829 (BOOKED) | "booked with the airline", Ultraviolet, 3 days |
| 3612 | Auer, 24983 to DEL, 1619 kg | 2977 (BOOKED) | "booked with the airline", Borealis, pickup 2026-11-28, arrival 2026-12-05 |

In the replay the last two carry the placeholder reference "DRY-RUN", because a dry run does not book.

**Limits**

- **Shipments the old process booked can't be answered with details after the switch.** They exist in our database only as closed legacy rows without destination, weight or booking reference, so a tracking question about them gets the "please send your booking reference" answer. Tracking questions arrive roughly 5–10 minutes after a booking, so this affects the last one or two old bookings.
- The status is what we know (booked / confirmed); there is no flight or location data, and the mail says so.
- What exactly the scorer checks is not documented. The answer contains everything the old process's scored answers contained.

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
| Trucker request written by the LLM | fixed template | user decision after the replay (ADR 38); the replay numbers above still include that LLM call |
| Prompts as drafted in prompts.md | small additions | found by the eval loop (ADR 35); `router/prompts/*.md` is now the source of truth |

## Cutover preparation: old mail is marked as handled

`router/scripts/backfill_legacy.py` was run on `router/data/router.db` (2026-11-21, ~18:05 CET): all 199 mails then in the mailbox are stored as processed, the poll cursor is at the newest one, and each of the 21 customer threads has a closed shipment (`FAILED`, note "legacy: handled by the old process before cutover"). A late reply in an old thread therefore triggers nothing. The script is idempotent and must be run once more right at cutover, so mails that arrived in between are covered too.

Known gap: a request the old process quoted, but whose acceptance arrives only after the switch, is not booked by the new router. Switch right after a round closes.

## Limits and things to know

- **Not built** (out of scope in the plan, or backlog in HANDOFF.md): applying tariff-update mails, rate offers from email-only airlines, the webhook, attachment reading and the dangerous-goods check. These mails are classified, stored and tagged `needs_review`; no mail goes out.
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
4. Backlog from HANDOFF.md: dangerous goods + attachments, tariff updates.
