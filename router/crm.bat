@echo off
rem Start the CRM on this machine and open it in the browser (Windows).
rem Read-only: the mailbox poller stays off, so nothing is fetched, sent or booked and no
rem LLM is called. The first run creates .venv and installs the dependencies.
rem
rem   crm.bat [port] [both]      default port 8010; "both" also opens the joke version
setlocal
cd /d "%~dp0"
set PORT=%1
if "%PORT%"=="" set PORT=8010
if not exist .venv\Scripts\uvicorn.exe (
  echo First run: installing into .venv, about a minute ...
  python -m venv .venv
  .venv\Scripts\python -m pip install --quiet -e .
)
set MAILBOX_POLL_ENABLED=false
set LANGFUSE_ENABLED=false
start "" /b powershell -NoProfile -Command "Start-Sleep 3; Start-Process 'http://localhost:%PORT%/crm'"
if /i "%2"=="both" start "" /b powershell -NoProfile -Command "Start-Sleep 4; Start-Process 'http://localhost:%PORT%/crm/slop'"
echo TALOS CRM: http://localhost:%PORT%/crm    joke version: http://localhost:%PORT%/crm/slop    Ctrl+C stops it.
.venv\Scripts\uvicorn app.main:app --port %PORT%
