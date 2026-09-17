"""Alert evaluator (ALERTS.md). Each test names the failure mode it guards against."""
from datetime import datetime, timedelta, timezone

import httpx

import alerts


def span(latency):
    return {"id": f"s{latency}", "type": "SPAN", "latency": latency}


def by_name(results):
    return {r["alert"]: r for r in results}


def test_below_minimum_volume_nothing_fires():
    """Guards: one slow request at 3 AM paging on-call."""
    r = by_name(alerts.evaluate([span(30.0)] * 5, errors=5, fhir_calls=35, tool_failures=35))
    assert not any(x["firing"] for x in r.values()) and not r["A1_p95_latency_s"]["evaluated"]


def test_each_alert_fires_on_its_own_threshold():
    """Guards: thresholds wired to the wrong metric (each fault must fire only its own alert)."""
    healthy = [span(2.0)] * 20
    assert not any(x["firing"] for x in alerts.evaluate(healthy, errors=1, fhir_calls=140, tool_failures=14))
    slow = [span(2.0)] * 18 + [span(12.0)] * 2
    assert [x["alert"] for x in alerts.evaluate(slow, 0, 140, 0) if x["firing"]] == ["A1_p95_latency_s"]
    assert [x["alert"] for x in alerts.evaluate(healthy, 2, 140, 0) if x["firing"]] == ["A2_error_rate"]
    assert [x["alert"] for x in alerts.evaluate(healthy, 0, 140, 15) if x["firing"]] == ["A3_tool_failure_rate"]
    assert by_name(alerts.evaluate(healthy, 45, 140, 0))["A2_error_rate"]["value"] == 1.0  # capped at one per request


def test_fetch_paginates_and_ignores_unfinished_span_versions():
    """Guards: double-counting spans (v2 API returns a start and an end version) or reading only the first page."""
    pages = [{"data": [{"id": "a", "type": "SPAN", "latency": None}, {"id": "a", "type": "SPAN", "latency": 1.0}],
              "meta": {"cursor": "next"}},
             {"data": [{"id": "b", "type": "SPAN", "latency": 2.0}], "meta": {}}]

    def handler(request: httpx.Request):
        assert request.url.path == "/api/public/v2/observations"
        return httpx.Response(200, json=pages.pop(0))

    end = datetime.now(timezone.utc)
    with httpx.Client(base_url="http://lf.test", transport=httpx.MockTransport(handler)) as client:
        got = alerts.fetch(client, "message", end - timedelta(minutes=15), end)
    assert sorted(o["id"] for o in got) == ["a", "b"] and all(o["latency"] for o in got)
