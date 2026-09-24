"""The Week 2 flow end to end, through HTTP.

Upload a document, read it, locate what was read, review the queue, approve one fact, and confirm it reached the
chart. Everything offline: OpenEMR is a MockTransport and the vision call is a fake, so this runs in CI with no
network and no key — the same discipline as the eval gate.

This is the test that would fail if the demo stopped working.
"""
import json

import httpx
import pytest

import test_main as T
from copilot import fhir, main, staging
from copilot.emr_write import content_filename
from copilot.schemas import SeenAllergy, SeenIntakeForm, SeenMedication, SeenValue

def _pdf(text: str) -> bytes:
    """A real, parseable one-page PDF with the given line of text, so locate.py has genuine coordinates."""
    content = f"BT /F1 12 Tf 72 720 Td ({text}) Tj ET".encode()
    stream = b"4 0 obj<</Length " + str(len(content)).encode() + b">>stream\n" + content + b"\nendstream endobj\n"
    return (b"%PDF-1.4\n"
            b"1 0 obj<</Type/Catalog/Pages 2 0 R>>endobj\n"
            b"2 0 obj<</Type/Pages/Kids[3 0 R]/Count 1>>endobj\n"
            b"3 0 obj<</Type/Page/Parent 2 0 R/MediaBox[0 0 612 792]/Contents 4 0 R"
            b"/Resources<</Font<</F1 5 0 R>>>>>>endobj\n"
            + stream +
            b"5 0 obj<</Type/Font/Subtype/Type1/BaseFont/Helvetica>>endobj\n"
            b"trailer<</Root 1 0 R>>\n%%EOF\n")


INTAKE_PDF = _pdf("Allergies Penicillin Medications lisinopril Concern cough")


def openemr_with_writes(state):
    """Week 1's FHIR mock, plus the standard-API write routes Week 2 needs."""
    def handler(request: httpx.Request) -> httpx.Response:
        path, method = request.url.path, request.method
        if "/apis/default/api/" in path:
            if path.endswith(f"/api/patient/{T.PID}"):
                return httpx.Response(200, json={"data": {"pid": 1, "uuid": T.PID}})
            if method == "POST" and path.endswith("/document"):
                state["uploaded"] = True
                return httpx.Response(200, json=True)
            if method == "GET" and path.endswith("/document"):
                rows = [{"filename": content_filename("intake", INTAKE_PDF), "id": 988, "hash": "h",
                         "docdate": "2026-09-23"}] if state.get("uploaded") else []
                return httpx.Response(200, json={"data": rows})
            if method == "POST" and path.endswith("/allergy"):
                state["allergy_body"] = json.loads(request.content)
                return httpx.Response(200, json={"data": {"id": 846, "uuid": "new-uuid"}})
            return httpx.Response(200, json={"data": []})
        return T.openemr(request)
    return handler


class FakeVision:
    """Returns one canned extraction, shaped exactly as the schema constrains the model to."""
    max_retries = 0

    def __init__(self, payload):
        self._payload, self.calls, self.messages = payload, [], self

    def with_options(self, **_):
        return self

    async def create(self, **kwargs):
        self.calls.append(kwargs)
        from anthropic.types import Message
        return Message.model_validate({"id": "m", "type": "message", "role": "assistant", "model": "m",
                                       "content": [{"type": "text", "text": self._payload}],
                                       "stop_reason": "end_turn", "stop_sequence": None,
                                       "usage": {"input_tokens": 10, "output_tokens": 10}})


SEEN = SeenIntakeForm(
    allergies=[SeenAllergy(substance="Penicillin", page=1, label_on_page="Allergies")],
    medications=[SeenMedication(name="lisinopril", dose="10mg", page=1, label_on_page="Medications")],
    chief_concern=SeenValue(value="cough", page=1, label_on_page="Concern"),
).model_dump_json()


@pytest.fixture
def client(monkeypatch):
    for k, v in {"OPENEMR_FHIR_BASE": T.BASE, "PUBLIC_ISSUER": T.BASE, "SMART_CLIENT_ID": "copilot",
                 "SMART_CLIENT_SECRET": "test-secret", "ALLOW_API_SESSIONS": "true", "EVAL_PATIENT_IDS": T.PID,
                 "AGENT_PUBLIC_URL": "http://agent.test", "HMAC_KEY": "test-hmac", "ANTHROPIC_API_KEY": "dummy",
                 "LLM_WARMUP": "false"}.items():
        monkeypatch.setenv(k, v)
    for k in ("AUDIT_DB_HOST", "LANGFUSE_PUBLIC_KEY", "VOYAGE_API_KEY"):
        monkeypatch.delenv(k, raising=False)
    from fastapi.testclient import TestClient
    state = {}
    with TestClient(main.app) as c:
        main.app.state.http = httpx.AsyncClient(transport=httpx.MockTransport(openemr_with_writes(state)))
        main.app.state.fhir = fhir.FhirClient(main.app.state.http, T.BASE, 6)
        main.app.state.emr_write.__init__(main.app.state.http, T.BASE, 6)
        main.app.state.staging = staging.MemoryStagingStore()
        main.app.state.llm = FakeVision(SEEN)
        c.emr_state = state
        yield c


def upload(c, handle, data=INTAKE_PDF, doc_type="intake_form"):
    return c.post("/api/session/documents", headers={"Authorization": f"Bearer {handle}"},
                  files={"file": ("intake.pdf", data, "application/pdf")}, data={"doc_type": doc_type})


# ---------------------------------------------------------------- the flow

def test_upload_extract_review_and_approve(client):
    """Guards: the demo. Every step of it, in order, through the real HTTP surface."""
    handle = T.open_session(client)

    r = upload(client, handle)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["document"]["document_id"] == "988"
    assert body["total"] == 3, "three values extracted"
    assert body["staged"] == 2, "only the two writable clinical facts are queued; a chief concern is context"

    # The citation overlay needs a located box; "Penicillin" is printed on the page next to its label.
    cites = body["extraction"]["allergies"][0]["citation"]
    assert cites["source_type"] == "document" and cites["source_id"] == "988"
    assert cites["bbox"] is not None and cites["bbox"]["page"] == 1

    q = client.get(f"/api/session/documents/988/facts", headers={"Authorization": f"Bearer {handle}"}).json()
    assert q["summary"]["pending"] == 2
    allergy = next(f for f in q["facts"] if f["fact_kind"] == "allergy")
    assert allergy["payload"]["title"] == "Penicillin"
    assert allergy["payload"]["comments"].startswith("doc=988 page=1 field=")

    d = client.post("/api/session/documents/988/facts/decision",
                    headers={"Authorization": f"Bearer {handle}"},
                    json={"field_path": allergy["field_path"], "decision": "approve"}).json()
    assert d["written"] is True and d["record"]["id"] == 846
    assert "doc=988" in client.emr_state["allergy_body"]["comments"]

    after = client.get("/api/session/documents/988/facts",
                       headers={"Authorization": f"Bearer {handle}"}).json()
    assert after["summary"]["pending"] == 1


def test_nothing_reaches_the_chart_without_an_approval(client):
    """Guards: THE property. Ingestion stores the document and stages facts; it writes no chart record."""
    handle = T.open_session(client)
    upload(client, handle)
    assert "allergy_body" not in client.emr_state


def test_a_rejected_fact_is_never_written(client):
    handle = T.open_session(client)
    upload(client, handle)
    q = client.get("/api/session/documents/988/facts",
                   headers={"Authorization": f"Bearer {handle}"}).json()
    field = q["facts"][0]["field_path"]
    d = client.post("/api/session/documents/988/facts/decision",
                    headers={"Authorization": f"Bearer {handle}"},
                    json={"field_path": field, "decision": "reject"}).json()
    assert d["written"] is False and "allergy_body" not in client.emr_state


def test_re_uploading_the_same_document_does_not_grow_the_queue(client):
    """Guards: the PRD's 'without creating duplicate or untraceable records', end to end."""
    handle = T.open_session(client)
    upload(client, handle)
    second = upload(client, handle).json()
    assert second["staged"] == 0
    q = client.get("/api/session/documents/988/facts",
                   headers={"Authorization": f"Bearer {handle}"}).json()
    assert q["summary"]["pending"] == 2


def test_the_page_image_is_served_for_the_overlay(client):
    """Guards: the PRD-required bounding-box overlay having no page to draw on."""
    handle = T.open_session(client)
    upload(client, handle)
    r = client.get("/api/session/documents/988/page/1.png", headers={"Authorization": f"Bearer {handle}"})
    assert r.status_code == 200 and r.headers["content-type"] == "image/png"
    assert r.content.startswith(b"\x89PNG")


# ---------------------------------------------------------------- refusals

def test_every_week_two_route_requires_a_session(client):
    """Guards: an unauthenticated upload, or reading another session's page images."""
    for method, url, kw in [("post", "/api/session/documents", {"files": {"file": ("a.pdf", b"x", "application/pdf")},
                                                                "data": {"doc_type": "intake_form"}}),
                            ("get", "/api/session/documents/988/facts", {}),
                            ("get", "/api/session/documents/988/page/1.png", {}),
                            ("post", "/api/session/documents/988/facts/decision",
                             {"json": {"field_path": "x", "decision": "approve"}})]:
        assert getattr(client, method)(url, **kw).status_code == 401


def test_an_unsupported_document_type_is_refused(client):
    """Guards: pitfall one — a third document type before two work reliably."""
    handle = T.open_session(client)
    assert upload(client, handle, doc_type="referral_fax").status_code == 400


def test_an_extraction_failure_still_leaves_the_document_stored_and_citable(client):
    """Guards: a failed read looking like a failed upload. The document IS in the chart either way."""
    handle = T.open_session(client)
    main.app.state.llm = FakeVision("not json")
    body = upload(client, handle).json()
    assert body["document"]["document_id"] == "988"
    assert body["extraction"] is None and body["reason"] == "unparseable" and body["staged"] == 0


# ---------------------------------------------------------------- the graph, through the API

class StubRetriever:
    """Stands in for the Voyage-backed retriever so the flow runs offline."""
    def __init__(self, *results):
        self.results, self.queries = list(results), []

    def search(self, q, **kw):
        self.queries.append(q)
        return self.results.pop(0) if self.results else []


def _chunk():
    from copilot.schemas import Citation, EvidenceChunk, SourceType
    return EvidenceChunk(
        chunk_id="pen-01", text="Amoxicillin is a penicillin-class antibiotic.", title="Penicillin",
        section="Cross-reactivity", score=0.9,
        citation=Citation(source_type=SourceType.guideline, source_id="pen-01",
                          page_or_section="Penicillin — Cross-reactivity", field_or_chunk_id="pen-01",
                          quote_or_value="Amoxicillin is a penicillin-class antibiotic."))


def test_a_question_returns_the_routing_record(client):
    """Guards: THE named PRD pitfall — a supervisor nobody outside Langfuse can inspect. Handoffs are part of
    the API contract, not only a trace."""
    handle = T.open_session(client)
    main.app.state.retriever = StubRetriever([_chunk()])
    main.app.state.llm = T.FakeClaude(T.plan_message(
        {"intent": "brief", "items": [{"kind": "record", "source_id": T.PENICILLIN, "section": "safety"}]}))
    body = T.ask(client, handle, "Is it safe to start amoxicillin?").json()

    assert body["handoffs"], "no routing record returned"
    hop = body["handoffs"][0]
    assert hop["to_node"] in {"extract", "retrieve", "answer", "refuse"}
    assert hop["reason"] and hop["correlation_id"] and hop["elapsed_ms"] >= 0


def test_the_routing_record_carries_no_prose(client):
    """Guards: COMP-3 — model prose reaching logs or the browser through a free-text reason."""
    from copilot.schemas import RoutingReason
    handle = T.open_session(client)
    main.app.state.retriever = StubRetriever([])
    main.app.state.llm = T.FakeClaude(T.plan_message({"intent": "brief", "items": []}))
    body = T.ask(client, handle, "Brief me.").json()
    codes = {r.value for r in RoutingReason}
    assert all(h["reason"] in codes for h in body["handoffs"])


def test_retrieved_evidence_comes_back_labelled_guideline(client):
    """Guards: the merge the PRD forbids — guideline text rendered as a fact about this patient."""
    handle = T.open_session(client)
    main.app.state.retriever = StubRetriever([_chunk()])
    main.app.state.llm = T.FakeClaude(T.plan_message({"intent": "brief", "items": []}))
    body = T.ask(client, handle, "Is it safe to start amoxicillin?").json()
    assert body["evidence"], "no evidence returned"
    assert all(e["citation"]["source_type"] == "guideline" for e in body["evidence"])


def test_a_broken_retriever_does_not_take_the_answer_down(client):
    """Guards: the whole point of wrapping the graph. Week 1's answer worked without any of this, and a Week 2
    failure must degrade the answer rather than remove it."""
    class Broken:
        def search(self, q, **kw):
            raise RuntimeError("voyage down")
    handle = T.open_session(client)
    main.app.state.retriever = Broken()
    main.app.state.llm = T.FakeClaude(T.plan_message(
        {"intent": "brief", "items": [{"kind": "record", "source_id": T.PENICILLIN, "section": "safety"}]}))
    r = T.ask(client, handle, "Brief me.")
    assert r.status_code == 200
    body = r.json()
    assert body["outcome"] in ("pass", "pass_with_removals") and body["sections"]
    assert body["evidence"] == []


def test_an_uploaded_document_is_visible_to_the_next_question(client):
    """Guards: a document ingested this session being invisible to the supervisor, so it can never route to
    extraction or accept a document citation."""
    handle = T.open_session(client)
    upload(client, handle)
    assert main.app.state.session_docs, "ingestion did not record the document for this session"
    main.app.state.retriever = StubRetriever([])
    main.app.state.llm = T.FakeClaude(T.plan_message({"intent": "brief", "items": []}))
    body = T.ask(client, handle, "What did the form say?").json()
    assert body["handoffs"]


def test_ingest_and_retrieval_log_the_fields_the_prd_names(caplog):
    """Guards: PRD §7's per-encounter log losing extraction confidence or retrieval hits.

    Both were computed and discarded — the ingest route returned located/total to the browser and logged
    neither, and the retriever logged only its failure path, so a retriever quietly returning nothing looked
    identical to one that was never asked. Also guards the other half: these lines must carry counts and
    scores, never a value, a label, a query or chunk text, because all four can carry PHI."""
    from pathlib import Path as _Path

    from copilot import graph as _graph
    from copilot import w2_routes as _w2

    src = _Path(_w2.__file__).read_text() + _Path(_graph.__file__).read_text()
    assert '"extraction_confidence"' in src, "PRD §7 names extraction confidence"
    assert '"hits"' in src and 'log.info("retrieval"' in src, "PRD §7 names retrieval hits"

    # And the payloads stay PHI-free: no field carries text read off a page or typed by a clinician.
    for banned in ('"query"', '"question"', '"chunk_text"', '"text": q', '"values":'):
        assert banned not in src.split('log.info("ingest"')[-1].split("})")[0], banned
