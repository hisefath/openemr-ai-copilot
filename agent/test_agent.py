"""End-to-end /chat with fake FHIR + fake Claude. Run: pytest -q"""
from types import SimpleNamespace as NS

import httpx
from fastapi.testclient import TestClient

import main
from verify import Briefing, Claim

FHIR = {
    "/AllergyIntolerance": {"entry": [{"resource": {"id": "a1", "code": {"text": "Penicillin"}, "reaction": [{"manifestation": [{"text": "Hives"}]}]}}]},
    "/MedicationRequest": {"entry": [{"resource": {"id": "m1", "status": "active", "medicationCodeableConcept": {"text": "Lisinopril 10 mg"}}}]},
}


def fhir_handler(request: httpx.Request):
    assert request.headers["Authorization"] == "Bearer demo-token"
    return httpx.Response(200, json=FHIR[request.url.path])


class FakeLLM:
    def __init__(self):
        self.calls = 0
        self.messages = NS(parse=self.parse)

    async def parse(self, **kw):
        self.calls += 1
        usage = NS(input_tokens=1, output_tokens=1)
        if self.calls == 1:  # ask for two tools in parallel
            return NS(stop_reason="tool_use", usage=usage, parsed_output=None, content=[
                NS(type="tool_use", id="t1", name="get_allergies", input={"patient_id": "p1"}),
                NS(type="tool_use", id="t2", name="get_medications", input={"patient_id": "p1"}),
            ])
        return NS(stop_reason="end_turn", usage=usage, content=[], parsed_output=Briefing(answer="Penicillin allergy; on lisinopril.", claims=[
            Claim(text="Penicillin allergy", source_id="AllergyIntolerance/a1", quoted_value="Penicillin"),
            Claim(text="On warfarin", source_id="MedicationRequest/zzz", quoted_value="warfarin"),
        ]))


def test_chat_verifies_citations(caplog):
    main.FHIR_BASE = ""
    import fhir
    fhir.FHIR_BASE = ""
    with TestClient(main.app) as client:
        main.app.state.http = httpx.AsyncClient(transport=httpx.MockTransport(fhir_handler), base_url="http://fhir")
        main.app.state.llm = FakeLLM()
        fhir.FHIR_BASE = "http://fhir"

        assert client.get("/health").json() == {"status": "ok"}
        assert client.post("/chat", json={"patient_id": "p1", "message": "hi"}).status_code == 422  # no auth header

        r = client.post("/chat", json={"patient_id": "p1", "message": "Brief me"},
                        headers={"Authorization": "Bearer demo-token", "X-Correlation-ID": "cid-123"})
        body = r.json()
        assert r.status_code == 200, body
        assert r.headers["X-Correlation-ID"] == "cid-123" and body["correlation_id"] == "cid-123"
        assert [c["verified"] for c in body["claims"]] == [True, False]
        assert body["verification_passed"] is False
        assert "not found in record" in body["claims"][1]["text"]

        other = client.post("/chat", json={"patient_id": "p2", "message": "x", "session_id": body["session_id"]},
                            headers={"Authorization": "Bearer demo-token"})
        assert other.status_code == 409  # a session can't hop patients
    assert "demo-token" not in caplog.text
