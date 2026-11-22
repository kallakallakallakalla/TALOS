# ultra-talos

The same job as `../router`, built the other way round: the router only **classifies** each
mail, and an autonomous **opencode runner** (`gpt-6-luna`) solves it through the platform API.
No workflow code, no state machine: the runner's prompt, its tools and the files in this
folder are the whole system.

```text
main.py            poll the mailbox every 10 s
  classify         ../router classifier (DecisionClassifier + prompts/router-classification.md)
  dispatch         category -> opencode agent, one runner per mail, one at a time per shipment
     rfq, customer_info   -> rfq       one all-in quote (truckers + portal + local tariffs)
     customer_accept      -> booking   book now, confirm to the customer
     customer_question    -> question  answer from the shipment file
     tracking_request     -> tracking  answer from our booking records
     tariff_update        -> update    put the change into data/*.csv, confirm to the sender
     trucker_quote, trucker_decline, rate_offer, booking_confirmation
                          -> stored only: the waiting runner reads them from the mailbox
     customer_reject      -> reject    close the file; cancel a booking if there is one
     other                -> stored only
```

## What a runner sees

| Path | Content |
|---|---|
| `AGENTS.md` | the shared system prompt: role, rules, mail style, the notes format, how to improve the desk |
| `.opencode/agent/*.md` | one prompt per runner |
| `tools/api.py` | every platform call (mailbox, portal, bookings, anything new); adds the key |
| `tools/quote_air.py` | all air prices for a shipment, anti-weight-break applied |
| `tools/read_attachment` | text of pdf, xlsx, docx, pptx, txt, csv (the router's reader) |
| `data/*.csv` | carriers, pickup zones, tariff lanes: standard format with `source`, `source_message_id`, `extra` |
| `knowledge/` | API notes, the OpenAPI spec, `learnings.md` (read first by every runner) |
| `mails/`, `shipments/<id>/notes.md` | every mail with its classification; one file per shipment, the only memory between mails |

Self-improvement is part of the prompt: when an API call fails in an undocumented way the
runner fetches the current `/openapi.json` and corrects `knowledge/api.md`; when a new file
format arrives it builds the reader into `tools/`; lasting insights go to
`knowledge/learnings.md`. It may not touch `main.py`, `opencode.json` or `.opencode/`.

## Run

```bash
cd ultra-talos
../router/.venv/bin/python main.py --once 24011   # one existing mail: classify + runner
../router/.venv/bin/python main.py                # poll loop, starts after the newest mail
python3 tools/quote_air.py --demo                 # self-check of the pricing rule
```

Keys come from `../router/.env`. Both LLM steps (classifier and desk sessions) use the
gateway (`DECISION_API_KEY` at `DECISION_API_BASE_URL`), which is scored as production cost.
`ULTRA_LLM=test` switches both to `EVALUATION_KEY` directly at OpenAI; `replay.py` always does.
Runner output: `logs/<mail id>-<agent>.jsonl`.

## Dry run (default) and going live

Every call that is not a GET is recorded in `outbox/` and **not sent**, because the live
router answers the same mailbox. To send and book for real, stop the live router first, then
put `SEND_ENABLED=true` into `ultra-talos/.env`.

Do not run the poll loop next to the live router for long: the mailbox allows **one poll per
5 seconds per team key** and the portal 10 rate reads per minute, and both are shared.

## Deliberately left out

- MCP servers for CSV / portal / mail: the runner has a shell, so one CLI (`tools/api.py`)
  and plain files cover it with fewer tokens. Add MCP when a runner without shell access is needed.
- A database: the file system is the state. Add one when shipments no longer fit in `grep`.
- A cap on parallel runners: a round is a handful of mails. Add a semaphore if that changes.
