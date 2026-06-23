@echo off
setlocal
cd /d "%~dp0"

if not exist ".venv\Scripts\activate.bat" (
  echo [KOZ] venv not found. Creating .venv...
  py -3.12 -m venv .venv || py -3 -m venv .venv || python -m venv .venv
  call .venv\Scripts\activate.bat
  echo [KOZ] Installing dependencies...
  python -m pip install --upgrade pip
  python -m pip install -r requirements.txt
) else (
  call .venv\Scripts\activate.bat
)

echo [KOZ] Starting server at http://127.0.0.1:8000 ...
python -m uvicorn app.main:app --host 127.0.0.1 --port 8000
endlocal
