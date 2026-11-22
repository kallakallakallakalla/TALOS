@echo off
rem Start the CRM on this machine and open it in the browser (Windows).
rem Read-only: it shows what the ultra-talos desk has done, from the desk's own files, and
rem changes nothing. The first run creates .venv and installs the dependencies.
rem
rem   crm.bat [port] [ultra-talos folder]
rem     port      default 8010
rem     folder    the ultra-talos folder to show (default: ..\ultra-talos next to this folder)
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
  if not exist "%~f2\" (
    echo Folder not found: %~2
    exit /b 1
  )
  set "ULTRA_TALOS_DIR=%~f2"
)
start "" /b powershell -NoProfile -Command "Start-Sleep 3; Start-Process 'http://localhost:%PORT%/crm'"
echo TALOS CRM: http://localhost:%PORT%/crm    Ctrl+C stops it.
.venv\Scripts\uvicorn app:app --port %PORT%
