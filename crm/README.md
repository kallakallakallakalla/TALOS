# TALOS CRM

A read-only console over what the ultra-talos desk has done (ADR 58, plan in
`docs/crm-ux-plan.md`). Its own small server, independent of the router: it reads the files
ultra-talos already writes and changes nothing (no mail, no booking, no LLM call).

```bash
bash crm/crm.sh                  # installs on the first run, port 8010, opens the browser
bash crm/crm.sh 8011 /path/to/ultra-talos
```

| Page | Question it answers |
|---|---|
| Today | What needs a person, who owes us an answer, what is the desk doing? Colour strips, key figures, route globe. |
| Shipments | Where does each request stand? Table or board by status; one page per shipment with a live timeline, the mails and the desk's notes. |
| Customers | Who asks for what, and how do they decide? |
| Carriers | Which airline was booked, which trucker answers how fast? |
| Rates | Tariff lanes and pickup zones. |
| Reports | Request to booking, win rate, why we lost, busiest lanes. |
| Settings | Where each figure comes from. |

The search in the header finds shipments, customers, BK numbers and mail text (`/` focuses it).
The page follows the desk's files every 2 seconds.

| File | Content |
|---|---|
| `desk.py` | reads `mails/`, `state/board.db`, `logs/*.jsonl` (the platform's answers: sent mails, bookings), `shipments/*/notes.md`, `data/*.csv`, `state/NEEDS_HUMAN` |
| `app.py` | the server: the page and the read-only `/crm/api/*` endpoints |
| `ui/` | plain HTML, CSS and JavaScript, no build step |
| `tests/test_crm.py` | `.venv/bin/python -m pytest` (after `pip install -e ".[dev]"`) |

`ULTRA_TALOS_DIR` points it at another ultra-talos folder (default `../ultra-talos`).
