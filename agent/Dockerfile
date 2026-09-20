# The agent ships as one Python package (`copilot`) plus its pinned runtime — nothing from the OpenEMR fork around it.
FROM python:3.12-slim
WORKDIR /app
# Pins install in their own layer so editing source does not reinstall the world on every build.
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY copilot ./copilot
# No access log: OAuth callbacks carry code/state in the query string (ARCHITECTURE §2); main.py logs path-only lines.
CMD ["sh", "-c", "exec uvicorn copilot.main:app --host 0.0.0.0 --port ${PORT:-8000} --no-access-log --proxy-headers --forwarded-allow-ips='*'"]
