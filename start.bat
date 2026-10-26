@echo off
REM InterviewForge launcher for cmd.exe.
REM Keep this file pure ASCII: cmd reads it in the OEM codepage (GBK here), so any non-ASCII
REM literal turns into mojibake before the command even runs - same trap as alembic.ini.
REM It pins the three machine-specific gotchas: repo venv interpreter (bare python exits 49),
REM PYTHONPATH=. and -X utf8.
setlocal

cd /d "%~dp0"

set "PY=.venv\Scripts\python.exe"
if not exist "%PY%" (
  echo Virtualenv not found at "%PY%".
  echo Create it with:  py -3.14 -m venv .venv ^&^& .venv\Scripts\python.exe -m pip install -r requirements.txt
  exit /b 1
)

if "%~1"=="" (set "PORT=8000") else (set "PORT=%~1")
if "%HOST_ADDR%"=="" (set "HOST_ADDR=127.0.0.1")

if not exist "data\interview_forge.db" (
  echo NOTE: data\interview_forge.db is missing. The page will load but every data API fails.
  echo Migrate first:  set PYTHONPATH=. ^&^& "%PY%" -X utf8 -m alembic upgrade head
  echo Seed samples:   set PYTHONPATH=. ^&^& "%PY%" -X utf8 -m app.scripts.seed
  echo ^(Intentionally not run automatically - it writes into the development database.^)
)

if not exist "frontend\dist\index.html" (
  echo NOTE: frontend\dist is missing. The server only serves the built console.
  echo Build it:  cd frontend ^&^& npm install ^&^& npm run build
  echo Dev mode:  cd frontend ^&^& npm run dev   ^(port 3000, /api proxied to 8000^)
)

set "PYTHONPATH=."
echo InterviewForge  console  http://%HOST_ADDR%:%PORT%/
echo                API docs  http://%HOST_ADDR%:%PORT%/docs
echo                stop with Ctrl+C
"%PY%" -X utf8 -m uvicorn app.main:app --host %HOST_ADDR% --port %PORT%
