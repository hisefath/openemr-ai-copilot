"""End to end through main.py: API session -> prefetch -> question -> Claude plan -> verification -> audit.
OpenEMR is mocked at the HTTP layer with REAL OpenEMR FHIR output for a seeded synthetic patient (E1: uncoded
penicillin allergy + active amoxicillin); Claude is a fake returning answer plans. Each test names its failure mode."""
import json
import time
from pathlib import Path

import anthropic
import httpx
import pytest
from anthropic.types import Message
from fastapi.testclient import TestClient

import fhir
import main
import observability as obs

FIX = json.loads((Path(__file__).parent / "fixtures" / "edge_patients.json").read_text())
E1 = FIX["E1"]
PID = E1["patient_uuid"]
BASE = "http://emr.test/apis/default/fhir"
USER = "Practitioner/a2c41ae8-004c-4a01-8ca5-b49c6e8d7397"
SCOPE = ("openid fhirUser launch launch/patient user/Patient.rs user/AllergyIntolerance.rs user/MedicationRequest.rs "
         "user/Condition.rs user/Observation.rs user/Encounter.rs user/Appointment.rs")
PENICILLIN = next(f"AllergyIntolerance/{e['resource']['id']}" for e in E1["AllergyIntolerance"]["entry"])
AMOXICILLIN = next(f"MedicationRequest/{e['resource']['id']}" for e in E1["MedicationRequest"]["entry"]
                   if "Amoxicillin" in json.dumps(e["resource"].get("medicationCodeableConcept")) and
                   (e["resource"].get("medicationCodeableConcept") or {}).get("coding"))


def openemr(request: httpx.Request) -> httpx.Response:
    path, params = request.url.path, dict(request.url.params)
    if path.endswith("/oauth2/default/introspect"):
        return httpx.Response(200, json={"active": True, "scope": SCOPE, "client_id": "copilot",
                                         "fhirUser": f"{BASE}/{USER}", "exp": time.time() + 3600})
    if path == f"/apis/default/fhir/Patient/{PID}":
        return httpx.Response(200, json=E1["Patient"]["entry"][0]["resource"])
    resource = path.rsplit("/", 1)[-1]
    if params.get("patient") != PID:
        return httpx.Response(404, json={})
    if resource == "Observation":
        key = "Observation_laboratory" if params.get("category") == "laboratory" else "Observation_vital_signs"
        return httpx.Response(200, json=E1[key])
    return httpx.Response(200, json=E1[resource])


def plan_message(plan: dict, stop="end_turn") -> Message:
    m = Message.model_validate({"id": "msg", "type": "message", "role": "assistant", "model": "claude-haiku-4-5",
                                "content": [{"type": "text", "text": json.dumps(plan)}], "stop_reason": stop,
                                "stop_sequence": None, "usage": {"input_tokens": 1200, "output_tokens": 90}})
    m._request_id = "req_test"
    return m


class FakeClaude:
    def __init__(self, *replies):
        self.replies, self.calls, self.messages = list(replies), [], self

    def with_options(self, **_):
        return self

    async def create(self, **kwargs):
        self.calls.append(kwargs)
        reply = self.replies.pop(0)
        if isinstance(reply, Exception):
            raise reply
        return reply


@pytest.fixture
def client(monkeypatch):
    for k, v in {"OPENEMR_FHIR_BASE": BASE, "PUBLIC_ISSUER": BASE, "SMART_CLIENT_ID": "copilot",
                 "SMART_CLIENT_SECRET": "test-secret", "ALLOW_API_SESSIONS": "true", "EVAL_PATIENT_IDS": PID,
                 "AGENT_PUBLIC_URL": "http://agent.test", "HMAC_KEY": "test-hmac", "ANTHROPIC_API_KEY": "dummy"}.items():
        monkeypatch.setenv(k, v)
    monkeypatch.delenv("AUDIT_DB_HOST", raising=False)
    monkeypatch.delenv("LANGFUSE_PUBLIC_KEY", raising=False)
    with TestClient(main.app) as c:
        state = main.app.state
        state.http = httpx.AsyncClient(transport=httpx.MockTransport(openemr))
        state.fhir = fhir.FhirClient(state.http, BASE, 6)
        yield c


def open_session(c: TestClient) -> str:
    r = c.post("/api/sessions", json={"access_token": "a-demo-token-value", "patient_id": PID})
    assert r.status_code == 200, r.text
    handle = r.json()["session_handle"]
    for _ in range(100):  # prefetch runs in the background
        status = c.get("/api/session", headers={"Authorization": f"Bearer {handle}"}).json()
        if status.get("load_statuses") and "pending" not in status["load_statuses"].values():
            return handle
        time.sleep(0.02)
    raise AssertionError("prefetch never finished")


def ask(c: TestClient, handle: str, question: str, **extra):
    return c.post("/api/session/messages", headers={"Authorization": f"Bearer {handle}"}, json={"question": question, **extra})


def test_safety_question_flags_real_conflict_and_audits_every_read(client):
    """Guards: UC2 'safe to start amoxicillin?' missing an uncoded penicillin allergy; reads not audited; a
    correlation id that doesn't match between the response header and body."""
    handle = open_session(client)
    status = client.get("/api/session", headers={"Authorization": f"Bearer {handle}"}).json()
    assert status["patient_banner"]["mrn"] and any(f["rule_id"] == "allergy-drug-class" for f in status["flags"])
    main.app.state.llm = FakeClaude(plan_message({"intent": "safety_check", "scope_violation": "none", "proposed_drugs": ["amoxicillin"],
                                                  "items": [{"kind": "record", "source_id": PENICILLIN, "section": "safety"},
                                                            {"kind": "record", "source_id": AMOXICILLIN, "section": "safety"}]}))
    r = ask(client, handle, "Is it safe to start amoxicillin?")
    assert r.status_code == 200, r.text
    body = r.json()
    assert r.headers["X-Correlation-ID"] == body["correlation_id"]
    assert body["outcome"] == "pass" and body["withheld_count"] == 0
    assert any(f["rule_id"] == "allergy-drug-class" and f["severity"] == "high" for f in body["flags"])
    lines = [ln for s in body["sections"] for ln in s["lines"]]
    assert any("Penicillin" in ln["text"] and PENICILLIN in ln["source_ids"] for ln in lines)
    events = main.app.state.audit.events
    kinds = [e.event.value for e in events]
    assert kinds.count("fhir_read") >= 7 and {"session_create", "llm_call", "question"} <= set(kinds)
    rendered = json.dumps([e.model_dump() for e in events])
    assert "Penicillin" not in rendered and "Amoxicillin" not in rendered and "safe to start" not in rendered  # no PHI, no question text
    assert all(e.fhir_path is None or "?" not in e.fhir_path for e in events)


def test_invented_source_id_is_withheld_and_audited_as_denied(client):
    """Guards: a model-invented record reaching the physician (FM-09)."""
    handle = open_session(client)
    main.app.state.llm = FakeClaude(plan_message({"intent": "brief", "items": [
        {"kind": "record", "source_id": PENICILLIN, "section": "safety"},
        {"kind": "record", "source_id": "MedicationRequest/00000000-dead-beef-0000-000000000000", "section": "background"}]}))
    body = ask(client, handle, "Brief me").json()
    assert body["outcome"] == "pass_with_removals" and body["withheld_count"] == 1
    assert any(e.event.value == "denied" for e in main.app.state.audit.events)


def test_question_about_another_patient_is_refused_and_audited(client):
    """Guards: UC6 disclosure about another patient; refusal not logged (FM-10)."""
    handle = open_session(client)
    main.app.state.llm = FakeClaude(plan_message({"intent": "other", "scope_violation": "other_patient", "items": []}))
    body = ask(client, handle, "What meds is the patient in room 5 on?").json()
    assert body["outcome"] == "refused" and body["sections"] == []
    assert any(e.event.value == "refusal" for e in main.app.state.audit.events)


def test_claude_timeout_falls_back_to_cited_chart_data(client):
    """Guards: an LLM failure producing an error screen or an unverified answer (FM-06)."""
    handle = open_session(client)
    main.app.state.llm = FakeClaude(anthropic.APITimeoutError(request=httpx.Request("POST", "http://api.test")))
    body = ask(client, handle, "Brief me").json()
    assert body["outcome"] == "fail" and body["notice"]
    assert any(ln["source_ids"] for s in body["sections"] for ln in s["lines"])
    assert any(k.startswith("error,kind=llm_timeout") for k in obs.METRICS)


def test_audit_failure_returns_no_phi(client):
    """Guards: disclosing chart data when the access can't be recorded (FM-15)."""
    handle = open_session(client)
    main.app.state.llm = FakeClaude(plan_message({"intent": "brief", "items": [{"kind": "record", "source_id": PENICILLIN, "section": "safety"}]}))
    main.app.state.audit.fail = True
    r = ask(client, handle, "Brief me")
    assert r.status_code == 503
    assert "Penicillin" not in r.text and r.json()["error"]["code"] == "audit_unavailable"


def test_missing_or_unknown_session_is_401_with_error_envelope(client):
    """Guards: questions without a session, and stack traces or input echoed in errors."""
    r = client.post("/api/session/messages", json={"question": "Brief me"})
    assert r.status_code == 401 and r.json()["error"]["correlation_id"] == r.headers["X-Correlation-ID"]
    r = ask(client, "not-a-real-handle", "Brief me")
    assert r.status_code == 401
    r = client.post("/api/sessions", json={"access_token": "short"})
    assert r.status_code == 400 and "short" not in r.text


def test_api_session_for_non_allowlisted_patient_is_refused(client):
    """Guards: API sessions reopening client-chosen patient binding (AUDIT SEC-1)."""
    r = client.post("/api/sessions", json={"access_token": "a-demo-token-value", "patient_id": "00000000-0000-0000-0000-000000000000"})
    assert r.status_code == 403


def test_health(client):
    assert client.get("/health").json() == {"status": "ok"}
