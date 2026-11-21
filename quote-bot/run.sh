#!/usr/bin/env bash
# One-command start: creates .venv and installs requirements on first run (or when requirements.txt
# changed), then starts the bot with --reply. Extra arguments are passed on (e.g. --redo-bookings).
set -euo pipefail
cd "$(dirname "$0")"

PY="${PYTHON:-}"
if [ -z "$PY" ]; then
  for c in python3.13 python3.12 python3.11 python3; do
    if command -v "$c" >/dev/null 2>&1 && "$c" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)'; then
      PY="$c"; break
    fi
  done
fi
[ -n "$PY" ] || { echo "Python 3.11+ required (set PYTHON=/path/to/python)"; exit 1; }

if [ ! -x .venv/bin/python ]; then
  echo "creating .venv with $PY"
  "$PY" -m venv .venv
fi
if [ ! -f .venv/.installed ] || [ requirements.txt -nt .venv/.installed ]; then
  .venv/bin/python -m pip install -q --upgrade pip
  .venv/bin/python -m pip install -q -r requirements.txt
  touch .venv/.installed
fi
exec .venv/bin/python -m quote_bot --reply "$@"
