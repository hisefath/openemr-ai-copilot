"""The dashboard BFF: read-only FHIR passthrough for the React patient dashboard.

Each test names the failure it guards against. The theme throughout is the one the rest of this project
keeps returning to: an unavailable card and an empty card are DIFFERENT FACTS, and the UI must be able to
tell them apart. A card that renders "none recorded" when the truth is "we could not find out" is the same
class of error as a bounding box drawn around the wrong value.
"""
import json
import time
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

from copilot import fhir, main
from copilot.schemas import LoadStatus

PID = "a2c4925c-53ef-4fc8-96bc-192cb0b79e34"
BASE = "http://emr.test/apis/default/fhir"
USER = "Practitioner/a2c41ae8-004c-4a01-8ca5-b49c6e8d7397"
SCOPE = ("openid fhirUser launch launch/patient user/Patient.rs user/AllergyIntolerance.rs "
         "user/MedicationRequest.rs user/Condition.rs user/Observation.rs user/Encounter.rs")


def bundle(*resources):
    """fhir.py enforces a patient lock on every searchset (ARCHITECTURE §1): a resource that does not
    reference THIS patient is recorded as an error, never rendered. Fixtures must carry the reference or
    they are testing the guard rather than the route."""
    return {"resourceType": "Bundle", "type": "searchset",
            "entry": [{"resource": {**r, "patient": {"reference": f"Patient/{PID}"}}} for r in resources]}


def openemr(request: httpx.Request) -> httpx.Response:
    path = request.url.path
    if path.endswith("/oauth2/default/introspect"):
        return httpx.Response(200, json={"active": True, "scope": SCOPE, "client_id": "copilot",
                                         "fhirUser": f"{BASE}/{USER}", "exp": time.time() + 3600})
    if path == f"/apis/default/fhir/Patient/{PID}":
        return httpx.Response(200, json={"resourceType": "Patient", "id": PID, "active": True,
                                         "name": [{"family": "Chen", "given": ["Maya"]}],
                                         "gender": "female", "birthDate": "1975-03-04",
                                         "identifier": [{"value": "MRN-0042",
                                                         "type": {"coding": [{"code": "MR"}]}}]})
    resource = path.rsplit("/", 1)[-1]
    if resource == "AllergyIntolerance":
        return httpx.Response(200, json=bundle(
            {"resourceType": "AllergyIntolerance", "id": "a1", "criticality": "high",
             "code": {"text": "Penicillin"}}))
    if resource == "CareTeam":
        return httpx.Response(500, json={})          # one card failing must not take the page down
    if resource == "Condition":
        return httpx.Response(200, json={"resourceType": "Bundle", "type": "searchset"})  # genuinely empty
    return httpx.Response(200, json=bundle())


@pytest.fixture
def client(monkeypatch):
    for k, v in {"OPENEMR_FHIR_BASE": BASE, "PUBLIC_ISSUER": BASE, "SMART_CLIENT_ID": "copilot",
                 "SMART_CLIENT_SECRET": "test-secret", "ALLOW_API_SESSIONS": "true",
                 "EVAL_PATIENT_IDS": PID, "AGENT_PUBLIC_URL": "http://agent.test",
                 "HMAC_KEY": "test-hmac", "ANTHROPIC_API_KEY": "dummy", "LLM_WARMUP": "false"}.items():
        monkeypatch.setenv(k, v)
    monkeypatch.delenv("AUDIT_DB_HOST", raising=False)
    monkeypatch.delenv("LANGFUSE_PUBLIC_KEY", raising=False)
    with TestClient(main.app) as c:
        st = main.app.state
        st.http = httpx.AsyncClient(transport=httpx.MockTransport(openemr))
        st.fhir = fhir.FhirClient(st.http, BASE, 6)
        yield c


def session(c: TestClient) -> dict:
    r = c.post("/api/sessions", json={"access_token": "a-demo-token-value", "patient_id": PID})
    assert r.status_code == 200, r.text
    return {"Authorization": f"Bearer {r.json()['session_handle']}"}


def test_patient_header_unwraps_the_read_bundle(client):
    """Guards: the identity bar rendering blank because fhir.py wraps a read as a one-entry searchset and
    the route forgot to unwrap it — the exact shape mismatch that returns 200 with nothing in it."""
    r = client.get("/api/dashboard/patient", headers=session(client))
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["resourceType"] == "Patient"
    assert body["name"][0]["family"] == "Chen"
    assert body["identifier"][0]["value"] == "MRN-0042"


def test_a_failing_card_reports_its_own_status_instead_of_500ing_the_page(client):
    """Guards THE design property of the dashboard: cards are independent.

    The PHP page rendered every panel in one server-side pass, so one failing query could empty the screen.
    Care Team returns HTTP 500 upstream here; the route must still answer 200 with a status the UI can
    render as 'unavailable', leaving the other five cards on screen."""
    r = client.get("/api/dashboard/cards/careteam", headers=session(client))
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["card"] == "careteam"
    assert body["status"] != LoadStatus.ok.value
    assert body["resources"] == []


def test_empty_is_reported_as_a_fact_not_as_a_failure(client):
    """Guards the distinction the whole project rests on: 'the chart records none of these' and 'we could
    not find out' must not collapse into one UI state. Condition returns a bundle with no entries."""
    r = client.get("/api/dashboard/cards/problems", headers=session(client))
    assert r.status_code == 200
    body = r.json()
    assert body["resources"] == []
    assert body["status"] in {LoadStatus.ok.value, LoadStatus.empty.value}
    assert body["status"] not in {"error", "forbidden", "expired"}


def test_a_populated_card_returns_its_resources(client):
    r = client.get("/api/dashboard/cards/allergies", headers=session(client))
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == LoadStatus.ok.value
    assert [x["code"]["text"] for x in body["resources"]] == ["Penicillin"]


def test_an_unknown_card_key_is_404_not_a_silent_empty_card(client):
    """Guards: a typo'd card key rendering as 'none recorded' forever. An unknown key is a bug in the
    front end, and it has to look like one."""
    r = client.get("/api/dashboard/cards/pancreas", headers=session(client))
    assert r.status_code == 404
    assert "unknown_card" in r.text


def test_every_card_the_ui_can_request_is_a_route_the_bff_serves(client):
    """Guards drift between the React card list and the BFF's card map. The UI fetches /cards/{key} for
    each key it renders; if the two lists diverge, a card silently 404s in production."""
    from copilot import dashboard_routes

    ui = Path(__file__).resolve().parents[2] / "dashboard" / "src" / "App.tsx"
    if not ui.exists():
        pytest.skip("dashboard source not present in this checkout")
    source = ui.read_text()
    for key in dashboard_routes.CARDS:
        assert f'cardKey="{key}"' in source, f"BFF serves {key!r} but no card in App.tsx requests it"


def test_dashboard_routes_never_write(client):
    """Guards: a POST/PUT/DELETE appearing on the dashboard surface. This is a presentation layer over the
    existing API — the surprise brief says so explicitly — and the read-only property should be mechanical,
    not a promise in a comment."""
    from copilot import dashboard_routes

    for route in dashboard_routes.router.routes:
        assert set(getattr(route, "methods", set())) <= {"GET", "HEAD"}, route.path


def test_the_built_dashboard_keeps_its_session_placeholder():
    """Guards the build silently dropping the placeholder the callback substitutes.

    smart.load_page() asserts exactly one placeholder at STARTUP, so losing it is a boot failure rather
    than a mystery after a clinician authenticates — but this catches it at test time instead."""
    built = Path(main.__file__).parent / "static" / "dashboard" / "index.html"
    if not built.exists():
        pytest.skip("dashboard not built into static/ in this checkout")
    assert built.read_text().count("<!--COPILOT_SESSION_META-->") == 1


def test_dashboard_launch_is_refused_cleanly_when_the_build_is_absent(client, monkeypatch):
    """Guards a 500 on a checkout where nobody ran npm build: the route must say what to do."""
    monkeypatch.delitem(main.app.state.pages, "dashboard", raising=False)
    r = client.get("/dashboard/launch", params={"iss": BASE, "aud": BASE, "launch": "x"},
                   follow_redirects=False)
    assert r.status_code == 503
    assert "dashboard_not_built" in r.text
