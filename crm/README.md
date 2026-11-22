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

## Netlify

This branch deploys the CRM to Netlify as a static snapshot. Connect the repository in
Netlify, pick this branch, and leave every build setting empty: `netlify.toml` at the
repository root sets them.

- `crm/netlify_build.py` runs during the build. It asks the real app (`app.py`) for every page
  and `/crm/api/*` answer and writes them to `crm/dist/`. The data is the ultra-talos
  snapshot committed in this repository (`state/board.db`, `mails/`, `logs/`, `shipments/`,
  `data/*.csv`, `state/NEEDS_HUMAN`). To show newer data, commit a new snapshot and Netlify rebuilds.
- The export's clock is set to the snapshot's newest activity, so "today" means the snapshot's
  day whenever the build runs. `CRM_SNAPSHOT_AT` (an ISO time) overrides that.
- The mail search takes a free query, so `netlify/functions/search.mjs` answers it, using
  the mail index the build writes.

To try the export locally: `pip install -r crm/requirements-netlify.txt && python crm/netlify_build.py`,
or `npx netlify-cli dev` from the repository root.

The deployed site is public. It holds the snapshot's mails and customers, so turn on
password protection in Netlify if needed.
