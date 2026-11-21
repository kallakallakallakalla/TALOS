#!/usr/bin/env bash
# Start the CRM on this machine and open it in the browser.
# Read-only: the mailbox poller stays off, so nothing is fetched, sent or booked and no LLM
# is called. The first run creates .venv and installs the dependencies.
#
#   ./crm.sh [port] [both]      default port 8010; "both" also opens the joke version
set -euo pipefail
cd "$(dirname "$0")"
PORT="${1:-8010}"
if [ ! -x .venv/bin/uvicorn ]; then
  echo "First run: installing into .venv (about a minute) ..."
  "${PYTHON:-python3}" -m venv .venv
  .venv/bin/python -m pip install --quiet -e .
fi

open_page() {
  if [ "$(uname)" = "Darwin" ]; then
    open -a "Google Chrome" "$1" 2>/dev/null || open "$1"
  elif command -v xdg-open >/dev/null; then
    xdg-open "$1" >/dev/null 2>&1 || true
  fi
}
URL="http://localhost:$PORT"
(
  sleep 3
  open_page "$URL/crm"
  if [ "${2:-}" = "both" ]; then open_page "$URL/crm/slop"; fi
) &
echo "TALOS CRM: $URL/crm    joke version: $URL/crm/slop    Ctrl+C stops it."
MAILBOX_POLL_ENABLED=false LANGFUSE_ENABLED=false exec .venv/bin/uvicorn app.main:app --port "$PORT"
