FROM python:3.12-slim
ARG OCR_LANGS="eng"
# TMPDIR: big uploads are spooled here (RAM-backed /data), not into the small /tmp
ENV PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1 DATA_DIR=/data TMPDIR=/data
RUN apt-get update \
 && apt-get install -y --no-install-recommends tesseract-ocr $(for l in $OCR_LANGS; do echo tesseract-ocr-$l; done) fonts-liberation fonts-dejavu-core \
 && rm -rf /var/lib/apt/lists/*
WORKDIR /app
COPY requirements.txt .
RUN pip install -r requirements.txt
COPY *.py ./
COPY static ./static
RUN useradd --no-create-home --uid 10001 app && mkdir /data && chown app /data
USER app
EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s CMD python -c "import urllib.request as u; u.urlopen('http://127.0.0.1:8000/healthz', timeout=3)"
# One worker on purpose: repair jobs are limited per process (MAX_JOBS). Scale by running more containers, not more workers.
CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "1"]
