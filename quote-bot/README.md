# quote_bot – 5U START Hack RFQ bot (v1, print only)

## Flow
```
0 startup   GET /v1/me, GET /v1/portal/ports → lane map (refresh 5 min + on lane miss)
1 poll      GET /v1/mailbox/messages?since_id=X   every 3 s, oldest first
2 read      GET /v1/mailbox/messages/{id}          only if poll item has no body
3 route     rules: RFQ / booking / tracking / rate_sheet; unclear → LLM (one call)
4 extract   regex: IATA lane, weight (kg/lb), volume (cbm/cft/dims); LLM fallback
5 lane      lane map → airlines
6 brackets  GET /v1/portal/brackets  → next weight break above chargeable weight
7 rates     GET /v1/portal/rates per airline (parallel) at actual weight + next break
8 price     cheapest total, no margin
9 output    readable block + QUOTE_JSON line; SQLite (since_id, messages, quotes)
```

## Setup
```bash
cd "/Users/Shared/3200 Hackathons/5U"
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env        # set FIVEU_API_KEY
```

## Run
```bash
python -m quote_bot --dump              # 1st: print raw API responses (check field names)
python -m quote_bot --once --no-llm     # process pending mails once, rules only
python -m quote_bot                     # continuous loop, print only
python -m quote_bot --reply             # continuous loop + send quote replies
python -m quote_bot --skip-backlog      # start loop after the newest existing mail
python -m quote_bot --message-id 42     # (re)process one mail
python -m quote_bot --since-id 0        # reset poll position
```

## Modules
| File | Content |
|---|---|
| `config.py` | env vars / `.env` |
| `api.py` | HTTP client, `X-API-Key`, 429/5xx retry with `Retry-After` + deadline |
| `extract.py` | mail parsing, quote stripping, regex router/extractor, LLM fallback |
| `pricing.py` | lane map, bracket cache, break-point check, best-price pick |
| `db.py` | SQLite: `state`, `messages`, `quotes` |
| `main.py` | CLI, loop, output, `--dump` |

## Notes
- Mail field names are guessed defensively (`body`/`text`/`content`, `from`/`sender`, `received_at`/`created_at`). Check `--dump`, adjust `extract.py` `Mail.from_api` if needed.
- Airline param for portal: `code`; switches to `name` automatically if the portal rejects codes.
- Mixed currencies across airlines: compared in EUR via `fx_eur_per_usd`; display stays as returned.
- Deadline: from mail timestamp; retries stop 60 s before the 6-min window ends.
- LLM tokens per mail shown in output and stored in `messages.llm_tokens`.
- Attachments ignored in v1.
- Carrier mails (sender in `start-kit/carrier-list.csv` or subject "pickup quote") → `carrier_quote`, no LLM.
- Pickup RFQs: German postcode → zone → airport (`start-kit/pickup_zones.csv`) = origin. With `--reply`: asks up to 5 truckers serving the zone, collects bids (≤ `PICKUP_WAIT_S`, or earlier when all answered), replies air + cheapest pickup.
- "fastest / urgent / express / asap" in the mail → shortest transit, then cheapest.
- Booking mail → matched to our quote (thread ids, then subject/sender) → `POST /v1/portal/bookings` (team_reference `TEAM-<rfq>-<lot>`; expired rate → re-priced and booked) → confirmation to customer + pickup order to winning trucker. Table `bookings`.
- Tracking mail → bookings from SQLite (reference in text, else latest for sender) → status reply.
- `HUMANIZE=1`: LLM rewrites each outgoing mail; prices/references verified, else template. `HUMANIZE=0` saves tokens.
- Late trucker bids after a quote was sent are logged, not used.
