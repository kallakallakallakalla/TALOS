# Architecture & decision records

Decisions 01–24 were taken in the planning session of 2026-11-21 (16:00–17:20 CET); 25–41 in the implementation session that followed (17:25 CET onwards). "User" means the decision came from the project owner; "proposed" means Claude proposed it and the user accepted it or didn't object. The full plan these decisions feed into is [workflow.md](workflow.md).

| # | Decision | Decided by |
|---|---|---|
| 01 | Scope: air export with optional trucker pickup; **no truck-only routes** | User |
| 02 | This repo replaces the old process on the other computer | User |
| 03 | Extend the existing `router/` architecture; no new framework | User |
| 04 | Poll the mailbox every 2 s; webhook later | User |
| 05 | Ask only the truckers that serve the postal zone | User |
| 06 | Portal API and local tariff DB are equal price sources | User |
| 07 | Book automatically on acceptance; API for portal airlines, email for tariff airlines | User |
| 08 | Option ranking: fastest / cheapest; default cheapest | User |
| 09 | Margin 0 | User |
| 10 | Trucker wait: 45 s, then a fallback price at 90 s | User (fallback) + proposed (timings) |
| 11 | Every loop is capped at 3 | User |
| 12 | An LLM reads every mail; no regex on mail text | User |
| 13 | An LLM writes every mail; code checks the facts | User (LLM, human-readable) + proposed (check, fallback) |
| 14 | `gpt-6-luna` for every LLM call | User |
| 15 | Classifier first, then focused extractors | User |
| 16 | Context lookup (no LLM) before the classifier | Proposed |
| 17 | Prompts in Langfuse Prompt Management with an eval loop | User (iterable) + proposed (mechanism) |
| 18 | Langfuse tracing: trace per mail, session per shipment | User (logging) + proposed (shape) |
| 19 | One quote per thread, enforced in code | Platform rule |
| 20 | Fix the existing poller's endless retry | Proposed |
| 21 | Dry-run flag and a replay test before cutover | Proposed |
| 22 | Start kit committed to the repo | User |
| 23 | Classifier improvement happens later, on the mail history | User |
| 24 | No manual bookings; booking belongs in code | User |
| 25 | Mails are signed "Alex, TALOS Air Freight" | User |
| 26 | Work on branch `quote-workflow`, one commit per build step | User |
| 27 | Prompts are measured on all recorded mails with real `gpt-6-luna` calls | User |
| 28 | Live portal reads in tests are allowed, throttled to 3 per minute | User |
| 29 | Unknown pickup zone: ask the customer for an airport | User |
| 30 | No live dry run yet; testing is on recorded mails only | User |
| 31 | Reasoning effort per task: classifier none, extractors low, composer none | Proposed |
| 32 | Prices are written as `1599.41 EUR` | Proposed |
| 33 | Composed mails are stored in an `outbound_mails` table | Proposed |
| 34 | The next weight break is only priced when it is at most 50% above the chargeable weight | Proposed |
| 35 | Prompt additions from the eval loop (lots, "import" wording, quoted mails, acknowledgements) | Proposed |
| 36 | In a dry run, trucker replies are matched to our request by sender | Proposed |
| 37 | Tracking questions are answered from our own records, without asking carriers | User |
| 38 | Trucker requests are a fixed template, not LLM-written (amends 13) | User |
| 39 | Tests, evals and replays call OpenAI directly with `EVALUATION_KEY`, never the gateway (amends 27) | User |
| 40 | `TEST_MODE=true` runs the live router as a test (amends 30) | User (test mode) + proposed (what it switches) |
| 41 | Trucker replies: no call per mail, one LLM call for all, wait at most 60 s (amends 10, 15) | User |

---

### 01 · Scope: air export with optional trucker pickup, no truck-only routes

- **Context:** the user's first sketch (`state.pdf`) had two branches: "Truck only" and "Truck + Airfreight". Every request received was an export from Germany to an international airport. Truckers only quote pickup from the customer to a German airport.
- **Decision:** every shipment is an optional trucker pickup followed by air freight. The truck-only branch is dropped.
- **Consequence:** there is no domestic trucking logic. The origin is either the airport for the customer's postal zone, or an airport the customer delivers to themselves.

### 02 · This repo replaces the old process on the other computer

- **Context:** a first version runs on another computer with the same team key. It wrote the "Air Freight Desk" mails. During the session it also sent duplicate trucker requests and didn't book at first. This repo is the newer version that is supposed to be better.
- **Decision:** this router replaces it, with the goal of being faster and better. Switch over right after a round closes, so that **only one process ever sends**.
- **Consequence:** until the switch-over, this router runs with `SEND_ENABLED=false`.

### 03 · Extend the existing `router/` architecture

- **Decision:** build on FastAPI + `MailboxPoller` + `MessageStore` (SQLite) + `DecisionClassifier` + Langfuse. No new framework, scheduler or service.
- **Note:** the user mentioned "LangChain". The code actually uses **Langfuse**: tracing plus prompt management, with OpenAI calls going through `langfuse.openai`.

### 04 · Poll the mailbox every 2 s; webhook later

- **Context:** the user thought the 10/min limit also applied to polling. It only applies to `GET /v1/portal/rates`. The mailbox has no stated limit, and its docs ask for a poll every 1–2 s. A webhook (`PUT /v1/mailbox/webhook`) exists; it was found by comparing the old local `openapi.json` with the live one, and needs a public URL or tunnel.
- **Decision:** poll every 2 s (`MAILBOX_POLL_INTERVAL_SECONDS=2`). The webhook may come later; the poll stays as a safety net even then.

### 05 · Ask only the truckers that serve the postal zone

- **Decision:**
  - zone = the first 2 digits of the pickup postal code
  - airport = `pickup_zones.csv[zone]`
  - truckers asked = carrier-list rows with `kind=trucker` whose `serves` contains the zone

  Example: 51447 → zone 51 → CGN → Maintal, Elbkante, Westfalenroute, Bodenseefracht.
- **Consequence:** the trucker request is written once per shipment and sent to each matching trucker. Each trucker is asked once.

### 06 · Portal API and local tariff DB are equal price sources

- **Context:** 15 airlines are priced on the portal, 10 only through the start-kit tariff files, and 4 only by email (exotic destinations). No airline is in more than one group.
- **Decision:**
  - A local tariff lane counts as a normal option while today is within its `valid_from`–`valid_to` and nothing newer from that airline has arrived.
  - A newer tariff from an airline **replaces that airline's lanes**.
- **Consequence:** `data/tariffs.json` is built once from the start kit, using an LLM for the messy files. `tariffs.replace_airline()` is the hook for tariff mails later.

### 07 · Book automatically on acceptance; API for portal airlines, email for tariff airlines

- **Context:** the portal booking API only accepts a `rate_id` that the portal issued. Tariff prices have no `rate_id`.
- **Decision:** as soon as the customer accepts:
  - portal option → `POST /v1/portal/bookings` (a 410 means the rate expired: refetch it and book)
  - tariff option → a booking email to the airline's address from the carrier list; its "Booking … confirmed" mail completes the booking

### 08 · Option ranking; default to cheapest

- **Decision:**
  - **fastest** = min air transit days → min trucker pickup hours → min price
  - **cheapest** = min air total + min trucker price
  - **no goal stated → cheapest**, without asking the customer

### 09 · Margin 0

- **Decision:** `MARGIN_EUR=0` for now. The 45 price-accuracy points probably punish any deviation from the reference price.

### 10 · Trucker wait: 45 s, then a fallback price at 90 s

- **Decision:**
  - Quote once every asked trucker has replied, or after 45 s if at least one has.
  - If none has replied after 90 s, quote with a **fallback trucker price** and hope a trucker still answers.
  - Fallback = the median €/kg of our stored trucker quotes (same zone first) × the shipment's kg. Until we have stored quotes it's 0.28 €/kg (from €192.97 for 679 kg).
- **Consequence:** the only remaining reason a shipment fails in pricing is that no airline covers the route.

### 11 · Every loop is capped at 3

- **Context:** the user saw cycles in the state-machine diagram and asked for a hard limit.
- **Decision:** every cycle stops after 3 rounds:
  - clarification questions
  - booking attempts
  - mail-processing retries
  - portal 429 retries
  - automatic answers per thread

  The composer gets 1 retry. Trucker waiting ends by time (45 s / 90 s), with each trucker asked and counted once.

### 12 · An LLM reads every mail; no regex on mail text

- **Context:** the first plan parsed trucker quotes with a regex.
- **Decision (user):** regex or plain parsing is only for API responses (JSON) and CSV data. Anything that comes from a mail, or from another human-written source (tariff files), is read by an LLM with structured output.

### 13 · An LLM writes every mail; code checks the facts

- **Decision:** every outgoing mail is written by an LLM from structured facts, in a human, warm, short tone, in the language of the thread. The kinds are quote, trucker request, clarification, booking note, booking request and answer.
- **Guardrails (proposed):**
  - Python checks that every price, reference and date from the facts appears verbatim.
  - 1 retry, then a plain template, so a mail always goes out.
  - The workflow decides who gets which mail; the LLM only writes the words.
- **Open:** `MAIL_SIGNATURE`. A person's name probably helps the "most human email" jury; round 1 was signed "Alex". This needs a name from the user.

### 14 · `gpt-6-luna` for every LLM call

- **Context:** the user first wanted a bigger model ("6 Terra or 6.1 Terra") for everything except the classifier. The gateway has `gpt-6.1-terra` but **no `gpt-6-terra`**. The user then decided to use `gpt-6-luna` for everything.
- **Decision:** `decision_model = extract_model = compose_model = "gpt-6-luna"`, each set separately in config so any task can be switched without code changes.

### 15 · Classifier first, then focused extractors

- **Context:** Claude suggested dropping the classifier because a reader would classify anyway. The user disagreed: with a classifier in front, **each follow-up call has one task less**, so its prompt is more focused.
- **Decision:**
  - The existing `DecisionClassifier` stays the first LLM step.
  - It gets new, fine-grained categories: `rfq`, `customer_info`, `customer_accept`, `customer_reject`, `customer_question`, `trucker_quote`, `trucker_decline`, `booking_confirmation`, `rate_offer`, `tariff_update`, `tracking_request`, `other`.
  - Only the categories that carry data get a second, type-specific extractor call (rfq, customer_info, trucker_quote, booking_confirmation).
- **Consequence:**
  - About 14 luna calls per request.
  - `processed_messages.category` stores the new values.
  - The classifier is given the context lookup's result as extra input.

### 16 · Context lookup (no LLM) before the classifier

- **Decision:** before classifying, look up:
  - the sender's role from the carrier list (customer / trucker / airline / ratedesk / tracking / unknown)
  - the thread (`in_reply_to`, `references`) → the shipment and its state

  Both come from API metadata and our own database, not from mail text.
- **Why:** on the real data, the old classifier labelled 5 trucker quotes "RFQ". Without context they would have started fake shipments.

### 17 · Prompts in Langfuse Prompt Management with an eval loop

- **Decision:**
  - One named, versioned prompt per task: `router-classification`, `talos-extract-rfq`, `talos-extract-trucker-quote`, `talos-extract-booking`, `talos-compose-mail`.
  - Each has a local fallback in `router/prompts/<name>.md`.
  - One Langfuse dataset per prompt, built from the real past mails plus hand labels.
  - `eval_prompts.py` runs a `staging` version against its dataset, and the version is promoted to `production` only if it is at least as good.
- **The drafts:** [prompts.md](prompts.md).

### 18 · Langfuse tracing: trace per mail, session per shipment

- **Decision:**
  - One trace per incoming mail, with `session_id = shipment-<id>`, `user_id = sender` and tags (role, category, state).
  - LLM generations are recorded automatically via `langfuse.openai`, linked to their prompt version.
  - Spans for portal calls, tariff lookup, `pick_best`, mail send and booking.
  - Scores: `customer_verdict`, `fact_check`, `extraction_complete`.
  - The tag `needs_review` marks anything unhandled.

### 19 · One quote per thread, enforced in code

- **Context (platform rule):** only the first quote in a customer-started thread counts.
- **Decision:** never send a quote if `shipments.quote_message_id` is already set. A carrier's reply never triggers a mail back to that carrier, and an `other` mail never triggers any mail.

### 20 · Fix the existing poller's endless retry

- **Finding:** `MailboxPoller.poll_once` stops at a failing mail and retries it on every tick, so all later mail is blocked.
- **Decision:** a new table `message_failures` caps retries at 3. After that the mail is stored as failed and the cursor moves on.

### 21 · Dry-run flag and a replay test before cutover

- **Decision:**
  - `SEND_ENABLED=false` by default: mails are composed and logged, never sent.
  - Replay test: the round 4 mails must produce a quote containing **€1599.41** and a booking call with reference "947".
  - Then 2 live rounds in dry run before the switch-over.

### 22 · Start kit committed to the repo

- **Decision:** `data/start-kit/` is committed (commit `18878bb`). The local tariff DB is built from it.

### 23 · Classifier improvement happens later, on the mail history

- **Decision (user):** for now, keep the classifier as is; only the categories change for routing. Improving it comes later, using the full history of past mails as datasets.

### 24 · No manual bookings; booking belongs in code

- **Context:** during the session, Claude booked round 6 by hand with the user's approval (`BK-8RCF17DZ`, team_reference "1470"). Afterwards the user said: **don't book anything by hand; booking is supposed to be in the code.**
- **Decision:** no manual bookings from dev sessions. Booking only happens through the workflow (ADR 07).

---

## Implementation session (2026-11-21, from 17:25 CET)

### 25 · Mails are signed "Alex, TALOS Air Freight"

- **Decision (user):** `MAIL_SIGNATURE=Alex, TALOS Air Freight`. A person's name reads more human, and round 1 was already signed "Alex". This closes the open point of ADR 13.

### 26 · Work on branch `quote-workflow`, one commit per build step

- **Decision (user):** `docs/` and the refreshed `openapi.json` are committed first, then one commit per build step of workflow.md section 11. `main` stays untouched until the user merges.

### 27 · Prompts are measured on all recorded mails with real `gpt-6-luna` calls

- **Context:** LLM cost is scored, and test calls go through the same team gateway.
- **Decision (user):** a full pass over all recorded mails is worth the one-off cost. The recorded mails live in `router/tests/fixtures/mails.json`, the hand labels in `labels.json`.

### 28 · Live portal reads in tests are allowed, throttled to 3 per minute

- **Decision (user):** the replay may read real portal rates, capped at 3 per minute so the live process keeps 7 of the shared 10. `scripts/replay.py` also caches every rate on disk, so a second run costs no reads.

### 29 · Unknown pickup zone: ask the customer for an airport

- **Context:** a postal zone that is not in `pickup_zones.csv` (e.g. 90 Nuremberg) has no airport and no trucker. The user asked whether an API could resolve it. It can't: the platform only has mailbox, portal, gateway and board endpoints.
- **Decision (user):** treat it as missing information and ask the customer which German airport they can deliver to. It counts against the limit of 3 clarification questions.

### 30 · No live dry run yet; testing is on recorded mails only

- **Decision (user):** the classify-only process on port 8000 is left alone. Step 9 of the build order (live dry run for 2 rounds) and step 10 (cutover) stay open.

### 31 · Reasoning effort per task

- **Finding:** `gpt-6-luna` is a reasoning model. With the default effort a one-word answer cost 33 reasoning tokens; `reasoning_effort="none"` cost 0 and was 3 times faster. Supported values are none, low, medium, high, xhigh.
- **Decision (proposed):** classifier `none`, extractors `low` (multi-lot arithmetic), composer `none`. Each is a setting (`*_REASONING_EFFORT`). With these values the classifier scored 151/151 and the extractors 59/59 on the recorded mails.

### 32 · Prices are written as `1599.41 EUR`

- **Context:** the plan's example was "EUR 1,599.41". The quotes that customers accepted used "1599.41 EUR", and a thousands separator can be misread as a decimal comma.
- **Decision (proposed):** two decimals, no thousands separator, currency after the number. The fact check requires exactly this string in the mail.

### 33 · Composed mails are stored in an `outbound_mails` table

- **Context:** the plan's data model had no place for outgoing mail. In a dry run nothing is sent, so there was nothing to inspect, and a reply's "our mail" context could not be looked up.
- **Decision (proposed):** every composed mail is stored (kind, recipient, subject, body, `in_reply_to`, template flag). `sent_message_id` stays empty in a dry run. `GET /shipments/{id}` shows them.

### 34 · The next weight break is only priced when it is close

- **Context:** the plan reads two portal rates per airline (real weight + next break). A lane has up to 4 airlines, so one request could use 8 of the 10 reads per minute.
- **Decision (proposed):** the next break is only read when it is at most 50% above the chargeable weight. In all start-kit tariffs a rate drops by less than 35% per break, so a break further away can't be cheaper.

### 35 · Prompt additions from the eval loop

- `talos-extract-rfq`: several lots are one shipment (sum pieces, gross weight, volume); every request is an export even if the buyer "imports"; ignore quoted older mails below the customer's text; always gross weight.
- `router-classification`: a customer who only thanks or acknowledges is `other`; a trucker who asks for more details instead of quoting is `trucker_decline`; a trucker answering a pickup order with "nothing to book" is `other`.
- `talos-compose-mail`: every `must_appear` value must occur character for character; arrival and departure questions are answered from the estimated dates.
- **Note:** these cover backlog items 1, 3 and 5 of HANDOFF.md at prompt level only. Dangerous goods, attachments and tracking answers are still not built.

### 36 · In a dry run, trucker replies are matched to our request by sender

- **Context:** in a dry run our trucker requests are never sent, so the truckers' replies answer the old process's mails and can't be matched by `in_reply_to`.
- **Decision (proposed):** only while `SEND_ENABLED=false`, a trucker's reply is matched to that trucker's newest unanswered request. With sending on, only the exact mail id counts.

### 37 · Tracking questions are answered from our own records

- **Context:** the plan left tracking answers out of scope. The old process started answering them at about 16:49 UTC and earned the 10 tracking points on rounds 17–22; its answer went to `tracking@hackloop.mail` in the thread and listed route, airline, booking reference, status, booking time and transit time.
- **Decision (user):** when a tracking request comes in, answer with the latest state we know from our database, as humanly as possible. No mail is written to an airline or trucker to look anything up, for now.
- **How:** `talos-extract-tracking` reads which shipment is meant; `workflow.match_shipment()` finds it by booking reference, or by destination and gross weight (then ready date and origin); the composer writes a `tracking_answer` from the shipment's state and booking facts. Details and test results are in [implementation.md](implementation.md#tracking-answers-added-after-step-8-adr-37).
- **Consequence:** shipments booked by the old process can't be answered with details after cutover, because our database has no booking facts for them.

### 38 · Trucker requests are a fixed template

- **Context:** ADR 13 had the LLM write every outgoing mail, including the pickup request to truckers. That request is the same list of values every time, its readers are carriers, and the "most human email" jury only reads mails to customers.
- **Decision (user):** the trucker request is a template with the values filled in (`templates.py`, kind `trucker_request`): postal code and city, airport, ready date, pieces, gross weight, volume. The same text goes to every trucker of the zone.
- **Consequence:** one LLM call less per shipment with pickup, and the request leaves a second or two earlier. All mails to customers and the booking request to airlines are still LLM-written with the fact check.

### 39 · Tests, evals and replays call OpenAI directly, never the gateway

- **Context:** every LLM call through the challenge gateway is counted as production usage and scored (40 of 100 points per mail). Until now the eval and replay runs went through the gateway (ADR 27).
- **Decision (user):** `EVALUATION_KEY` in `router/.env` is a separate OpenAI key for every test, evaluation and replay run from now on. It is used directly against `https://api.openai.com/v1`. **It must never be registered at the gateway** (`POST /v1/gateway/openai-key`).
- **How:** `config.evaluation_settings()` swaps the key and base URL and is used by `eval_prompts.py`, `replay.py`, `compose_samples.py` and `build_tariffs.py`. It raises if the key is missing or if the evaluation URL points at the gateway, so a test run can't fall back to scored usage. The running router is unchanged: it uses `DECISION_API_KEY` through the gateway.
- **Checked:** `gpt-6-luna` is available with this key directly at OpenAI; the booking-extractor eval scored 12/12 on that route.

### 40 · `TEST_MODE=true` runs the live router as a test

- **Context:** the user asked to restart the servers "in test mode". A new-code dry run had already been running on port 8000 since about 17:14 UTC, started outside this session: it used the gateway for its LLM calls and wrote 6 dry-run shipments into the production database `router.db`.
- **Decision:** one switch, `TEST_MODE=true` in `router/.env`, makes the whole router test-safe (`config.as_test_settings`):
  - nothing is sent, nothing is booked (`send_enabled` is forced to false)
  - LLM calls use `EVALUATION_KEY` directly at OpenAI, not the scored gateway (ADR 39)
  - shipments and mails go to `data/test.db`, so test shipments never mix with production ones
  - portal rate reads are throttled to 3 per minute (ADR 28)
- **This is step 9 of the build order** (live dry run), which ADR 30 had postponed.
- **Cutover from test mode:** set `TEST_MODE=false` and `SEND_ENABLED=true`, run `scripts/backfill_legacy.py`, restart. The backfill now also closes every shipment left over from a dry run (quote never sent, booking "DRY-RUN"); otherwise a later "please book" could book such a shipment for real.

### 41 · Trucker replies are read together in one LLM call

- **Context:** each trucker reply cost two LLM calls (classifier + extractor). With 4–5 truckers that was 8–10 of about 15 calls per request, the largest cost block.
- **Decision (user):**
  - Any mail from a trucker we asked, who has not answered yet, **is** that trucker's answer. It gets no classifier call and is stored as `trucker_quote`. Matching is by the mail we sent, or else by sender.
  - The quote goes out as soon as every asked trucker has answered, or after **60 s** with whatever came back (`TRUCKER_WAIT_SECONDS`). Nothing came back: fallback trucker price. This replaces the 45 s / 90 s rule of ADR 10.
  - All replies are then read in **one** LLM call (`talos-extract-trucker-quotes`): bodies only, no headers, a short prompt, no reasoning tokens.
  - The cheapest (or fastest) trucker is still chosen by code, not by the LLM: comparing numbers in code is exact.
- **Accuracy guard:** a price is only accepted if that exact number stands in that trucker's own mail. One retry; an offer that still fails is dropped. If the call fails altogether, the fallback price is used.
- **Measured** (evaluation key, all recorded trucker replies): 7 pickup requests with 34 replies (31 quotes, 3 "we need the weight"), every field correct, in three runs, with reasoning effort `none` and `low`. `none` is used.
- **Effect:** for a request with 5 truckers, 10 LLM calls become 1.
- **Note on ADR 12:** mail text is still read by an LLM. Code only checks that the extracted price occurs in the mail.

### ADR 42: Customer attachments are read locally; vision only when they block a quote

- **Decision:** customer attachments are downloaded once and turned into text without an LLM (`app/attachments.py`: pdftotext, xlsx/docx zip XML, csv/txt). The text (max 3,000 characters per file) is stored with the mail and added to `mail_text()` as `ATTACHMENT <name>:` blocks. Vision: see ADR 43.
- **Conflicts:** if the body and the attachment disagree on a cargo figure, the attachment wins (it is the formal document). Mails 8500, 8667 and 8869 had lower weight or volume in the attachment, and all three customers turned the quote down as too expensive.
- **Not built:** tariff-update attachments and xls files (no local parser).

### ADR 43: Dangerous goods wait for their documents; vision reads what text extraction cannot see

- **Dangerous goods:** the quote-request extractor fills `dangerous_goods` from a UN number, a hazard class, a dangerous-goods entry in an attachment, or goods that are regulated by nature (lithium batteries, aerosols, flammable liquids, adhesives, ...). "Non-dangerous" in the mail does not override this. Until `dg_documents_received` is true, `missing_fields` asks for the Shipper's Declaration and the MSDS. We send no quote and do no pricing; after 3 questions the shipment fails. Mails 2267, 2540 and 10142 were turned down for exactly this reason.
- **Vision when the mail comes in:** one call per file, and only for files with something text extraction cannot see: an image (png/jpg/gif/webp), a PDF with images or with no usable text (the whole PDF is sent), or a docx/xlsx/pptx with pictures (at most 4 pictures of at least 5 KB each; the vision text is added to the file's own text). Text-only files never trigger a vision call. The result is stored with the mail.
- This replaces ADR 42's "vision only when fields are missing": a dangerous-goods label in a picture never shows up as a missing field.

### ADR 44: Arrive-by dates and date checks

- **Arrive-by date:** `deliver_by` is the latest arrival date the customer accepts. `pick_best` keeps only the options whose estimated arrival is on or before it, then applies the goal. If none arrives in time, we quote the fastest option and the mail says so.
- **Date checks:** `pricing.date_problems` checks, in code, for a ready date in the past, a delivery date before the ready date (probably swapped) and a ready date more than 90 days ahead. Each problem becomes a follow-up question through `missing_fields` (at most 3 questions). The extractor copies dates as written and does not "fix" them.
- **Tariff validity:** tariff lanes must be valid on the shipping day, which is the later of today and the ready date, not only today.

### ADR 45: Special cargo is quoted only once its handling details are known

- `special_cargo` is one of: live_animals, perishables, temperature_controlled, valuables, human_remains. Until `special_cargo_details_received` is true, `missing_fields` asks for the details listed per type in `SPECIAL_CARGO_DETAILS`. After that we quote normally, with no special-cargo surcharge, because we have no data for one.

### ADR 46: Preferred and excluded carriers

- The extractor copies the names as written. `pricing.carrier_in` matches them in code, by word, ignoring words like "Air", "Cargo" and "GmbH".
- Excluded carriers are never asked or used. If excluding them would leave no airline at all, we ignore the exclusion rather than fail.
- A preferred carrier with a price is used. If the best option without the preference is cheaper, the quote names it with its price (`cheaper_alternative`). A preferred carrier without a price gets a short note in the mail.
- The template fallback mail does not mention preferences or the arrive-by date. It is only used when the LLM mail fails its checks.

### ADR 47: Airlines are asked by email when the portal cannot price; a 503 is retried once

- **Email-only destinations** (ASU, KTM, PPT, TNR, ULN, i.e. not on the portal's lane list): the airlines in `carrier-list.csv` that serve the destination get a rate request (template `airline_rate_request`). Mail 10830 (FRA-ULN) failed silently for lack of this.
- **Portal down** (503 "The rate portal is down. Contact the airline directly."): every non-tariff airline that serves the destination gets the same request. Tariff prices are still used.
- **Temporary 503** ("Rates are still published on the portal page"; the page has the same data as the API): wait 60 s, retry once (`portal_unavailable_retry_seconds`). If it still fails, the portal counts as down.
- **Lane list:** the 114 lanes are saved to `data/portal_lanes.json` and used when `/ports` is down.
- **Replies:** like trucker replies (ADR 41), with no classifier call, matched by `in_reply_to` or sender and read in ONE LLM call (`talos-extract-airline-rates`). A price counts only if it appears in the mail. It becomes an `AirOption(source="email")`, and booking goes by email, as for tariff airlines.
- **Wait:** quote after 60 s (`airline_wait_seconds`) or once all airlines have answered. With no price at all, keep waiting for a late reply up to 10 minutes, then send the customer a `no_price` mail and mark the shipment FAILED. No more silence.
- **Storage:** table `airline_requests`, the same shape as `trucker_requests` plus `transit_days`.
- **Booking an email quote:** the airline's offer reference (for example "R-NGz6tmITGPLXxEZU") goes into the booking request.
- **Mailbox limit:** the platform now allows one poll per 5 seconds (429 "one poll per 5 seconds"). The poll interval is 5 s, and a 429 waits for its Retry-After + 1 s. The timers (quotes, reminders) also run when a poll fails.

### ADR 48: Customer mails: short, corporate-light, varied, with a footer

- **Context:** the composer wrote correct but uniform mails: the same structure and closing every time, raw timestamps ("2026-11-21 15:34 UTC"), the price on its own line after a longer lead-in. The "most human email" jury reads exactly these mails.
- **Decision (user):** the user set the style rules and picked the tone from five written examples (the "corporate light" one):
  - plain text only: no markdown, no lists, no em dashes; none of the stock phrases "Certainly", "I hope this finds you well", "Please find below", "do not hesitate to reach out"
  - greeting, a short thank-you ("thanks for contacting us"), then 3 to 5 short sentences that lead with the all-in price and the customer's priority (fastest, cheapest, or the date they asked about)
  - timestamps in natural wording ("28 Nov", "today until 15:34 UTC")
  - loosely built sentences, a little office and trade wording, now and then one small typing slip, never in a number, date, name, airline, route or reference
  - a call to action and an offer to phone at the end, then a sign-off
  - a footer between two divider lines: company, Theresienstraße 90, 80333 München (TUM main campus, building N1), phone number
  - variety from mail to mail
- **How:**
  - `talos-compose-mail` carries the rules and the chosen example with placeholders instead of values, so no example number can leak into a mail.
  - Variety comes from code, not from asking the model to vary: `composer.wording()` picks a greeting, opener, call to action, questions line, sign-off and slip at random for each mail and names them in the request. The same request also gives today's date, which the model needs for "today until ...".
  - The footer is appended in code (`MAIL_FOOTER`, `composer.with_footer`): always exact, no output tokens. The fallback templates get it too.
  - `composer.plain_text()` removes em dashes and bold from a draft before the fact check, in case the model ignores the rule.
  - The quote facts now include `customer_priority` (the extracted goal), so "the fastest option" is only written when the option was chosen as the fastest.
- **Scope:** mails to customers only. The booking request to the airline stays exact and structured, with ISO dates, no slips and no footer. The trucker request (ADR 38) is unchanged: a second postal code in a pickup request could be read as the pickup address.
- **Phone number:** `+49 89 99998 417` is from the range the Bundesnetzagentur reserves for media productions (089 99998 000 to 999). It reaches nobody. A real number goes into `MAIL_FOOTER`.
- **Not measured:** the five examples were written by hand, not by `gpt-6-luna`; no `EVALUATION_KEY` was available in the session. `scripts/compose_samples.py` is the check before the prompt gets the production label.


### ADR 49: The trucker is the cheapest one that picks up in time, for both goals

**Decided 2026-11-21.** Replaces the trucker part of ADR 08 (cheapest price for "cheapest", shortest pickup for "fastest").

- **Rule:** of the truckers that answered, take the cheapest one whose pickup time is at most `TRUCKER_PICKUP_CUTOFF_HOURS` (default 14). If none is that fast, take the cheapest of all. The goal no longer changes the trucker; it still chooses the airline.
- **Why:** `GET /v1/board/me` gives accuracy points per round, and 45 × (1 − 10 × deviation) reproduces them. Working backwards, the expected price in the scored pickup rounds is our air price plus one specific trucker offer, to the cent, and that trucker was neither always the cheapest nor always the fastest. Our reading: every trucker that gets the goods to the flight counts the same, and the cheapest of those is expected.
- **Evidence (16 scored rounds with pickup):** the rule picks the expected trucker in 13, is within 3 EUR in round 51, and is wrong in rounds 13 and 36 (both Kiel → HAM, where a 19 h trucker was expected, so the real cut-off was higher there). The old rules were right in 8.
- **Not known:** what sets the scorer's cut-off. 14 hours is fitted to past rounds, not documented anywhere. Rejected rounds give no points, so the four `too_low` rejections (14, 46, 49, 50) cannot be checked.
- **Booking by email when the portal refuses:** since 2026-11-21 ~20:05 UTC `POST /v1/portal/bookings` can answer `503 "Booking is not available for <airline>. Please book by email"`. The router then books by mail with the rate_id (same path as tariff and email airlines) instead of retrying the portal.


### ADR 50: One updatable store for reference data, changed by update mails

**Decided and built 2026-11-21.** Plan and what was built: [data-updates.md](data-updates.md). Changes ADR 06 (`replace_airline()` hook) and ADR 19 (`tariff_update` had no handler and sent no mail).

- **Context:** the carrier list, the pickup zones and the tariffs are read from files once, at startup. Carriers can mail us changes (new tariffs, single rates, new destinations or zones, new emails). These were tagged `tariff_update` and ignored, so the bot kept quoting from old data. Nobody knows yet what a real update mail looks like.
- **Decision (user):**
  - **One store, three separate tables:** `carriers`, `pickup_zones`, `tariff_lanes` in `router.db`, plus a `data_updates` log. They are not merged: they hold different kinds of data.
  - **Read on every lookup, not at startup.** `Reference` and `Tariffs` keep their methods and query the DB. A change is used by the very next quote. A read-only `GET /reference/...` shows the current data.
  - **The start-kit files are the seed.** They stay in git and fill the tables only when the tables are empty (a fresh checkout, a test DB, a replay).
  - **Use the existing classifier type `tariff_update`**, with a wider description (tariffs, single rates, carrier changes, zone changes). No new category.
  - **Both shapes of mail:** a full dataset like the start kit (attachments in any start-kit format, read as for RFQs, ADR 42/43) and a short mail changing one thing.
  - **Two LLM calls on the mail:** one extracts typed changes, one **flags every change** (`ok` / `unclear` / `suspicious`, with a reason). Suspicious is kept lean: a price more than 4× (over +300 %) or less than ¼ of the old one (checked in code), an unclear removal, contradicting values, a one-shipment offer, or text aimed at the bot.
  - **Put semantics:** insert if missing, overwrite only the fields given if it exists, lists (`serves`) only grow, lanes the mail does not mention stay. Delete only when the mail says so; a removed carrier is deactivated, not deleted. If two lanes are valid for the same route on the same day, the latest `valid_from` wins, then the later mail. Relative changes ("+5 %") are calculated in code. A changed email keeps the old one as an alias, and a deactivated carrier can still be booked for quotes already sent.
  - **Checks in code, always:** an airline or trucker may only change its own data, the rate desk may change any carrier's data (matched by address). Values pass the same checks as `build_tariffs.py`; no zone may be left without a trucker.
  - **Apply what passes**, `unclear` included. Only `suspicious` changes and failed checks are held (`needs_review`); the rest of the mail is still applied in one transaction, at most once per mail.
  - **Prices are stored in EUR:** the rate in the mail, else the portal's `fx_eur_per_usd` for USD, else the change is held.
  - **Confirm to the sender:** one `update_confirmation` mail per update mail, naming what changed and saying if something is held. It is a template with the stored values, not an LLM mail (as every carrier mail, ADR 38).
  - **We never ask carriers for updates**; only incoming mails are handled.
  - **UI:** a "Tariff updates" tab lists every change, one row each, with before → after, status and flag (table `data_changes`, `GET /data-updates`). Update mails that come in before the handler exists are listed as "not handled yet" and applied once by a one-off script when it ships.
- **Why:** one place to look up and change reference data, no restart needed, no old data lost by accident, and a mail from outside never changes prices without a check and a record.
- **Cost:** two extra LLM calls per update mail (extract, flag), plus vision for scanned files. Update mails are rare, so this is small next to the RFQ flow.


### ADR 51: Price source per airline: portal, then email, then local tariff

**Decided and built 2026-11-21.** Plan: [data-updates.md, section 8a](data-updates.md#8a-which-price-counts-per-airline). Changes ADR 06 ("portal and local tariff DB are equal price sources").

- **Context:** today every airline has exactly one price source (15 portal, 10 tariff, 4 email-only), so the sources never compete. Once update mails can store tariffs (ADR 50), a portal or email-only airline can also have local lanes, and the same airline would appear twice with two prices.
- **Decision (user):** the portal is the most reliable source. Per airline, use the portal price if there is one, else the price from the airline's email, else the local tariff. Who is asked how does not change: portal airlines on the portal, email-only airlines by email, tariff airlines from our table.
- **How:** `pricing.by_source(options)` keeps one source per airline before `pick_best`. When the portal is down, every airline that is not tariff-only is asked by email (ADR 47); its local lanes are the fallback if it does not answer. Booking follows the source of the chosen price, as today.
- **Why:** live prices beat stored ones, and a stored tariff can still save a quote when the portal and the airline are silent.

### ADR 52: A local CRM over the router's data


- **Context:** the only view into the system was `/ui`, a list of conversations. Accounts, carriers, lanes, the mail stream and the configuration could only be read from the database.
- **Decision (user):** a browser interface for the whole system, run locally, as a CRM fitted to this use case: matte glass look, no gimmicks, real figures. It has to include all data, follow the live database, and show the routes on an interactive 3D globe.
- **How:**
  - `GET /crm` serves plain HTML, CSS and JavaScript from `app/crm_ui/`. No build step, no framework, no request to another host.
  - `app/insights.py` computes every figure from stored rows: shipments as deals, accounts, airlines, truckers, lanes, the attention list and the findings. `app/crm_api.py` exposes them under `/crm/api/`. The interface only formats.
  - Live updates: `MessageStore.change_token()` is one cheap query that changes whenever a mail, shipment, trucker answer, air option or composed mail changes. The page polls it every 3 s and reloads the snapshot only when it moved.
  - The dry run is readable on a fresh checkout: `app/replay_data.py` rebuilds `replay.db` from the committed `result.json` and the recorded mails.
  - The globe is drawn on a canvas with an orthographic projection of the sphere. Routes are great circles between airport coordinates (`app/airports.py`); the land outline is Natural Earth 1:110m, simplified (`land.js`).
- **What is deliberately not shown:**
  - In a dry run: timings (two clocks are mixed) and win rates (the recorded verdicts answered the old process's quotes).
  - Aircraft positions: there is no such data. A shipment in transit is placed along its route by its planned dates, and the page says so.
  - LLM cost: it is in Langfuse, not in the database.
- **Read-only:** the CRM never sends, books or calls an LLM, and the system page shows no keys. Notes, tasks and manual actions (requote, book by hand) are not part of this version.
- **Safety when only looking:** start the app with `MAILBOX_POLL_ENABLED=false`, otherwise the poller reads the mailbox and spends LLM calls.
- **Tests:** `tests/test_crm.py` checks the figures against the recorded replay (`data/replay/recorded-run.json`) (counts, money adding up to the cent, pricing flags, conversation order), the endpoints, the change token and that no secret reaches the system page.
- **Later additions:** an Analysis page (all metrics, a quote comparison per shipment), CSV export of every table, pipeline filters, formal wording. `crm.sh` / `crm.bat` start it; given the database file of a running router as second argument, it follows that router's live data.
- **File handles:** `MessageStore` now closes every connection after its block. Before, connections stayed open until garbage collection; with the page polling every 3 s the process ran out of file handles on macOS (limit 256).
- **Branch:** the CRM lives on `entwicklung`. It was merged into `main` and reverted there; merging `main` back into `entwicklung` keeps it, with reference data and tariffs now read from the database (ADR 50).

### ADR 53: Customer mails mirror the customer; no footer, no canned wording, no planted slips

- **Context:** the "most human email" award (+10, the jury picks up to three accepted mails per round with team names hidden) kept going to other teams. The style of ADR 48 worked against it in three ways: office phrases ("kindly note", "as per your request") read as boilerplate, the divider footer reads as an automatic signature and adds nothing when names are hidden, and a planted typing slip is a trick, not a voice. The customers' own mails, on the other hand, have distinct voices: "Cheers, Klaus", "have a lovely day", "Viele Gruesse / Many greetings", a formal "Dear Sir or Madam" signed with a job title.
- **Decision (user):** rewrite the prompt, remove the footer and the typos. This replaces the style rules of ADR 48; its fact rules, the 3 to 5 sentence limit and the natural dates stay.
- **How:**
  - The prompt tells the model to mirror the customer's register from the mail we reply to: greeting, formality, and their sign-off returned in kind. It starts with the answer instead of thanks, uses one concrete thing from their mail, writes no label lines, and ends a quote with one short question.
  - A formal greeting uses the full name as signed. Mr or Ms is never guessed.
  - One true remark shows the work: the quote facts now carry `airlines_compared`, so "I compared three airlines" is only written when it is so.
  - Removed from code: `MAIL_FOOTER` and `composer.with_footer`, the random wording slots (`composer.wording`) and the slip instruction. The mail goes out as the model wrote it, signed with name and company on two lines. The check for em dashes and bold stays.
  - Two examples in the prompt, one relaxed and one formal, with placeholders instead of values.
- **Not measured:** no evaluation key was available in the session, so no mail was generated with the new prompt. Read `scripts/compose_samples.py --label staging` before promoting it. Whether the jury rewards this is a judgment; there is no data on which mails it picked.
- **To go live:** the running router needs this code, and the prompt has to be pushed to Langfuse and promoted (`scripts/push_prompts.py`, then `--promote talos-compose-mail`).

### ADR 54: The CRM is a supervisor console: it shows what needs a person, and it may act

- **Context:** the CRM of ADR 52 showed everything the router stores, on eight pages, and nothing followed from reading it. The user's judgment: "a ton of info, but nothing actionable or insightful. If everything runs smoothly, why would you need it?" A system that works on its own needs a person in three situations only: a case it could not finish, a result that has to be checked, and a setting that is wrong.
- **Decision (user):** rebuild it around those three. It serves both the hackathon and a product demo, and it may act on the live system. The globe stays as it is. Simplify further.
- **Pages:**
  - **Now:** one sentence (how many cases need a decision), one line on whether the router is reading the mailbox, and the queue. Only cases a person can act on are counted: an estimated trucking price or an unsure classification is not one. In live data a question or a booking is listed after it has waited 2 minutes; before that it is work in progress. A stopped poller is the first case. When the queue is empty the page still links the two conclusions: how many decided quotes were declined, and whether something in the setup should change.
  - **Rounds:** one row per request with outcome, time to answer, the board's verdict and points where `GET /v1/board/me` can be read, and the reason of a loss in the customer's own words (the reply without its opening thanks). An unanswered question back to the customer is "waiting for customer", not a loss. A second tab shows the quote mails as the jury reads them, the accepted ones first.
  - **Lookup:** accounts, airlines, truckers, tariffs and mail as tabs behind the search box. The second table of the same requests (the old pipeline) and the pickup-zone table (the trucker table turned round) are gone; the ledger has the airline column and the outcome filter instead.
  - **System:** mode and poller, classifier accuracy. Configuration, inventory and prompts fold out.
  - **Status bar**, under the header on every page, in the manner of a status page: the overall state with the uptime, then strips of ticks. The mailbox strip has one tick per minute of the last half hour, from the poll results the poller now keeps in memory; the request, booking and mail strips have one tick per item, coloured by how it went. It is part of the answer to the 3-second version poll, so it costs no request of its own.
  - A shipment's page opens with **What happened**: the steps that led to its state, each with its reason, and what a later fact showed (an offer that arrived after an estimate went out). The actions of its open case are on the page. Request, offer and correspondence fold out.
  - Removed: Desk, Network and Analysis as pages, the key-figure tiles, the stepper, the offer-analysis card, the outbound and inbound mail charts.
- **Actions (`app/actions.py`):** retry a booking, send the customer a reminder, close a shipment, process a failed mail again, dismiss an item. Each goes through the workflow's own operation (`_book`, `_clarify`, `process_message`), so an action does nothing the router could not do itself.
  - They only run in the process whose poller is running. A second process that reads the same database (`crm.sh` with a database path) can dismiss and nothing else: it must not book or send behind the state machine's back.
  - They take `poller.work_lock`, which the poll cycle now holds for a whole cycle, so an action never runs between two steps of a shipment.
  - `SEND_ENABLED` still decides whether mail leaves and whether a booking is real. The page asks before every action and says which of the two will happen.
  - Dismissed items are stored by key in `crm_dismissed` and can be restored on the System page.
  - `retry_mail` reads the mail again from the mailbox and replaces the record written when the router gave up. If processing fails again the mail is stored as given up once more, so it is never lost. A reminder is refused once the customer was asked `MAX_CLARIFICATIONS` times: the workflow would close the shipment instead of asking.
- **No login, so the action endpoint is guarded (`crm_api._only_from_this_page`):** before the guard, a plain `text/plain` POST from any website open in the same browser was accepted (checked with curl). Now the body must be `application/json` (a foreign page cannot send that without a preflight, which this app never answers), a stated `Origin` must be the host the request was sent to, and that host must be `localhost` or listed in `CRM_ACTION_HOSTS` (a foreign domain pointed at this machine is refused). Whoever can open the page can act: the port belongs on localhost or behind the operator's own access control.
- **Quote mails are checked against the prompt's own rules** (`insights.mail_checks`): opens with thanks, stock phrase, label lines, raw dates, more than 5 sentences, markup, phone number or footer. These are pattern checks for mechanical breaches; whether a mail sounds like a person stays a reader's call. On the recorded run all 14 quotes break at least one rule; that run predates ADR 53.
- **The board (`app/board.py`):** the answer of `GET /v1/board/me` is documented only as free-form JSON and could not be read from the development sandbox, so it is taken apart tolerantly and the ledger works without it. If the board answers but no entry can be matched to a request, the ledger prints the field names of an entry, so the names in `board.normalise` can be corrected. Untested against the real endpoint.
- **Performance page (user):** which airlines and truckers were used most, and how often customers ask for what. `insights.scorecard` counts, per quote that went out, the airline chosen and the trucker used, next to how often each was priced or asked; and per request what it contained (pickup, priority, further questions, deadline, dangerous goods, special cargo, named carriers, insurance). Insurance is not extracted by the router, so it is a word search in the request mail (English and German); the recorded run contains none.
- **Withdrawn (user):** the Tuning page with its verdicts per setting, the record of LLM use (`llm_calls`, the listener in `StructuredLLM`), every figure about LLM cost, and on the Performance page the strip of key figures and the per-stage counts. LLM cost stays where ADR 52 left it, in Langfuse.
- **Frame (user):** the product is pitched as the better forwarder against the freight giants, and the console stays a working tool, not a marketing page. So it carries no slogans and no competitor column: the figures on the Performance page are the evidence, in the tool's own numbers. System sits below a rule in the navigation as the router's own setup. Figures about other forwarders are not in the product: the published studies found could not be opened from the development sandbox, and an unverified number does not go on screen.
- **Look (user):** harder. Square corners, opaque panels with a ruled edge instead of blurred glass and shadows, no coloured background fields and no grain, ruled tables with small capital headings. The globe is unchanged.
- **Review:** an independent read of the console as its operator found the first version only half coherent (cases counted that nobody could act on, a loss column that restated the outcome, nine cards per shipment, no action on the page a case opens). The points above marked as removed or changed come from it. The actions and their edges are covered by `tests/test_console.py`.
- **Notifications:** with the browser's permission a new case in the queue is announced, and the tab title carries the count. Nobody has to watch a page that is empty when things work.
- **Honest limits:** in a dry run the action buttons are off and timings and win rates stay hidden (ADR 52).

