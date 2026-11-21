# Architecture & decision records

Every decision taken in the planning session of 2026-11-21 (16:00–17:20 CET). "User" means the decision came from the project owner; "proposed" means Claude proposed it and the user accepted it or didn't object. The full plan these decisions feed into is [workflow.md](workflow.md).

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
