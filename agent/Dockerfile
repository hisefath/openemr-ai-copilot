# The agent ships as one Python package (`copilot`) plus its pinned runtime — nothing from the OpenEMR fork around it.
FROM python:3.12-slim
WORKDIR /app
# Tesseract is an OS package, not a Python one: pytesseract only shells out to the binary. Without this layer the
# agent imports and starts perfectly and then fails at OCR call time — so a scanned lab PDF works on a developer's
# Mac (Homebrew tesseract) and silently returns unlocated-everything in the deployed app. Its own layer, above the
# pip layer, because it changes far less often than requirements.txt.
RUN apt-get update \
    && apt-get install -y --no-install-recommends tesseract-ocr \
    && rm -rf /var/lib/apt/lists/*
# Pins install in their own layer so editing source does not reinstall the world on every build.
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY copilot ./copilot
# No access log: OAuth callbacks carry code/state in the query string (ARCHITECTURE §2); main.py logs path-only lines.
CMD ["sh", "-c", "exec uvicorn copilot.main:app --host 0.0.0.0 --port ${PORT:-8000} --no-access-log --proxy-headers --forwarded-allow-ips='*'"]
