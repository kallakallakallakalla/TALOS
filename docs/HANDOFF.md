# Handoff: implementing the TALOS quote workflow

For: the implementing agent (Fable). Written 2026-11-21 at the end of the planning session. **The plan is reviewed and agreed. Implement it as written. If something in the plan turns out to be wrong, stop and ask the user. Don't redesign.**

> **Update, 2026-11-21 evening:** steps 0–8 of the plan are implemented on branch `quote-workflow` (one commit per step). Read [implementation.md](implementation.md) first: it has the current state, the deviations from the plan, the measured results and what is still open (steps 9 and 10). The sections below describe the state *before* the implementation and are kept for the rules, traps and backlog.

## Read in this order

1. **[workflow.md](workflow.md): the implementation plan.** It covers architecture, flow, state machine, loop guards, data model, config, module specs, build order (steps 0–10) and cutover.
2. [decisions.md](decisions.md): every decision and why. Use it when the plan seems ambiguous.
3. [prompts.md](prompts.md): v1 drafts of all system prompts.
4. [platform-api.md](platform-api.md): every endpoint, limit and error code of the hackathon API.
5. [data.md](data.md): the start kit (carrier list, pickup zones, 10 tariff files) and the pricing formula.
6. [mailbox-analysis.md](mailbox-analysis.md) and [scoring.md](scoring.md): what the mail and the scoring look like in practice.
7. [../vocab.md](../vocab.md): freight vocabulary.

The user also has a review page with the same plan and diagrams: https://claude.ai/artifact/G9TpdMjsTuCd7cVCxjDjne (it's private to them).

## The goal in one paragraph

Simulated customers email `talos@hackloop.mail` asking for air-freight quotes. A new round arrives about every 6 minutes, and each round closes 6 minutes after it is sent. We answer each request with one all-in EUR quote:

- the **trucker pickup**, if the customer wants us to collect: ask the truckers serving the postal zone by mail
- **air freight**: the portal API plus the local tariff DB

When the customer accepts, we **book**. Score per accepted mail: 45 price accuracy, 40 LLM cost, 15 response time. On top of that: +10 for the most human email, +10 for a correct tracking answer on a booked shipment.

## Current state of the repo and the system

| Item | State |
|---|---|
| `router/` | Working MVP. It polls every 10 s, classifies every mail with `gpt-6-luna` (6 old categories), and stores the result in `router/data/router.db`. **It never sends.** Tests: `cd router && .venv/bin/pytest` |
| Running process | At the end of the session a `uvicorn app.main:app --port 8000` from `router/` was running locally (classify only). Check with `ps aux \| grep uvicorn` before starting another one. |
| `router/.env` | Holds `HACKLOOP_API_KEY`, `DECISION_API_KEY` (team key for the LLM gateway), `DECISION_API_BASE_URL=https://api.start-hack.5u.ai/v1/gateway`, `DECISION_MODEL=gpt-6-luna`, the Langfuse keys and host, and `OPENAI_API_KEY`. **Never print or commit the values.** |
| Python | 3.14 venv in `router/.venv`; Langfuse SDK **5.2.0**; OpenAI calls go through `langfuse.openai.AsyncOpenAI` |
| Langfuse | `LANGFUSE_HOST` / `LANGFUSE_BASE_URL` in `router/.env` decide which instance is used. A local Docker stack is in `langfuse/` (`docker compose up -d`; UI on `localhost:3000`; credentials in `langfuse/credentials.md`, git-ignored). |
| `data/start-kit/` | Committed (`18878bb`): carrier list, pickup zones, 10 tariff files (txt, xlsx, docx, pdf, jpg) |
| `openapi.json` | Refreshed from the live server, including the webhook endpoints; reformatted. Not committed yet. |
| `docs/` | This documentation. Not committed yet. |
| `state.pdf` | The user's hand-drawn first flow sketch (untracked). The plan supersedes it. |
| Old process | **Still running on another computer with the same team key.** It sends the "Air Freight Desk" quotes and books. See "Rules" below. |

## Rules for the implementation

1. **Never send mail or book from this machine until the user approves cutover (step 10).** Keep `SEND_ENABLED=false`. The old process is live with the same key; a second sender means duplicate quotes, and only the first quote in a thread counts.
2. **No manual bookings or test mails to customers or carriers** (ADR 24). Use mocks (`httpx.MockTransport`, like the existing tests) and recorded mails.
3. **The portal rate limit (10 rate reads per minute) is shared with the old process,** because it's per team key. During development and dry runs, mock `/portal/rates` or use it sparingly, or you'll starve the live process. `ports` and `brackets` don't count against the limit.
4. One commit and one test per build step, exactly in the order of workflow.md section 11. Each step has a "done when"; don't move on until it holds. The existing tests must stay green.
5. Reuse the existing patterns:
   - the clients take `settings` plus an optional `http_client`
   - LLM calls copy `DecisionClassifier`: `@observe`, `chat.completions.parse`, `langfuse_prompt=`
   - storage methods live on `MessageStore`
   - dependencies are wired in `main.lifespan`
6. The Langfuse 5.2 API for trace attributes (`session_id`, `user_id`, `tags`) must be checked against the installed SDK in step 0. Don't guess it.
7. Every LLM call uses `gpt-6-luna` (config: `decision_model`, `extract_model`, `compose_model`).
8. **No regex on mail text.** Regex or plain parsing is only allowed on API JSON and CSV data (ADR 12).

## Facts and traps worth knowing (all verified in the session)

**Mailbox**
- Message ids are **global across teams** (sparse).
- `GET /v1/mailbox/messages/{id}` also returns mail **we** sent. That's how to read our own quotes.
- `references` lists the thread from its first mail down to the parent, so `references[0]` is the customer's first mail, which is the shipment id.

**Portal**
- Airline lookup needs the code or the exact full name: `polaris` → 404, `PL` or `Polaris Air Logistics` → OK.
- A `rate_id` is valid for about 15 min. `total` = freight + fuel + security, airport to airport; trucking is not included.
- `chargeable_weight_kg = max(kg, cbm × 167)`.
- The portal prices only the weight you send. For the anti-weight-break check, also query the next break weight.

**Bookings**
- Confirmations arrive **unthreaded** from the airline ("Booking BK-… confirmed", "Your reference: …").
- The plan uses `team_reference = str(shipment.id)`.

**Truckers**
- They answer about 30 s after our request.
- Each reply has a price, "Pickup time: N hours", a validity of about 15 min, and a `TRK-…` ref.
- No booking is needed with truckers.

**Customers**
- "That works for us, please go ahead and book it" means accept.
- "That seems too low to be right…" means rejected as too low.

**Tariff files**
- comma decimals
- Excel date serials (46327 = 2026-11-01)
- `D+4` / "2 days" transit formats
- break sets differ per airline (250 vs 300; some have no 2000 break)
- Danube Wings is two scanned, rotated JPGs

**LLM gateway**
- It has `gpt-6-luna` and `gpt-6.1-terra`, but **no `gpt-6-terra`**.

**Rate limit**
- The **10/min limit applies only to `/portal/rates`**, not to the mailbox.

**Existing bug**
- The poller retries a failing mail forever (ADR 20, fixed in step 0).

## Still open (ask the user)

Both points from the planning session are closed: the signature is "Alex, TALOS Air Freight" (ADR 25), and `docs/` plus `openapi.json` are committed on `quote-workflow` (ADR 26). What is open now is listed in [implementation.md](implementation.md).

## What happened in the session (timeline, CET)

| Time | Event |
|---|---|
| ~16:20 | Explored the repo, the API (OpenAPI + live calls), the start kit and the 24 mails of rounds 1–6. Wrote `docs/`. |
| 16:36 | With the user's approval, booked round 6 by hand: `BK-8RCF17DZ` (FQ, DUS→KUL, €3949.95, team_reference "1470"). The airline confirmed at 16:36:41. The user then ruled: no more manual bookings (ADR 24). |
| ~16:40 | Found that the live `openapi.json` had new webhook endpoints. Refreshed the file. |
| ~16:45 | Decisions 01–05, 08, 09, 22, 23. Start kit committed. First plan and review page. |
| ~16:55 | Truckers per zone confirmed; loops capped at 3; plan rebuilt on the existing router architecture. |
| ~17:00 | Fallback trucker price; default cheapest; local tariffs booked by email. |
| ~17:05 | No regex on mail; LLM reads and writes all mail; prompts in Langfuse; tracing. Terra proposed. |
| ~17:10 | Switched to `gpt-6-luna` everywhere; classifier kept as the first step with focused extractors. |
| ~17:20 | Decision records and this handoff written. |

## Later backlog: observed after the plan was frozen (NOT part of this plan)

The user said these will be implemented later; **don't build them now.** They are recorded here so nothing is lost. All of them were seen in the mailbox between 16:36 and 17:18 CET (rounds 7–13).

1. **Reply loop in the old process.** It answers every customer "thanks for confirming" with another confirmation, and the customer answers again. One thread (Hofmann, FRA→DOH) grew to about 45 mails at one every 15 s. The plan's rule "`other` never triggers a mail" covers this, but a classifier category such as `customer_ack` (no reply, ever) would make it explicit.
2. **Dangerous-goods rejections** (`dg_docs_missing`, rounds 10 and 11).
   - The mail text said "general cargo, non-dangerous", but the commodity (industrial adhesives, maintenance spray cans) and an attachment (`PL_53839.xlsx`, `packing-spec_33955.pdf`) pointed to dangerous goods.
   - The customers expected us to **ask for the dangerous-goods documents** before quoting.
   - This needs attachment reading and a dangerous-goods check before quoting.
3. **Multi-lot requests.**
   - Two or three "Lots" are shipped as one consignment under a single air waybill, so weights and volumes are summed and the chargeable weight is computed once.
   - Weights may be given per piece, and volumes as per-piece dimensions (e.g. "4 cartons, each 48 kg, each 110 x 110 x 52 cm").
   - Gross and net weight are both given; use gross.
   - Round 9 (Fulda → LOS, 3 lots) got **no answer** from the old process: it asked the truckers with "Weight: n/a", and a trucker replied asking for the weight.
4. **Tracking questions** arrive from `tracking@hackloop.mail`, unthreaded, on behalf of a customer. They name the customer, lane, weight and ready date ("Kessler Feinmechanik GmbH, DUS to KUL, 1549 kg … Where is it now?"). The shipment has to be matched by those facts, and the answer comes from our booking records. This is worth +10 per booked shipment.
5. **"Import" wording.** Some requests say "our buyer in Doha is importing … we deliver to FRA". It's still an export from Germany; the extractor must not be confused by the wording.
6. **Late bookings didn't count.**
   - The old process booked rounds 3, 4, 5, 7 and 8 at about 16:54, long after the customers accepted. `board/me` shows `booked: false` for those rounds.
   - Bookings made within about a minute of acceptance (rounds 6, 12, 13) show `booked: true`.
   - Book immediately on acceptance.
7. **Rejected quotes were booked anyway.** The old process booked round 1 (rejected, too low) and rounds 10 and 11 (rejected, dangerous goods). Never book a rejected shipment.
8. **Old process extras.**
   - Its `team_reference` format is `TALOS-<request id>-1`.
   - It sends "Pickup order" mails to truckers after booking, and they answer "nothing to book with us". That mail is wasted.
9. **Board at 17:19 CET.** TALOS was 11th of 14 with 611.78 points: 8 of 12 answered rounds accepted, 11 bookings, 1 "human" award. 13 rounds sent so far.
