@echo off
rem Start the CRM on this machine and open it in the browser (Windows).
rem Read-only: the mailbox poller stays off, so nothing is fetched, sent or booked and no
rem LLM is called. The first run creates .venv and installs the dependencies.
rem
rem   crm.bat [port] [database]
rem     port      default 8010
rem     database  the SQLite file of a running router, to follow its live data.
rem               Without it the copy in this folder is shown.
setlocal
cd /d "%~dp0"
set PORT=%1
if "%PORT%"=="" set PORT=8010
if not exist .venv\Scripts\uvicorn.exe (
  echo First run: installing into .venv, about a minute ...
  python -m venv .venv
  .venv\Scripts\python -m pip install --quiet -e .
)
if not "%~2"=="" (
  if not exist "%~f2" (
    echo Database file not found: %~2
    exit /b 1
  )
  set "SQLITE_DB_PATH=%~f2"
)
set MAILBOX_POLL_ENABLED=false
set LANGFUSE_ENABLED=false
start "" /b powershell -NoProfile -Command "Start-Sleep 3; Start-Process 'http://localhost:%PORT%/crm'"
echo TALOS CRM: http://localhost:%PORT%/crm    Ctrl+C stops it.
.venv\Scripts\uvicorn app.main:app --port %PORT%
