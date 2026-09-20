"""Creates the Clinical Co-Pilot operations dashboard in Langfuse (ARCHITECTURE §7, KEY_METRICS.md) through Langfuse's
unstable dashboards API, so the dashboard is code instead of clicks. Safe to re-run: it only adds what's missing.
  docker run --rm -v "$PWD":/app -w /app --env-file agent/.env agentforge-agent-dev python deploy/langfuse_dashboard.py
Counts come from the agent's metric events (one row each); `message` spans are only used for latency.
"""
import os
import time

import httpx

NAME = "Clinical Co-Pilot operations"
DESCRIPTION = ("Requests, errors, latency, tool calls, retries and verification outcomes for the Co-Pilot agent. "
               "Error rate = Errors / Questions answered; tool failure rate = tool_failure / fhir_call (ALERTS.md).")


def name_is(value):
    return {"column": "name", "operator": "=", "type": "string", "value": value}


def names(*values):
    return {"column": "name", "operator": "any of", "type": "stringOptions", "value": list(values)}


def outcome(value):
    return {"column": "metadata", "operator": "=", "type": "stringObject", "key": "outcome", "value": value}


COUNT = [{"measure": "count", "agg": "count"}]
# (name, chartType, metrics, dimensions, filters, x, y, width, height)
WIDGETS = [
    ("Questions answered", "NUMBER", COUNT, [], [name_is("metric.verification")], 0, 0, 3, 4),
    ("Errors", "NUMBER", COUNT, [], [name_is("metric.error")], 3, 0, 3, 4),
    ("p95 answer latency (ms)", "NUMBER", [{"measure": "latency", "agg": "p95"}], [], [name_is("message")], 6, 0, 3, 4),
    ("Claude cost (USD)", "NUMBER", [{"measure": "totalCost", "agg": "sum"}], [], [name_is("claude")], 9, 0, 3, 4),
    ("Answer latency p50 / p95 (ms)", "LINE_TIME_SERIES",
     [{"measure": "latency", "agg": "p50"}, {"measure": "latency", "agg": "p95"}], [], [name_is("message")], 0, 4, 6, 6),
    ("Answers, errors and tool failures", "BAR_TIME_SERIES", COUNT, [{"field": "name"}],
     [names("metric.verification", "metric.error", "metric.tool_failure")], 6, 4, 6, 6),
    ("FHIR calls, failures, retries, queue waits, tools", "HORIZONTAL_BAR", COUNT, [{"field": "name"}],
     [names("metric.fhir_call", "metric.tool_failure", "metric.fhir_forbidden", "metric.retry", "metric.queue_wait",
            "tool.get_lab_history", "tool.get_encounters")], 0, 10, 6, 6),
    ("Claude tokens", "LINE_TIME_SERIES",
     [{"measure": "inputTokens", "agg": "sum"}, {"measure": "outputTokens", "agg": "sum"}], [], [name_is("claude")],
     6, 10, 6, 6),
    ("Verified: pass", "NUMBER", COUNT, [], [name_is("metric.verification"), outcome("pass")], 0, 16, 3, 4),
    ("Verified: pass with removals", "NUMBER", COUNT, [], [name_is("metric.verification"), outcome("pass_with_removals")], 3, 16, 3, 4),
    ("Verification: fail (fallback)", "NUMBER", COUNT, [], [name_is("metric.verification"), outcome("fail")], 6, 16, 2, 4),
    ("Refused", "NUMBER", COUNT, [], [name_is("metric.verification"), outcome("refused")], 8, 16, 2, 4),
    ("Clarify", "NUMBER", COUNT, [], [name_is("metric.verification"), outcome("clarify")], 10, 16, 2, 4),
    # Queue depth (ARCHITECTURE §7): charted as the number of FHIR calls that had to wait on the OpenEMR semaphore,
    # because a count is what the widget API can aggregate and a rising share is the signal worth watching.
    ("FHIR calls that queued", "NUMBER", COUNT, [], [name_is("metric.queue_wait")], 0, 20, 3, 4),
    ("FHIR calls that queued, over time", "LINE_TIME_SERIES", COUNT, [], [name_is("metric.queue_wait")], 3, 20, 9, 4),
]


def call(lf: httpx.Client, method: str, path: str, **kwargs) -> dict:
    """One API call, waiting out Langfuse's rate limit (bounded: 6 tries)."""
    for attempt in range(6):
        r = lf.request(method, path, **kwargs)
        if r.status_code != 429:
            if r.is_error:
                raise SystemExit(f"{method} {path}: HTTP {r.status_code} {r.text[:300]}")
            return r.json()
        time.sleep(float(r.headers.get("retry-after") or 5 * (attempt + 1)))
    raise SystemExit(f"{method} {path}: still rate limited")


def main() -> None:
    """Converges: creates the dashboard, and any widget or placement from WIDGETS that it doesn't have yet."""
    with httpx.Client(base_url=os.environ.get("LANGFUSE_HOST", "https://cloud.langfuse.com").rstrip("/") + "/api/public/unstable",
                      auth=(os.environ["LANGFUSE_PUBLIC_KEY"], os.environ["LANGFUSE_SECRET_KEY"]), timeout=30) as lf:
        dashboard = next((d for d in call(lf, "GET", "/dashboards", params={"limit": 100})["data"] if d["name"] == NAME), None)
        widgets = {}  # name -> id, newest first wins
        for w in call(lf, "GET", "/dashboard-widgets", params={"limit": 100})["data"]:
            widgets.setdefault(w["name"], w["id"])
        for name, chart, metrics, dimensions, filters, *_ in WIDGETS:  # widgets first: a bad definition fails early
            if name not in widgets:
                widgets[name] = call(lf, "POST", "/dashboard-widgets", json={
                    "name": name, "view": "observations", "chartType": chart,
                    "metrics": metrics, "dimensions": dimensions, "filters": filters})["id"]
                print(f"  widget created: {name}")
        if dashboard is None:
            dashboard = call(lf, "POST", "/dashboards", json={"name": NAME, "description": DESCRIPTION})
            print(f"dashboard created: {NAME}")
        placed = {p.get("widgetId") for p in dashboard["definition"]["widgets"]}
        for name, _, _, _, _, x, y, width, height in WIDGETS:
            if widgets[name] not in placed:
                call(lf, "POST", f"/dashboards/{dashboard['id']}/placements", json={
                    "type": "widget", "widgetId": widgets[name], "x": x, "y": y, "width": width, "height": height})
                print(f"  placed: {name}")
        print(f"'{NAME}' ({dashboard['id']}) has all {len(WIDGETS)} widgets")


if __name__ == "__main__":
    main()
