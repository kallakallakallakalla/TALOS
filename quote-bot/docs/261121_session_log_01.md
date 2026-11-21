# Session log 2026-11-21 – 5U challenge, quote_bot (Claude chat with Ko, team TALOS)

Chronological record of the chat so the next person/agent has the full context. Current state and
architecture: `CLAUDE.md`.

## 15:43 – Start kit, deck and data
- Request: look at start kit, presentation, understand the data.
- Deck: digital air forwarder, one mailbox, RFQ every 6 min, quote → book → track, scoring 45 accuracy / 40 LLM cost / 15 time, only accepted quotes (within 5 % of best price, first reply, within 6 min), hourly unannounced changes, judging pitch/UX/tech depth/leaderboard.
- Start kit: carrier list (29 airlines, 22 truckers), 32 pickup zones, 10 airline tariffs in deliberately different formats.
- Normalized all tariffs → `data/261121_tariffs_normalized_01.csv` + `data/ingest_startkit.py` (Danube scan typed by hand).
- Findings: identical price rule in all sheets; higher break cheaper in 12.8 % of cases; surcharges must be on billed kg (naive calc wrong 7.8 %); next break is enough; carrier spread median 16 % (2nd best within 5 % only 12 %) → must price all carriers; quirks (flat breaks, ×1.06 minimums); niche destinations only via 4 airlines without sheets.
- Gaps of quote_bot v1 vs deck: print only, no pickup, no carrier e-mails, no booking/tracking/rate sheets/UI.

## 16:32 – "Check if bookings work, none on the leaderboard; tracking doesn't work"
- DB showed booking mails stored as `done`, no `bookings`/`sent` tables → the running process was an old build without booking code. Current code passed the mock e2e.
- Advice: stop old bot, `--message-id 1503 --reply` (rate still valid), restart.

## 16:41 – Push to GitHub, new branch
- Repo `kallakallakallakalla/TALOS` (private; main has teammates' `router/`, `langfuse/`, `openapi.json`, `vocab.md`).
- Branch `quote-bot`, folder `quote-bot/`; first push excluded `.env`, DB, logs.

## 16:45 – "Things don't get booked, fix + fix tracking, be sure"
- Still old process. Read the API spec (`openapi.json`): booking 201/200/404/409/410/**503 → book by e-mail with the airline**, confirmation mail "Booking <ref> confirmed", no status call, allowed recipients incl. `ratedesk@`/`tracking@hackloop.mail`, only first quote per thread counts, `/v1/board/me` reason codes.
- Fixes: booking mails recognised by rules; 410/404 → fresh rate (same airline, else cheapest); 409 recorded; 503 → booking e-mail to airline; airline confirmation → status confirmed; tracking reply from CRM with reference/status/transit, quoted-not-booked case; single-instance lock; build hash banner; `--redo-bookings`.
- Mock rebuilt to behave like the live API; all flows pass. Delivered build 3dc4ce14.

## 16:51 – "Include the router folder to lower LLM cost"
- Declined to integrate: router calls an LLM on every mail via api.openai.com (outside the 5U gateway → bypasses cost scoring; via the gateway it raises cost); categories don't fit. Bigger saving: `HUMANIZE=0`.

## 16:53 – Start command
`pkill -f quote_bot; cd …/5U; source .venv/bin/activate; python -m quote_bot --reply --redo-bookings` → old acceptances booked (10 bookings).

## 16:59 – Dangerous goods edge case + rate plans via text/PDF
- Live log: RFQ 2267 said "non-dangerous", packing spec attachment showed DG; customer refused because we quoted without asking for DG documents. "we will not go ahead" matched the booking regex → BK-ZM78K6DQ booked by mistake (Juniper, 2685.35 EUR; no cancel API). Thank-you mails were classified `booking` by the LLM → re-confirmation loop (~7×).
- Fixes (build 4f99c1ab): attachments read (pdf/xlsx/docx/txt/csv/images); DG evidence → DG document request instead of quote, quote after documents arrive (in thread); refusal rule (never book); thank-you rule (no reply, no LLM); no re-confirmation for existing bookings; no second quote in a quoted thread; no pickup order mails.
- Rate sheets: airline mails with attachments/tariff text → rule parser (158/158 start-kit lanes exact) → SQLite `tariffs` → priced alongside the portal for airlines not on the portal; booked by e-mail; confirmations matched. Start-kit tariffs seeded (`TARIFF_PRICING=0` to disable).
- Requirements + `pypdf`, `openpyxl`.

## 17:10 – Push everything with full context, must run on the next computer unchanged
- User chose to commit `.env` (team API key) to the private repo.
- Added `run.sh`/`run.bat` (create venv + install + run), updated `CLAUDE.md`, `README.md`, `.env.example`, this log; committed DB snapshot and mail logs; excluded only `.venv` (machine-specific), `__pycache__`, `.DS_Store`, lock file.
- Running process at that time: build 3dc4ce14 (needs restart for 4f99c1ab).
