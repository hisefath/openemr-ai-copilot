"""Writes to OpenEMR's standard REST API.

Every shape asserted here was measured against a running OpenEMR 8.5 before it was written down — the underscore
category path, the bare `true` upload response, and the pid/puuid split are all real behaviours that cost a
debugging session each.
"""
import asyncio
import json

import httpx
import pytest

from copilot.deadline import Deadline
from copilot.emr_write import EmrWriteClient, category_path, content_filename

FHIR_BASE = "http://emr.test/apis/default/fhir"
PUUID = "a2c40eca-61ca-4d75-bbee-35756d77a9eb"
PDF = b"%PDF-1.4 fake"


def ctx():
    return dict(token="t", deadline=Deadline(10.0), correlation_id="cid")


def client(handler):
    http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return EmrWriteClient(http, FHIR_BASE), http


def run(coro):
    return asyncio.run(coro)


# ---------------------------------------------------------------- path and filename rules

def test_category_path_uses_underscores_and_a_leading_segment():
    """Guards: THE silent orphan. '?path=Lab Report' returns 200, writes the file, and leaves the document
    uncategorised and unreachable forever, because getLastIdOfPath matches replace(LOWER(name),' ','')."""
    assert category_path("Lab Report") == "Categories/Lab_Report"
    assert " " not in category_path("Patient Information")
    assert category_path("Lab Report").startswith("Categories/")


def test_content_filename_carries_a_stable_digest():
    """Guards: idempotency drifting. OpenEMR's own documents.hash column was measured and matches neither the
    content nor the stored bytes, so our digest has to travel somewhere we control — the filename."""
    a, b = content_filename("lab", PDF), content_filename("lab", PDF)
    assert a == b and a.endswith(".pdf") and "lab_" in a
    assert content_filename("lab", b"different") != a


# ---------------------------------------------------------------- identifiers

def test_resolve_pid_translates_uuid_to_the_numeric_pid():
    """Guards: posting a document to a uuid. Document routes take the numeric pid; the session holds a uuid."""
    def handler(req):
        assert req.url.path.endswith(f"/api/patient/{PUUID}")
        return httpx.Response(200, json={"data": {"pid": 1, "uuid": PUUID}})
    c, _ = client(handler)
    assert run(c.resolve_pid(PUUID, **ctx())) == 1


def test_resolve_pid_returns_none_rather_than_guessing():
    """Guards: a missing pid becoming 0 or a crash mid-ingest."""
    c, _ = client(lambda req: httpx.Response(200, json={"data": {}}))
    assert run(c.resolve_pid(PUUID, **ctx())) is None


def test_the_standard_api_base_is_derived_from_the_fhir_base():
    """Guards: posting writes at the FHIR base, where these routes do not exist."""
    seen = {}

    def handler(req):
        seen["path"] = req.url.path
        return httpx.Response(200, json={"data": {"pid": 1}})
    c, _ = client(handler)
    run(c.resolve_pid(PUUID, **ctx()))
    assert "/apis/default/api/" in seen["path"] and "/fhir/" not in seen["path"]


# ---------------------------------------------------------------- documents

def test_upload_posts_then_lists_because_the_write_returns_no_id():
    """Guards: expecting an id from the upload. insertAtPath returns bool, so the citation anchor can only come
    from a follow-up list call."""
    calls, uploaded = [], []

    def handler(req):
        calls.append((req.method, req.url.path))
        if req.method == "POST":
            uploaded.append(1)
            return httpx.Response(200, json=True)
        # Empty before the upload (so the dedup check does not short-circuit), present after it.
        rows = [{"filename": content_filename("lab", PDF), "id": 987, "hash": "abc"}] if uploaded else []
        return httpx.Response(200, json={"data": rows})

    c, _ = client(handler)
    res = run(c.upload_document(1, "Lab Report", PDF, prefix="lab", **ctx()))
    assert res.ok and res.data["data"]["id"] == 987 and res.data["deduplicated"] is False
    assert [m for m, _ in calls] == ["GET", "POST", "GET"]   # dedup check, upload, resolve id


def test_identical_bytes_are_not_uploaded_twice():
    """Guards: the PRD's 'without creating duplicate or untraceable records'. A re-ingest must return the
    existing row and make no POST at all."""
    posts = []

    def handler(req):
        if req.method == "POST":
            posts.append(req.url.path)
            return httpx.Response(200, json=True)
        return httpx.Response(200, json={"data": [{"filename": content_filename("lab", PDF), "id": 987}]})

    c, _ = client(handler)
    res = run(c.upload_document(1, "Lab Report", PDF, prefix="lab", **ctx()))
    assert res.ok and res.data["deduplicated"] is True and res.data["data"]["id"] == 987
    assert posts == [], "re-upload of identical bytes must not POST"


def test_a_200_that_lists_nothing_back_is_a_failure_not_a_success():
    """Guards: the silent-orphan signature. OpenEMR answers 200 true even when the category did not resolve;
    a document we cannot cite is not a document we stored."""
    def handler(req):
        if req.method == "POST":
            return httpx.Response(200, json=True)
        return httpx.Response(200, json={"data": []})
    c, _ = client(handler)
    res = run(c.upload_document(1, "Lab Report", PDF, **ctx()))
    assert not res.ok and res.detail == "uploaded_but_not_listed"


def test_the_upload_sends_multipart_under_the_field_openemr_reads():
    """Guards: a body OpenEMR's $_FILES['document'] never sees, which fails as an empty-file error."""
    seen = {}

    def handler(req):
        if req.method == "POST":
            seen["ctype"] = req.headers.get("content-type", "")
            seen["body"] = req.content
            return httpx.Response(200, json=True)
        rows = [{"filename": content_filename("doc", PDF), "id": 1}] if "body" in seen else []
        return httpx.Response(200, json={"data": rows})

    c, _ = client(handler)
    run(c.upload_document(1, "Lab Report", PDF, **ctx()))
    assert seen["ctype"].startswith("multipart/form-data")
    assert b'name="document"' in seen["body"] and PDF in seen["body"]


def test_the_category_path_actually_travels_on_the_query_string():
    """Guards: the underscore fix being applied in code and then not reaching OpenEMR."""
    seen = {}

    posted = []

    def handler(req):
        seen.setdefault("path_param", dict(req.url.params).get("path"))
        if req.method == "POST":
            posted.append(1)
            return httpx.Response(200, json=True)
        rows = [{"filename": content_filename("doc", PDF), "id": 1}] if posted else []
        return httpx.Response(200, json={"data": rows})

    c, _ = client(handler)
    run(c.upload_document(1, "Lab Report", PDF, **ctx()))
    assert seen["path_param"] == "Categories/Lab_Report"


# ---------------------------------------------------------------- structured records

def test_an_approved_fact_posts_to_the_puuid_route_with_provenance():
    """Guards: losing the link back to the document a fact came from. `comments` is whitelisted for insert and
    reads back out through this same standard API."""
    seen = {}

    def handler(req):
        seen["path"], seen["body"] = req.url.path, json.loads(req.content)
        return httpx.Response(200, json={"data": {"id": 845, "uuid": "new-uuid"}})

    c, _ = client(handler)
    res = run(c.write_record(PUUID, "allergy",
                             {"title": "Penicillin", "comments": "doc=987 page=1 field=allergies[0]"}, **ctx()))
    assert res.ok and res.data["data"]["id"] == 845
    assert seen["path"].endswith(f"/api/patient/{PUUID}/allergy")
    assert "doc=987" in seen["body"]["comments"]


def test_an_unsupported_record_kind_is_refused_before_the_network():
    """Guards: a typo'd kind producing a 404 that reads like a server problem — and pitfall one, a third
    document type arriving before two work."""
    c, _ = client(lambda req: httpx.Response(500))
    res = run(c.write_record(PUUID, "lab_result", {}, **ctx()))
    assert not res.ok and res.detail == "unsupported_kind:lab_result"


# ---------------------------------------------------------------- failure behaviour

def test_an_http_error_is_reported_without_echoing_the_body():
    """Guards: COMP-3. OpenEMR error payloads can reflect request content, which may carry document text."""
    c, _ = client(lambda req: httpx.Response(403, json={"error": "patient Penicillin record detail"}))
    res = run(c.write_record(PUUID, "allergy", {"title": "x"}, **ctx()))
    assert not res.ok and res.status == 403 and res.detail == "http_403"
    assert "Penicillin" not in json.dumps(res._asdict(), default=str)


def test_an_expired_deadline_does_not_start_a_write():
    """Guards: a write firing after the caller gave up, landing a record nobody is waiting to confirm."""
    called = []

    def handler(req):
        called.append(1)
        return httpx.Response(200, json={})
    c, _ = client(handler)
    res = run(c.write_record(PUUID, "allergy", {}, token="t", deadline=Deadline(0.0), correlation_id="cid"))
    assert not res.ok and res.detail == "deadline" and called == []


def test_a_transport_failure_is_a_result_not_an_exception():
    """Guards: an ingest crash surfacing as a 500 instead of a named, retryable outcome."""
    def handler(req):
        raise httpx.ConnectError("refused")
    c, _ = client(handler)
    res = run(c.write_record(PUUID, "allergy", {}, **ctx()))
    assert not res.ok and res.detail == "ConnectError"
