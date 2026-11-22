#!/usr/bin/env bash
# Start the CRM on this machine and open it in the browser.
# Read-only: the mailbox poller stays off, so nothing is fetched, sent or booked and no LLM
# is called. The first run creates .venv and installs the dependencies.
#
#   ./crm.sh [port] [database]
#     port      default 8010
#     database  the SQLite file of a router, to follow its live data. Without it the router's
#               usual database is used (~/.talos/router.db): on the machine where the router
#               runs, "Live" therefore follows it by itself; elsewhere "Live" is empty and the
#               recorded dry run is shown.
set -euo pipefail
PORT="${1:-8010}"
if [ -n "${2:-}" ]; then
  if [ ! -f "$2" ]; then echo "Database file not found: $2"; exit 1; fi
  SQLITE_DB_PATH="$(cd "$(dirname "$2")" && pwd)/$(basename "$2")"
  export SQLITE_DB_PATH
  echo "Live data from $SQLITE_DB_PATH"
fi
cd "$(dirname "$0")"

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
# If something already listens on the port, the browser would show that program instead of
# this one: typically an older copy of the console that was never stopped.
if .venv/bin/python -c "import socket, sys; s = socket.socket(); s.settimeout(0.5); sys.exit(s.connect_ex(('127.0.0.1', int(sys.argv[1]))))" "$PORT" 2>/dev/null; then
  echo "Port $PORT is already in use, so this copy cannot start there."
  if command -v lsof >/dev/null 2>&1; then
    OLD="$(lsof -nP -iTCP:"$PORT" -sTCP:LISTEN -t 2>/dev/null | head -1)"
    if [ -n "$OLD" ]; then
      WHERE="$(lsof -a -p "$OLD" -d cwd -Fn 2>/dev/null | sed -n 's/^n//p')"
      echo "It is used by process $OLD${WHERE:+, started in $WHERE}."
      echo "If that is an older copy of this console, stop it and start again:"
      echo "  kill $OLD"
    fi
  fi
  echo "Or start this copy on another port:  bash $(basename "$0") $((PORT + 1))"
  exit 1
fi

URL="http://localhost:$PORT"
(
  sleep 3
  open_page "$URL/crm"
) &
echo "TALOS CRM: $URL/crm    Ctrl+C stops it."
MAILBOX_POLL_ENABLED=false LANGFUSE_ENABLED=false exec .venv/bin/uvicorn app.main:app --port "$PORT"
