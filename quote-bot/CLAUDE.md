# 5U AI Challenge – START Hack Munich 2026 – team TALOS

Context handoff, state **2026-11-21 17:15 (Europe/Berlin)**. Read fully before changing code.
Chronological log of the whole working session: `docs/261121_session_log_01.md`.

## Quick start (any computer)
```bash
cd quote-bot            # this folder (repo TALOS, branch quote-bot)
./run.sh                # first run: creates .venv + installs requirements, then: python -m quote_bot --reply
./run.sh --redo-bookings   # optional: also re-run booking/tracking mails an older build skipped
```
Windows: `run.bat`. Needs Python ≥ 3.11 and internet access to `api.start-hack.5u.ai`.
`.env` (team API key) and `quote_bot.sqlite3` (CRM state: since_id, quotes, bookings) are committed on purpose
(private repo, user decision) so the bot continues where it stopped.
**Only one bot may run at a time** (two bots = double replies). Stop the old one (`pkill -f quote_bot`) before
starting on another computer; push the DB again if the old one kept running after this snapshot.
Startup log line `quote_bot build <hash>` shows which code runs (hash of `quote_bot/*.py`).

## Challenge (from `5U AI challenge deep dive.pptx.pdf`)
- Be a digital air freight forwarder. One mailbox per team (`talos@hackloop.mail`), new customer RFQ ~every 6 min, all night. Every hour something changes unannounced.
- Stages: **Quote** (RFQ → check rates → ask 2–5 carriers → quote within 6 min) → **Book** (customer accepts → book with rate_id → confirm) → **Track** (customer asks status → answer from own CRM).
- Scoring per accepted quote, max 100: **accuracy 45** (closest to best price), **LLM cost 40** (low spend per quote), **time 15** (faster = more). Only accepted quotes score: within 5 % of best price, **first reply only**, within 6 min.
- Required components: router, RFQ agent, tracking agent, tariff ingestion, pricing module, CRM, sessions UI with streaming. Judging: pitch, UX, tech depth, leaderboard.
- LLM only via the 5U gateway (OpenAI models, cost counted). Do not route LLM calls elsewhere.

## API – https://api.start-hack.5u.ai (docs /docs, spec `../openapi.json` on branch main)
Auth: header `X-API-Key: <team key>` on every call.
| Endpoint | Notes |
|---|---|
| GET /v1/me | `{"team":"talos","display_name":"TALOS","mailbox":"talos@hackloop.mail"}` |
| GET /v1/mailbox/messages?since_id&limit | `{"messages":[...],"next_since_id":N}`; oldest first; max limit 200. Items: id, from, to, subject, body, received_at, `in_reply_to`, `references` (thread ids first→parent), `attachments` [{id, filename, content_type, size}] |
| GET /v1/mailbox/messages/{id} | id must be int |
| GET /v1/mailbox/messages/{id}/attachments/{attachment_id} | raw file bytes |
| POST /v1/mailbox/messages | `{to, subject, body, in_reply_to?, attachments?[{filename, content_type, content_base64}]}` → 201. Allowed recipients: customers, start-kit carriers, `ratedesk@hackloop.mail`, `tracking@hackloop.mail` (others 422). 60 mails/min. **A quote counts only as a reply in a thread a customer started, and only the first quote per thread counts.** |
| GET /v1/portal/ports | `{"lanes":[{origin,destination,airlines:[{code,name}]}]}` |
| GET /v1/portal/brackets?airline=CODE | `{airline, airline_code, breaks_kg:[...]}` |
| GET /v1/portal/rates?origin&destination&airline&weight_kg&volume_cbm | `{rate_id, airline, airline_code, origin, destination, weight_kg, chargeable_weight_kg, total (freight+fuel+security), currency, transit_days, valid_until (~15 min), dataset_version, fx_eur_per_usd?}`; rate-limited per minute (429 + Retry-After); **503 = portal down, message says where to get the rate (rate desk)** |
| POST /v1/portal/bookings | `{rate_id, team_reference}` → 201 booked `{booking_reference BK-xxxxxxxx, team_reference, rate_id, airline, airline_code, price, currency, status "booked"}`; 200 = same rate_id+ref again; 404 unknown; 409 booked under another ref; 410 expired → fetch fresh rate; **503 = booking system down → book by e-mail with the airline**. Afterwards the airline mails **"Booking <ref> confirmed"**. No status call: keep bookings in own CRM. |
| /v1/gateway/chat/completions, /responses, /embeddings, /models | OpenAI-compatible; gpt-4.1-nano NOT offered → bot picks gpt-4o-mini |
| GET /v1/board | public leaderboard, all bookings, last rounds; cost points added hourly |
| GET /v1/board/me | per request: verdict (accepted/rejected/pending/open/no answer/voided), **reason code**, points per kind, booked y/n; own bookings. Best diagnostic tool. |

## Start kit + data findings (`start-kit/`, `data/`)
- `carrier-list.csv` (name,kind,email,serves): 29 airlines (serves = IATA destinations) + 22 truckers (serves = 2-digit German postcode zones). 4 niche airlines (Zephyr Steppe, Himalaya Bridge, Lagoon Isles, Pampero) are the only ones for ASU, KTM, PPT, TNR, ULN.
- `pickup_zones.csv`: 32 zones → CGN/DUS/FRA/HAM/MUC/STR; every zone has 2–5 truckers. Truckers bid by e-mail; they say no booking is needed ("we collect once your air freight booking is confirmed").
- 10 tariff files (pdf table, pdf blocks, scanned jpg with decimal commas, 4 xlsx layouts, docx, txt) of airlines **not on the portal**: Cobalt, Danube, Elbflug, Helix, Kestrel, Orion, Quetzal, Vega, Wayzata, Yellowtail. Normalized: `data/261121_tariffs_normalized_01.csv` (158 lanes, 107 distinct, 47 with ≥2 carriers; rebuilt by `data/ingest_startkit.py`, Danube typed from the scan). Valid 2026-11-01..30.
- Tariff rule (all sheets): chargeable = max(kg, cbm × 167); freight = max(minimum, rate(break) × billed kg); total = freight + (fuel + security) × billed kg; billed kg = chargeable or a higher break if cheaper.
- Analysis over all lanes × 1–2500 kg: higher break cheaper in 12.8 %; surcharges on actual instead of billed kg picks wrong in 7.8 %; checking only the **next** break never misses; minimum binds ~1 %. Carrier spread: 2nd-best within 5 % of best in only 12 % of cases (median gap 16 %) → price every carrier.
- Quirks: 13 flat breaks (same rate two breaks), 28/158 minimums = integer × 1.06 (meaning unknown), no rate inversions.

## Code: package `quote_bot/` (Python ≥ 3.11; requests, openai, python-dotenv, pypdf, openpyxl)
```bash
./run.sh                               # = .venv + python -m quote_bot --reply
python -m quote_bot --reply            # production loop (poll 3 s)
python -m quote_bot --reply --redo-bookings   # + re-run skipped booking/tracking mails first
python -m quote_bot --message-id N --reply    # (re)process one mail (no instance lock)
python -m quote_bot --dump             # raw API responses
python tests/run_e2e.py                # offline end-to-end vs tests/mock_api.py (all flows below)
python tests/test_extract.py           # regex tests
```
Modules:
- `config.py` – env/.env (strips invisible Unicode; the key had U+202F/U+2060 from copy-paste).
- `api.py` – FiveUClient: thread-local sessions, retries 429/5xx for GET, POST only 429; `book()` retries 429/502/504 but **not 503**; `attachment()`; `send_message` logs to mail_logs + `on_sent` hook (→ table `sent`).
- `extract.py` – Mail normalisation, quote stripping, rules router (order): `booking_confirmed` ("Booking BK-… confirmed") → carrier mail (`rate_sheet` if attachment/tariff text, else `carrier_quote`) → reply with refusal (`declined`, never booked) → reply with acceptance (`booking`) → thank-you/ack (`other`, no reply) → strong tracking phrase (`tracking`) → RFQ regex (lane, weight kg/lb, volume cbm/cft/dims) → LLM fallback (JSON, one call). `load_airline_emails`, LLM helpers `transcribe_image`, `extract_tariff`.
- `pricing.py` – LaneMap (/ports, 5 min + forced on miss), BracketCache, Pricer: portal airlines in parallel at actual weight + next break; **plus rate-sheet airlines** (TariffStore) not offered on the portal for that lane (rate_id `TAR-…`, `source: tariff`); cheapest (mixed currencies via fx) or fastest. `grand_total()` = air + pickup.
- `tariffs.py` – rule parser for rate sheets (row tables, origin-grouped tables, block layout, inline or separate surcharges, decimal comma, `-45/+45`, `from N kg`, `N kg+` headers; 158/158 start-kit lanes exact), LLM fallback, `price_lane` (rule above), `TariffStore` (SQLite `tariffs`, newest per carrier+lane; seeded from the normalized CSV at startup).
- `attachments.py` – download + text: txt/csv, pdf (pdfplumber or pypdf), xlsx (openpyxl), docx (zip XML), images via gateway vision.
- `dg.py` – dangerous-goods evidence (UN number, class, packing group, "dangerous goods", flammable, lithium batteries, Gefahrgut …; negations like "non-dangerous" removed), document detection (DGD/MSDS), DG request template.
- `pickup.py` – postcode → zone → airport; asks up to 5 truckers; bids (thread ids or sender+zip; price regex, LLM if ambiguous); finalize when all answered or `PICKUP_WAIT_S`. Pending requests in memory only.
- `reply.py` – quote, booking confirmation, tracking texts (status: booked/confirmed/requested; quoted-not-booked case).
- `writer.py` – `HUMANIZE=1` (default): LLM rewrites outgoing mails, facts verified, else template (~300–500 tokens/mail).
- `db.py` – SQLite: `state` (since_id), `messages` (category, method, status, flags, llm_tokens), `quotes`, `sent`, `bookings`, `tariffs`.
- `maillog.py` – `mail_logs/YYMMDD_mail_incoming.txt` / `_outgoing.txt`.
- `main.py` – CLI, single-instance lock (`<db>.lock`, loop mode), build banner, loop (sequential, oldest first). Handling:
  - **rfq** → if reply in a thread we already quoted: no second quote. Read attachments (fill missing weight/volume). **Dangerous goods found and no DG documents attached → send DG document request (template, no LLM), status `awaiting_dg_docs`; the customer's reply in that thread re-runs the original RFQ and the quote goes in reply to the documents mail.** Else pickup (truckers) + pricing → quote reply.
  - **booking** → find RFQ (thread ids, else sender+subject) → per lot `TALOS-<rfq>-<lot>`: book rate_id; 404/410 → fresh rate same airline (else cheapest) → book; 409 → recorded as booked; 503 or rate-sheet airline → booking e-mail to the airline (status `requested`). Confirmation to customer. Already booked → **no new mail** (stopped a thank-you loop).
  - **booking_confirmed** → booking status `confirmed` (+ booking reference for e-mail bookings); unknown → rebuilt from the quote whose rate_id is in the mail.
  - **tracking** → bookings from CRM (reference in text, else sender + lane named, else latest) → status reply; none but a quote → "quoted, not booked yet".
  - **rate_sheet** → body + attachments parsed → `tariffs`.
  - **declined** → saved, never booked. **other** → saved, no reply. **carrier_quote** → pickup bid.
  - `--redo-bookings` → booking/tracking mails with status done/unmatched/booking_failed/error and airline confirmations, confirmations first.

Env (`.env`, see `.env.example`): FIVEU_API_KEY, FIVEU_BASE_URL, LLM_MODEL, LLM_MAX_CHARS, POLL_INTERVAL_S=3, DB_PATH, PORTS_REFRESH_S, BRACKETS_TTL_S, QUOTE_WINDOW_S=360, SAFETY_MARGIN_S=60, RATE_WORKERS, HTTP_TIMEOUT_S, AUTO_REPLY, REPLY_SIGNATURE, CARRIER_LIST, PICKUP_ZONES, PICKUP_WAIT_S=150, PICKUP_MAX_TRUCKERS=5, MAIL_LOG_DIR, HUMANIZE=1, **TARIFF_PRICING=1** (0 = portal only), **TARIFF_SEED_CSV** (default `data/261121_tariffs_normalized_01.csv`, empty = no seed).

## Tests (all pass, 2026-11-21)
- `tests/run_e2e.py` with `tests/mock_api.py` (mock behaves like the live API: 201/200/404/409/410/503 bookings, airline confirmation mails, `references`, attachments): RFQ+quote, LLM RFQ, pickup with 3 truckers, rate expired → re-price → book, portal booking down → e-mail booking → confirmation, rate-sheet PDF mail → tariff quote (Orion HAM-DXB 812.50 vs portal 875.00) → e-mail booking → confirmed, DG RFQ (attachment UN 1133) → DG request → documents → quote, refusal → not booked, thank-you → ignored, tracking replies, `--redo-bookings`.
- Tariff parser vs normalized CSV: 158/158 lanes exact (pdf via pypdf).
- Live mails from 2026-11-21 (thank-yous, refusal, "please go ahead and book it", trucker bids, airline confirmation) all classified by rules, 0 tokens.

## Decisions taken with the user
Airlines from /ports · regex first, LLM fallback · break-point check next break only · loop 3 s · SQLite · best effort + flags · requests + openai SDK · margin 0 (cheapest total) · currency as returned · sequential mails · Python ≥ 3.11 · small package · code delivered into the folder, fast iterations · **teammates' `router/` (branch main) NOT integrated**: it calls an LLM on every mail via api.openai.com (outside the 5U gateway → bypasses cost scoring; through the gateway it would raise cost), and its categories lack booking/confirmation/carrier bid · **tariff pricing on by default** (start-kit + mailed rate sheets; assumption: scorer's best price includes these airlines; `TARIFF_PRICING=0` to switch off) · no pickup order mails to truckers · no second quote in an already quoted thread · `.env` + DB snapshot committed (private repo) · HUMANIZE still 1 (biggest remaining LLM cost; consider 0).

## Live history (2026-11-21, UTC+1)
- 16:00 TALOS had exactly 60 leaderboard points; no bookings on the board.
- Until ~16:50 a stale bot (pre-booking code) ran: booking mails stored as `done`, nothing booked. After restart with `--redo-bookings` (build 3dc4ce14) old acceptances were booked (10 bookings in the DB snapshot).
- BK-8RCF17DZ ("confirmed" mail 16:36) was booked by someone else (manual or another tool) — make sure only one system answers this mailbox.
- Thank-you loop: thank-you mails were classified `booking` by the LLM → re-confirmation → next thank-you (~7× × 350 tokens). Fixed (ack rule + no re-confirmation).
- DG edge case: RFQ 2267 (weidner-spielwaren, GRU, 715 kg adhesives, pickup 61391) said "non-dangerous" but the attached packing specification showed dangerous goods; we quoted; customer: "you quoted without asking us for the dangerous goods documents, so we will not go ahead". "not go ahead" matched the booking rule → **BK-ZM78K6DQ (Juniper Cargo Lines, 2685.35 EUR) booked by mistake**; no cancel API (user decides whether to e-mail Juniper). Fixed (attachments, DG flow, refusal rule).
- 17:10 running process was still build 3dc4ce14; build 4f99c1ab (DG, refusals, ack, rate sheets, tariff pricing) delivered but not yet started → restart needed.

## Open TODOs (priority)
1. Restart on build 4f99c1ab+ and watch `/v1/board/me` reason codes (DG requests accepted? tariff quotes accepted/booked?).
2. If tariff-airline quotes get rejected as too cheap → `TARIFF_PRICING=0`.
3. LLM cost: `HUMANIZE=0`; keep rules-first.
4. Portal rates 503 → ask `ratedesk@hackloop.mail` (not implemented).
5. Persist pending pickup requests in SQLite (lost on restart).
6. Hourly changes: watch `dataset_version`, lanes, brackets, fx.
7. Sessions UI with streaming (required deliverable; not started). Data: QUOTE_JSON lines, SQLite, mail_logs.
8. ASK 2–5 carriers by e-mail for lanes without portal/tariff (niche airlines ASU/KTM/PPT/TNR/ULN).
9. `mail_logs/261121_*`: entries with ids 1–11 and `trucks.test` addresses are from an offline mock test, not live.

## User environment
macOS, zsh (`#` comments interactive only with `setopt interactivecomments`), miniconda Python 3.13. Local folder `/Users/Shared/3200 Hackathons/5U` (= this `quote-bot/` folder). Preferences: short answers, no filler, versioned filenames `YYMMDD_name_01`, reply in the language of the question.
