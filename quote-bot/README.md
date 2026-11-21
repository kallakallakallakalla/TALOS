# quote_bot – TALOS digital air forwarder (5U AI challenge, START Hack Munich 2026)

Reads the team mailbox, quotes air freight (portal + rate-sheet airlines + trucker pickup), books accepted
quotes, answers tracking questions, ingests new rate sheets. Full context for developers/agents: `CLAUDE.md`,
session history: `docs/261121_session_log_01.md`.

## Start (new computer, no changes needed)
```bash
git clone https://github.com/kallakallakallakalla/TALOS.git
cd TALOS && git checkout quote-bot && cd quote-bot
./run.sh                    # creates .venv, installs requirements, runs: python -m quote_bot --reply
```
Windows: `run.bat`. Requirements: Python ≥ 3.11, internet. `.env` (API key) and `quote_bot.sqlite3`
(state: last mail id, quotes, bookings) are in the repo, so the bot continues where it stopped.

**Run only one bot at a time.** Stop the old one first (`pkill -f quote_bot`), otherwise customers get
double replies. If the old bot kept running after this commit, copy/push its newer `quote_bot.sqlite3`.
The first log lines show `quote_bot build <hash>` (code version) and the tariff count.

## Flow
```
poll mailbox every 3 s (oldest first)
 ├─ customer RFQ      → attachments read → dangerous goods? ask for DG documents first
 │                      → pickup postcode? ask truckers (≤150 s) → price all portal airlines
 │                        (+ next weight break) and rate-sheet airlines → cheapest (or fastest) → quote reply
 ├─ "please book"     → book rate_id on the portal (expired → fresh rate; portal down / rate-sheet airline
 │                      → booking e-mail to the airline) → confirmation to customer
 ├─ "Booking BK-… confirmed" (airline) → booking confirmed in CRM
 ├─ status question   → status from CRM (bookings table)
 ├─ rate sheet (airline mail, pdf/xlsx/docx/txt/csv/image) → parsed into tariffs table
 ├─ refusal / thank-you → saved, no booking, no reply
 └─ trucker bid       → pickup price for the waiting RFQ
```
Rules first; the LLM (5U gateway, cost counted) only when rules cannot classify or extract.

## Commands
```bash
./run.sh --redo-bookings                     # also re-run booking/tracking mails an older build skipped
python -m quote_bot --message-id 42 --reply  # (re)process one mail
python -m quote_bot --dump                   # raw API responses
python -m quote_bot --once --no-llm          # one pass, rules only, print only
python tests/run_e2e.py                      # offline end-to-end test against a mock of the API
python tests/test_extract.py                 # regex tests
```
(inside the venv: `source .venv/bin/activate`)

## Settings (`.env`)
| Variable | Default | Meaning |
|---|---|---|
| `FIVEU_API_KEY` | – | team key |
| `TARIFF_PRICING` | 1 | 0 = quote portal airlines only |
| `HUMANIZE` | 1 | LLM rewrites outgoing mails (≈300–500 tokens each); 0 = templates, cheaper |
| `PICKUP_WAIT_S` | 150 | max wait for trucker bids |
| `QUOTE_WINDOW_S` / `SAFETY_MARGIN_S` | 360 / 60 | quote window, retries stop before its end |
| others | see `.env.example` | |

## Modules
| File | Content |
|---|---|
| `config.py` | env / `.env` |
| `api.py` | HTTP client (retries, rate limits, bookings, attachments, send mail) |
| `extract.py` | mail parsing, rules router, RFQ extraction, LLM fallback |
| `pricing.py` | lanes, weight breaks, portal + tariff pricing, best pick |
| `tariffs.py` | rate-sheet parser, tariff pricing, tariff store |
| `attachments.py` | attachment download + text (pdf, xlsx, docx, txt, csv, images) |
| `dg.py` | dangerous-goods detection + document request |
| `pickup.py` | postcode → airport, trucker requests and bids |
| `reply.py` / `writer.py` | mail texts / optional LLM rewrite |
| `db.py` | SQLite CRM: state, messages, quotes, sent, bookings, tariffs |
| `maillog.py` | mail logs in `mail_logs/` |
| `main.py` | CLI, loop, handlers |
