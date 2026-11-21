# 5U AI Challenge – START Hack Munich 2026 – team TALOS

Context handoff (state 2026-11-21 16:30). Read fully before changing code.

## Challenge (from `5U AI challenge deep dive.pptx.pdf`)
- Be a digital air freight forwarder. One mailbox per team (`talos@hackloop.mail`), new customer RFQ ~every 6 min, all night. Every hour something changes unannounced.
- Stages: **Quote** (RFQ → check rates → ask 2–5 carriers → quote within 6 min) → **Book** (customer accepts → book with rate_id → confirm) → **Track** (customer asks status → answer from own CRM).
- Scoring per accepted quote, max 100: **accuracy 45** (closest to best price), **LLM cost 40** (low spend per quote), **time 15** (faster = more). Only accepted quotes score: within 5 % of best price, **first reply only**, within 6 min.
- Required components: router, RFQ agent, tracking agent, tariff ingestion, pricing module, CRM, sessions UI with streaming. Judging: pitch, UX, tech depth, leaderboard.
- LLM only via 5U gateway (OpenAI models, cost counted).

## API – https://api.start-hack.5u.ai (docs /docs, spec /openapi.json)
Auth: header `X-API-Key: <team key>` on every call.
| Endpoint | Notes |
|---|---|
| GET /v1/me | `{"team":"talos","display_name":"TALOS","mailbox":"talos@hackloop.mail"}` |
| GET /v1/mailbox/messages?since_id&limit | `{"messages":[...],"next_since_id":N}`; ids are global (not per team), oldest first |
| GET /v1/mailbox/messages/{id} | id must be int (422 = validation error otherwise) |
| POST /v1/mailbox/messages | `{to, subject, body, in_reply_to?, attachments?}` → 201, returns full message incl. `id`; 60 mails/min |
| GET /v1/portal/ports | `{"lanes":[{origin,destination,airlines:[{code,name}]}]}`; origins CGN, DUS, FRA, HAM, MUC, STR … |
| GET /v1/portal/brackets?airline=CODE | `{airline, airline_code, breaks_kg:[0,45,100,300,500,1000]}` |
| GET /v1/portal/rates?origin&destination&airline&weight_kg&volume_cbm | `{rate_id, airline, airline_code, origin, destination, weight_kg, chargeable_weight_kg, total, currency, transit_days, valid_until (~15 min), dataset_version, fx_eur_per_usd?}`; 429 + Retry-After, X-RateLimit-* headers |
| POST /v1/portal/bookings | `{rate_id, team_reference}` → BookingOut `{booking_reference, team_reference, rate_id, airline, airline_code, price, currency, status}`; 404 unknown, 409 booked by other ref, 410 expired, 200 = idempotent retry |
| /v1/gateway/chat/completions, /responses, /embeddings, /models | OpenAI-compatible; models incl. gpt-4o-mini, gpt-4.1, gpt-4.1-mini, gpt-5, gpt-5.1, gpt-5.2 … (gpt-4.1-nano NOT listed → bot picks gpt-4o-mini) |
| GET /v1/board, /v1/board/me | leaderboard; /board/me with X-API-Key = own breakdown |

Chargeable weight = max(actual, cbm × 167). Rates are computed by the portal.

## Start kit (`start-kit/`, `data/`)
- `carrier-list.csv` (name,kind,email,serves): 29 airlines (serves = IATA destinations) + ~23 truckers (serves = 2-digit German postcode zones).
- `pickup_zones.csv` (zone,area,airport): e.g. 50/51/53 → CGN, 40–47 → DUS, 60–65 → FRA, 80–93 → MUC, 20–28 → HAM, 70–73 → STR.
- 10 tariff files (pdf/xlsx/docx/txt/jpg) of airlines NOT in the portal (Cobalt, Danube, Elbflug, Helix, Kestrel, Orion, Quetzal, Vega, Wayzata, Yellowtail). Teammate normalized them: `data/ingest_startkit.py` → `data/261121_tariffs_normalized_01.csv` (carrier,origin,destination,transit_days,minimum_eur,fuel_eur_kg,security_eur_kg,break_kg,rate_eur_kg). Tariff rule: freight = max(minimum, rate(break) × chargeable), surcharges fuel+security per billed kg. **Not used by the bot yet.**

## Code: package `quote_bot/` (Python 3.11+, requests, openai, python-dotenv)
Run from the 5U folder, venv `.venv`:
```bash
source .venv/bin/activate
python -m quote_bot --dump            # raw API responses
python -m quote_bot --reply           # production loop (poll 3 s, quote + reply, book, track)
python -m quote_bot --reply --skip-backlog
python -m quote_bot --message-id N --reply   # (re)process one mail
python -m quote_bot --once --no-llm   # one pass, rules only, no sending without --reply
python tests/run_e2e.py               # offline e2e vs tests/mock_api.py (quote→pickup→book→track)
python tests/test_extract.py          # regex tests
```
Modules:
- `config.py` – env/.env (strips invisible Unicode from values; the key had U+202F/U+2060 from copy-paste).
- `api.py` – FiveUClient, thread-local sessions, retries 429/5xx for GET (POST only 429, bookings idempotent → full retry), shared 429 pause, deadline-aware. `send_message` logs to mail_logs + calls `on_sent` hook. `book()`.
- `extract.py` – Mail normalisation (defensive field names), quote stripping, regex router/extractor (IATA lane, weight kg/lb, volume cbm/cft/dims, pieces), carrier detection (carrier-list emails or subject "pickup quote") → `carrier_quote`, LLM fallback (JSON mode, one call: category + shipments), `merge_rules_into_llm` (regex numbers override LLM), `extract_price`, `complete_text`.
- `pricing.py` – LaneMap (/ports, refresh 5 min + forced on miss), BracketCache, Pricer: all airlines in parallel at actual weight + next weight break (break-point check), cheapest total (mixed currencies compared in EUR via fx), `priority="fastest"` → min transit then cheapest. Airline param = code, auto-switch to name on 404/422. `grand_total()` = air + pickup.
- `pickup.py` – postcode (5 digits, zone in pickup_zones) → origin airport; asks up to 5 truckers serving the zone (`Pickup quote <zip> <city> to <airport> on <date>`), collects bids (match by in_reply_to/references, else sender+zip), price regex (prefers total/all-in, LLM if ambiguous), finalize when all answered or `PICKUP_WAIT_S` (150 s). Pending requests in memory only; finished kept to flag late bids. `is_fastest()` (fastest/urgent/express/asap/eilig…).
- `reply.py` – quote body (air, pickup, total, transit, valid until, rate_id as quote reference), booking + tracking drafts, `send_quote` (only if every lot priced).
- `writer.py` – `HUMANIZE=1`: LLM rewrites each outgoing mail, verifies prices/refs present, else template. Costs ~300–500 tokens/mail.
- `db.py` – SQLite `quote_bot.sqlite3`: `state` (since_id), `messages` (id, category, method, status, flags, llm_tokens), `quotes` (best rate per lot, raw incl. pickup + grand_total), `sent` (our sent mail id → in_reply_to), `bookings`.
- `maillog.py` – every incoming/outgoing mail appended to `mail_logs/YYMMDD_mail_incoming.txt` / `_outgoing.txt`.
- `main.py` – CLI, loop (sequential oldest first, `check_pending()` each poll), routing: rfq → pricing (+pickup) → reply; booking → find RFQ (thread ids, else subject/sender) → book each lot (`TALOS-<rfq>-<lot>`; 404/410 → re-price same airline + book) → confirm customer + pickup order to winning trucker; tracking → bookings from DB → status reply; carrier_quote → pickup bid. Console: readable block + `QUOTE_JSON` line.

Env (`.env`, see `.env.example`): FIVEU_API_KEY, FIVEU_BASE_URL, LLM_MODEL, POLL_INTERVAL_S=3, DB_PATH, PORTS_REFRESH_S, BRACKETS_TTL_S, QUOTE_WINDOW_S=360, SAFETY_MARGIN_S=60, RATE_WORKERS, AUTO_REPLY, REPLY_SIGNATURE, CARRIER_LIST, PICKUP_ZONES, PICKUP_WAIT_S, PICKUP_MAX_TRUCKERS, MAIL_LOG_DIR, HUMANIZE.

## Decisions taken with the user
Airlines from /ports (not CSV) · regex first, LLM fallback · break-point check next break only · continuous loop 3 s · SQLite · best effort + flags on gaps · requests + openai SDK · margin 0 (cheapest total) · currency as returned · attachments ignored · sequential mails · Python 3.11+ · small package layout · user wants code delivered into this folder, fast iterations.

## Observed live
- 275 RFQ "Fastest air freight to ORD" (lindqvist-medtech), 379 booking (before booking code existed).
- 412–415, 1134–1140: trucker bids "Re: Pickup quote 51447/51896 Bergisch Gladbach to CGN". 412–415 came from requests NOT sent by this bot → check if a teammate script also sends pickup requests (double requests).
- 947 RFQ rheintal-elektronik CGN→ORD 679 kg 4.19 cbm 6 pcs, pickup 51896 Bergisch Gladbach: 4 airlines, best XC 1483.43 EUR + pickup Elbkante 115.98 → quoted 1599.41 EUR in 33 s (no LLM). 1156 = customer booking for it (arrived before booking code; reprocess: `--message-id 1156 --reply`; rate expired → re-price path).
- Leaderboard: TALOS has **exactly 60 points**. Cause unknown. Hypotheses to check via `GET /v1/board/me` (X-API-Key): accuracy 45 + time 15 with cost 0; or partial accuracy (quote within 5 % but not closest; e.g. best price incl. start-kit tariff airlines or carrier email quotes we ignore); or pickup priced differently (cheapest trucker vs. what scorer expects); or 60 = per-quote average over several quotes.

## Open TODOs (priority)
1. `GET /v1/board/me` → understand the 60 (which component is missing).
2. Include start-kit tariff airlines (`data/261121_tariffs_normalized_01.csv`) in best-price search; maybe email airline desks ("ask 2–5 carriers") for spot quotes.
3. LLM cost: keep rules-first; consider `HUMANIZE=0` if cost score suffers.
4. Persist pending pickup requests in SQLite (lost on restart).
5. Hourly changes: watch `dataset_version`, lanes, brackets, fx; refresh caches on change.
6. Rate-sheet mails with attachments (tariff ingestion agent) – currently only categorised.
7. Sessions UI with streaming (required deliverable; not started). QUOTE_JSON lines + SQLite + mail_logs are the data source.
8. Tune PICKUP_WAIT_S vs time score; trucker replies so far came within ~30 s.

## User environment
macOS, zsh (`#` comments not allowed interactively unless `setopt interactivecomments`), miniconda Python 3.13 + `.venv`. Folder: `/Users/Shared/3200 Hackathons/5U`. Preferences: short answers, no filler, versioned filenames `YYMMDD_name_01`.
