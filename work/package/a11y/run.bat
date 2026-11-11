@echo off
rem Windows start: double-click, or run in a terminal. Creates .venv on first run.
cd /d "%~dp0"
if not exist .venv\Scripts\python.exe (
  py -m venv .venv || goto :fail
  .venv\Scripts\python -m pip install -r requirements.txt || goto :fail
)
if "%MAX_UPLOAD_MB%"=="" set MAX_UPLOAD_MB=200
if "%MAX_PAGES%"=="" set MAX_PAGES=1000
where tesseract >nul 2>nul || echo NOTE: tesseract-ocr not found: scanned PDFs cannot be OCRed (see README)
echo Open http://localhost:8000  (upload limit %MAX_UPLOAD_MB% MB, %MAX_PAGES% pages). Press Ctrl+C to stop.
.venv\Scripts\python -m uvicorn app:app --port 8000
goto :eof
:fail
echo Setup failed: install Python 3.10+ from python.org (tick "Add to PATH") and try again.
pause
