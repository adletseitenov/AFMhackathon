@echo off
setlocal
cd /d "%~dp0"

echo [KOZ] Creating virtual environment...
py -3.12 -m venv .venv || py -3 -m venv .venv || python -m venv .venv

echo [KOZ] Installing core dependencies (torch-free)...
call .venv\Scripts\activate.bat
python -m pip install --upgrade pip
python -m pip install -r requirements.txt

echo [KOZ] Core setup complete. The own model trains in seconds (scikit-learn, no downloads).
echo.
echo [KOZ] OPTIONAL: live multimodal extractors (heavy, only for live upload demo):
echo     python -m pip install faster-whisper==1.0.2 easyocr==1.7.1 open-clip-torch==2.24.0 torch==2.3.0
echo   After installing, pre-download models:
echo     python -c "from faster_whisper import WhisperModel; WhisperModel('small')"
echo     python -c "import easyocr; easyocr.Reader(['ru','en'])"
echo     python -c "import open_clip; open_clip.create_model_and_transforms('ViT-B-32', pretrained='laion2b_s34b_b79k')"
endlocal
