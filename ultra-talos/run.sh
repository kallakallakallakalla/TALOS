#!/bin/sh
# Keeps the router running: restarts it if it ever exits. Stop with: pkill -f ultra-talos/run.sh; pkill -f "ython main.py"
cd "$(dirname "$0")"
while true; do
  ../router/.venv/bin/python main.py >> logs/router.log 2>&1
  echo "$(date -u +%FT%TZ) router exited, restarting in 3 s" >> logs/router.log
  sleep 3
done
