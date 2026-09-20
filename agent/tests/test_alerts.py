"""Alert evaluator (ALERTS.md). Each test names the failure mode it guards against."""
import json
from datetime import datetime, timedelta, timezone

import httpx

from copilot import alerts


def span(latency):
    return {"id": f"s{latency}", "type": "SPAN", "latency": latency}


def errors(n, per_request=1):
    """n requests with an error, each emitting per_request metric.error events on its own trace."""
    return [{"id": f"e{i}-{k}", "type": "EVENT", "traceId": f"t{i}"} for i in range(n) for k in range(per_request)]


def by_name(results):
    return {r["alert"]: r for r in results}


def test_below_minimum_volume_nothing_fires():
    """Guards: one slow request at 3 AM paging on-call."""
    r = by_name(alerts.evaluate([span(30.0)] * 5, 0, errors(5), fhir_calls=35, tool_failures=35))
    assert not any(x["firing"] for x in r.values()) and not r["A1_p95_latency_s"]["evaluated"]


def test_each_alert_fires_on_its_own_threshold():
    """Guards: thresholds wired to the wrong metric (each fault must fire only its own alert)."""
    healthy = [span(2.0)] * 20
    assert not any(x["firing"] for x in alerts.evaluate(healthy, 0, errors(1), fhir_calls=140, tool_failures=14))
    slow = [span(2.0)] * 18 + [span(12.0)] * 2
    assert [x["alert"] for x in alerts.evaluate(slow, 0, [], 140, 0) if x["firing"]] == ["A1_p95_latency_s"]
    assert [x["alert"] for x in alerts.evaluate(healthy, 0, errors(2), 140, 0) if x["firing"]] == ["A2_error_rate"]
    assert [x["alert"] for x in alerts.evaluate(healthy, 0, [], 140, 15) if x["firing"]] == ["A3_tool_failure_rate"]


def test_error_events_count_once_per_request():
    """Guards: one request emitting two error kinds (LLM timeout, then audit unavailable) paging A2 on its own."""
    healthy = [span(2.0)] * 20
    assert by_name(alerts.evaluate(healthy, 0, errors(1, per_request=2), 140, 0))["A2_error_rate"] == {
        "alert": "A2_error_rate", "value": 0.05, "threshold": 0.05, "requests": 20, "evaluated": True, "firing": False}


def test_latency_alert_fires_below_the_question_deadline_and_ignores_scans():
    """Guards: a p95 threshold above the 9 s question deadline, which slow Claude or FHIR could never cross, and
    schedule scans (60 s budget) paging A1."""
    assert alerts.P95_LATENCY_S == 8.0
    near_deadline = [span(3.0)] * 17 + [span(8.9)] * 3
    assert by_name(alerts.evaluate(near_deadline, 0, [], 140, 0))["A1_p95_latency_s"]["firing"]
    with_scans = alerts.evaluate([span(2.0)] * 18, 4, [], 140, 0)
    assert by_name(with_scans)["A1_p95_latency_s"]["value"] == 2.0 and with_scans[0]["requests"] == 22


def test_fetch_paginates_and_ignores_unfinished_span_versions():
    """Guards: double-counting spans (v2 API returns a start and an end version) or reading only the first page."""
    pages = [{"data": [{"id": "a", "type": "SPAN", "latency": None}, {"id": "a", "type": "SPAN", "latency": 1.0}],
              "meta": {"cursor": "next"}},
             {"data": [{"id": "b", "type": "SPAN", "latency": 2.0}], "meta": {}}]

    def handler(request: httpx.Request):
        assert request.url.path == "/api/public/v2/observations" and request.url.params["environment"] == "production"
        return httpx.Response(200, json=pages.pop(0))

    end = datetime.now(timezone.utc)
    with httpx.Client(base_url="http://lf.test", transport=httpx.MockTransport(handler)) as client:
        got = alerts.fetch(client, "message", end - timedelta(minutes=15), end)
    assert sorted(o["id"] for o in got) == ["a", "b"] and all(o["latency"] for o in got)


def test_firing_alert_exits_zero_and_logs_result(monkeypatch, capsys):
    """Guards: a firing alert exiting non-zero, which Railway shows as a crashed cron run (seen live 2026-09-17)."""
    monkeypatch.setenv("LANGFUSE_PUBLIC_KEY", "pk")
    monkeypatch.setenv("LANGFUSE_SECRET_KEY", "sk")
    monkeypatch.delenv("ALERT_WEBHOOK_URL", raising=False)
    monkeypatch.setattr(alerts.sys, "argv", ["alerts.py"])
    rows = {"message": [span(2.0)] * 22, "schedule_scan": [], "metric.error": errors(22),
            "metric.fhir_call": [{}] * 140, "metric.tool_failure": []}
    monkeypatch.setattr(alerts, "fetch", lambda client, name, start, end: rows[name])
    assert alerts.main() == 0
    out = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert out["alert"] is True and [r["alert"] for r in out["results"] if r["firing"]] == ["A2_error_rate"]


def test_fetch_waits_out_rate_limit():
    """Guards: one Langfuse 429 crashing the evaluator run."""
    responses = [httpx.Response(429, headers={"retry-after": "0"}),
                 httpx.Response(200, json={"data": [{"id": "a", "type": "SPAN", "latency": 1.0}], "meta": {}})]
    end = datetime.now(timezone.utc)
    with httpx.Client(base_url="http://lf.test", transport=httpx.MockTransport(lambda request: responses.pop(0))) as client:
        assert [o["id"] for o in alerts.fetch(client, "message", end - timedelta(minutes=15), end)] == ["a"]
