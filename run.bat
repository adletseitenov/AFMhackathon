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

echo [KOZ] Freeing port 8000 if a previous run still holds it...
for /f "tokens=5" %%P in ('netstat -ano ^| findstr :8000 ^| findstr LISTENING') do taskkill /F /PID %%P >nul 2>&1

echo [KOZ] Starting server at http://127.0.0.1:8000 ...
set KOZ_AUTO_DISCOVER=1
rem KOZ_DEEP_DISCOVER=1 — фон периодически глубоко разбирает топ-1 свежий пост
rem (реальные Whisper+OCR+CLIP), чтобы в ленте были посты с доказательствами к показу.
set KOZ_DEEP_DISCOVER=1
python -m uvicorn app.main:app --host 127.0.0.1 --port 8000
endlocal
