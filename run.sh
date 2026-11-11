#!/usr/bin/env bash
# Local start: ./run.sh   (creates .venv on first run)
set -e
cd "$(dirname "$0")"
[ -d .venv ] || { python3 -m venv .venv && .venv/bin/pip install -q -r requirements.txt; }
command -v tesseract >/dev/null || echo "NOTE: tesseract-ocr not found: scanned PDFs cannot be OCRed (see README)"
echo "Open http://localhost:${PORT:-8000}  (upload limit ${MAX_UPLOAD_MB:-200} MB). Ctrl+C stops it."
exec .venv/bin/uvicorn app:app --port "${PORT:-8000}"
