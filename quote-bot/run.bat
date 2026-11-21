@echo off
rem One-command start on Windows: creates .venv + installs requirements, then runs the bot with --reply.
cd /d "%~dp0"
if not exist .venv\Scripts\python.exe (
  py -3 -m venv .venv || python -m venv .venv
)
.venv\Scripts\python -m pip install -q -r requirements.txt
.venv\Scripts\python -m quote_bot --reply %*
