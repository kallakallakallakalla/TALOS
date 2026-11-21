# TALOS Router MVP

FastAPI-Router mit **Langfuse**-Integration, einem **Placeholder Input-API-Client** und strukturierter Multi-Enum-Klassifikation über die **`gpt-6-luna` Decision API** (OpenAI-kompatibel mit Structured Outputs).

## Projektstruktur

```text
router/
├── app/
│   ├── __init__.py
│   ├── config.py          # Pydantic Settings (.env)
│   ├── schemas.py         # Enums (EmailCategory) & DTOs
│   ├── input_client.py    # Input API Client (inkl. Placeholder-Modus)
│   ├── mailbox_client.py  # Hackloop Mailbox Client (GET /v1/mailbox/messages)
│   ├── storage.py         # SQLite-Persistenz (since_id, Deduplizierung, Ergebnisse)
│   ├── poller.py          # 10s Background-Poller mit Lock & Retry-Safe Cursor
│   ├── classifier.py      # gpt-6-luna Decision Classifier + Langfuse Tracing/Prompt-Fallback
│   └── main.py            # FastAPI Endpoints (/health, /run, /classify, /poll, /messages)
├── tests/
│   └── test_router.py     # Unit- & API-Tests mit gemocktem LLM
├── .env.example
├── pyproject.toml
└── README.md
```

## Setup

```bash
cd router
python3 -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
cp .env.example .env
```

In `.env` die eigenen Keys eintragen:
- `LANGFUSE_PUBLIC_KEY`, `LANGFUSE_SECRET_KEY`, `LANGFUSE_HOST`
- `DECISION_API_KEY`, `DECISION_API_BASE_URL`, `DECISION_MODEL` (Standard: `gpt-6-luna`)
- `INPUT_API_URL` (leer lassen = Placeholder-Daten werden verwendet)

## Starten

```bash
uvicorn app.main:app --reload --port 8000
```

## Endpunkte

- `GET /health` – Healthcheck
- `POST /run` – Holt Input von der Input-API (oder Placeholder, optional `{"input_id": "sample-1"}`) und klassifiziert ihn.
- `POST /classify` – Klassifiziert direkt einen übergebenen Text (`{"text": "..."}`).
- `POST /poll` – Führt manuell einen geschützten Poll-Zyklus gegen `GET /v1/mailbox/messages` aus.
- `GET /poller/status` – Status des 10s-Background-Pollers, `last_since_id` und Anzahl verarbeiteter Mails.
- `GET /messages` – Listet alle gespeicherten & kategorisierten Mails (optional `?category=RFQ&limit=50&offset=0`).
- `GET /messages/{message_id}` – Detailansicht einer gespeicherten Mail inkl. Entscheidung & Rohdaten.

## Tests

```bash
pytest
```
