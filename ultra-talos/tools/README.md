# Tools

| Tool | Use |
|---|---|
| `python3 tools/api.py METHOD PATH [JSON\|@file] [--out FILE]` | Any platform API call. Adds the key, retries 429, records instead of sending in a dry run. `--out` saves a binary answer (attachments). |
| `python3 tools/quote_air.py ORIGIN DEST GROSS_KG CBM [--date YYYY-MM-DD]` | All air prices for a shipment: portal + local tariffs, anti-weight-break, cheapest first. `--demo` runs its self-check. |
| `tools/read_attachment FILE` | Text of pdf, xlsx, docx, pptx, txt, csv. Empty output: no text layer, use vision (read tool) or `tesseract FILE -`. |
| `python3 tools/board.py show [SHIPMENT] \| find TEXT \| note SHIPMENT KIND TEXT \| sql "..."` | The black board (`state/board.db`): everything that happened, across all sessions. |
| `tools/notify "TEXT"` | Calls a human (macOS notification, board, `state/NEEDS_HUMAN`): captcha, login. |
| `chrome-devtools` MCP tools | A real Chrome window (navigate, snapshot, fill, click). For the portal web page when the API answers 503. |

New tools go here: one file, a usage line at the top, one row in this table.
