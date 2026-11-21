#!/usr/bin/env bash
# Start the CRM on this machine and open it in the browser.
# Read-only: the mailbox poller stays off, so nothing is fetched, sent or booked and no LLM
# is called. The first run creates .venv and installs the dependencies.
#
#   ./crm.sh [port] [both]      default port 8010; "both" also opens the joke version
set -euo pipefail
cd "$(dirname "$0")"
PORT="${1:-8010}"

# A Python of 3.12 or newer, by name. On a Mac without developer tools the plain `python3`
# is only a stub that asks to install Xcode, so the versioned names are tried first.
find_python() {
  for candidate in "${PYTHON:-}" python3.13 python3.12 python3; do
    if [ -n "$candidate" ] && command -v "$candidate" >/dev/null 2>&1 \
      && "$candidate" -c 'import sys; sys.exit(sys.version_info < (3, 12))' 2>/dev/null; then
      echo "$candidate"
      return
    fi
  done
}

if [ ! -x .venv/bin/uvicorn ]; then
  echo "First run: installing into .venv (about a minute) ..."
  UV="$(command -v uv || true)"
  if [ -z "$UV" ] && [ -x "$HOME/.local/bin/uv" ]; then UV="$HOME/.local/bin/uv"; fi
  if [ -n "$UV" ]; then  # uv brings its own Python if this machine has none
    "$UV" venv --quiet --clear --python 3.13 .venv
    "$UV" pip install --quiet -p .venv/bin/python -e .
  else
    PY="$(find_python)"
    if [ -z "$PY" ]; then
      echo "No Python 3.12 or newer found. Either install one from https://www.python.org/downloads/"
      echo "and open a new terminal, or install uv, which brings its own Python:"
      echo "  curl -LsSf https://astral.sh/uv/install.sh | sh"
      echo "Then run this script again."
      exit 1
    fi
    "$PY" -m venv --clear .venv
    .venv/bin/python -m pip install --quiet -e .
  fi
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
