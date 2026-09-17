"""Alert evaluator for ALERTS.md (A1 p95 latency, A2 error rate, A3 tool failure rate).

Reads the last WINDOW_MIN minutes from the Langfuse v2 observations API (the legacy traces API is unavailable to new
organizations), evaluates each alert once there are at least MIN_REQUESTS requests, and posts firing alerts to
ALERT_WEBHOOK_URL (Slack/Discord incoming webhook) or prints them as JSON lines. No PHI: only counts, rates, thresholds.
Run every 5 minutes (Railway cron):  python alerts.py
"""
import json
import os
import sys
from datetime import datetime, timedelta, timezone
from typing import Dict, List, Optional

import httpx

WINDOW_MIN = 15
# ponytail: fixed offset for Langfuse Cloud ingestion (measured 1-8 min); detection lags 10-25 min. Push metrics to a
# real-time backend (OTel/Prometheus) when paging needs to be faster.
INGEST_LAG_MIN = 10
MIN_REQUESTS = 20
P95_LATENCY_S = 10.0
ERROR_RATE = 0.05
TOOL_FAILURE_RATE = 0.10
REQUEST_SPANS = ("message", "schedule_scan")
PAGE_LIMIT = 1000


def fetch(client: httpx.Client, name: str, start: datetime, end: datetime, fields: str = "core") -> List[dict]:
    """All completed observations with this name in the window (cursor pagination, deduplicated by id)."""
    seen: Dict[str, dict] = {}
    cursor: Optional[str] = None
    while True:
        params = {"name": name, "fromStartTime": start.isoformat().replace("+00:00", "Z"),
                  "toStartTime": end.isoformat().replace("+00:00", "Z"), "limit": PAGE_LIMIT, "fields": fields}
        if cursor:
            params["cursor"] = cursor
        r = client.get("/api/public/v2/observations", params=params)
        r.raise_for_status()
        body = r.json()
        for o in body.get("data", []):
            if o.get("type") == "EVENT" or o.get("latency") is not None:  # spans are ingested at start and end
                seen[o.get("id") or f"{o.get('startTime')}-{len(seen)}"] = o
        cursor = (body.get("meta") or {}).get("cursor")
        if not cursor or not body.get("data"):
            return list(seen.values())


def p95(values: List[float]) -> Optional[float]:
    if not values:
        return None
    v = sorted(values)
    return v[min(len(v) - 1, round(0.95 * (len(v) - 1)))]


def evaluate(requests: List[dict], errors: int, fhir_calls: int, tool_failures: int) -> List[dict]:
    """Pure: completed request spans and metric event counts -> alert results (ALERTS.md definitions).
    errors = metric.error events: HTTP 5xx, LLM-caused fallback, verifier exception, audit unavailable."""
    n = len(requests)
    latencies = [o["latency"] for o in requests if o.get("latency") is not None]
    errors = min(errors, n)  # one request can emit two error kinds (e.g. LLM timeout, then audit unavailable)
    results = []
    for alert, value, threshold, enough in (
            ("A1_p95_latency_s", p95(latencies), P95_LATENCY_S, n >= MIN_REQUESTS),
            ("A2_error_rate", errors / n if n else None, ERROR_RATE, n >= MIN_REQUESTS),
            ("A3_tool_failure_rate", tool_failures / fhir_calls if fhir_calls else None, TOOL_FAILURE_RATE, n >= MIN_REQUESTS)):
        firing = bool(enough and value is not None and value > threshold)
        results.append({"alert": alert, "value": None if value is None else round(value, 4), "threshold": threshold,
                        "requests": n, "evaluated": enough, "firing": firing})
    return results


def main() -> int:
    host = os.environ.get("LANGFUSE_HOST", "https://cloud.langfuse.com").rstrip("/")
    auth = (os.environ["LANGFUSE_PUBLIC_KEY"], os.environ["LANGFUSE_SECRET_KEY"])
    if len(sys.argv) == 3:  # replay a past window: python alerts.py 2026-09-17T13:00:00Z 2026-09-17T13:15:00Z
        start, end = (datetime.fromisoformat(a.replace("Z", "+00:00")) for a in sys.argv[1:])
    else:
        end = datetime.now(timezone.utc) - timedelta(minutes=INGEST_LAG_MIN)
        start = end - timedelta(minutes=WINDOW_MIN)
    with httpx.Client(base_url=host, auth=auth, timeout=30) as client:
        requests = [o for name in REQUEST_SPANS for o in fetch(client, name, start, end)]
        errors, fhir_calls, tool_failures = (len(fetch(client, f"metric.{m}", start, end))
                                             for m in ("error", "fhir_call", "tool_failure"))
    results = evaluate(requests, errors, fhir_calls, tool_failures)
    window = {"from": start.isoformat(timespec="seconds"), "to": end.isoformat(timespec="seconds")}
    firing = [r for r in results if r["firing"]]
    webhook = os.environ.get("ALERT_WEBHOOK_URL")
    for r in firing:
        text = (f":rotating_light: {r['alert']} = {r['value']} (threshold {r['threshold']}) over {WINDOW_MIN} min, "
                f"{r['requests']} requests. Runbook: ALERTS.md")
        if webhook:
            httpx.post(webhook, json={"text": text, "content": text}, timeout=10)
    print(json.dumps({"alert": bool(firing), "window": window, "results": results}))
    return 1 if firing else 0


if __name__ == "__main__":
    sys.exit(main())
