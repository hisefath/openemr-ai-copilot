"""The review queue.

One property carries this module: a vision model reading a smudged scan cannot alter a chart on its own. The
tests that matter are the ones where that could quietly stop being true — a write that fails but is recorded as
approved, a re-ingest resurrecting a decision, or a fact reaching the chart with no trace of where it came from.
"""
import asyncio

import httpx
import pytest

from copilot.deadline import Deadline
from copilot.emr_write import EmrWriteClient
from copilot.schemas import (Citation, DocumentRef, DocumentType, IntakeAllergy, IntakeForm, IntakeMedication,
                             LabReport, LabResult, SourceType, StagedStatus, BBox)
from copilot.staging import (MemoryStagingStore, approve, derive, provenance, queue_summary, reject)

PUUID = "a2c40eca-61ca-4d75-bbee-35756d77a9eb"
DOC = DocumentRef(document_id="988", doc_type=DocumentType.intake_form, content_hash="h", page_count=1)
LAB_DOC = DocumentRef(document_id="989", doc_type=DocumentType.lab_pdf, content_hash="h", page_count=1)


def cite(field, value, located=True):
    return Citation(source_type=SourceType.document, source_id="988", page_or_section="1",
                    field_or_chunk_id=field, quote_or_value=value,
                    bbox=BBox(page=1, x0=1, y0=2, x1=3, y1=4) if located else None)


def intake():
    return IntakeForm(document_id="988",
                      allergies=[IntakeAllergy(substance="Penicillin", reaction="rash",
                                               citation=cite("allergies[0].substance", "Penicillin"))],
                      medications=[IntakeMedication(name="lisinopril", dose="10mg",
                                                    citation=cite("medications[0].name", "lisinopril"))])


def ctx():
    return dict(token="t", deadline=Deadline(30.0), correlation_id="cid")


def client(handler):
    return EmrWriteClient(httpx.AsyncClient(transport=httpx.MockTransport(handler)), "http://e/apis/default/fhir")


def run(coro):
    return asyncio.run(coro)


# ---------------------------------------------------------------- deriving

def test_every_staged_fact_carries_the_document_page_and_field_it_came_from():
    """Guards: a fact reaching the chart with no way back to the page it was read off."""
    facts = derive(intake(), DOC)
    assert {f.fact_kind for f in facts} == {"allergy", "medication"}
    assert all("doc=988" in f.payload["comments"] for f in facts)
    assert provenance(cite("allergies[0].substance", "x")) == "doc=988 page=1 field=allergies[0].substance"


def test_facts_start_pending_and_are_keyed_for_idempotency():
    f = derive(intake(), DOC)[0]
    assert f.status is StagedStatus.pending and f.document_id == "988" and f.field_path


def test_an_unlocated_fact_cannot_carry_high_confidence():
    """Guards: a fact the page could not confirm being sorted to the top of the queue as if it were solid."""
    form = IntakeForm(document_id="988", allergies=[
        IntakeAllergy(substance="Sulfa", citation=cite("allergies[0].substance", "Sulfa", located=False))])
    assert derive(form, DOC, confidence=0.95)[0].confidence <= 0.5


def test_lab_results_are_derived_but_have_no_write_route():
    """Guards: quietly dropping lab values because they cannot be written. They stage and stay cited."""
    report = LabReport(document_id="989", results=[
        LabResult(test_name="Potassium", value="5.1", citation=cite("results[0].value", "5.1"))])
    facts = derive(report, LAB_DOC)
    assert len(facts) == 1 and facts[0].fact_kind == "lab"


# ---------------------------------------------------------------- the queue

def test_re_ingesting_the_same_document_does_not_duplicate_the_queue():
    store = MemoryStagingStore()
    assert store.put(derive(intake(), DOC)) == 2
    assert store.put(derive(intake(), DOC)) == 0
    assert len(store.pending()) == 2


def test_a_re_ingest_cannot_resurrect_a_decision_already_made():
    """Guards: THE subtle one. A clinician rejects a fact; the front desk re-uploads the same scan; the fact
    must not reappear as pending for someone else to approve."""
    store = MemoryStagingStore()
    store.put(derive(intake(), DOC))
    reject(store, document_id="988", field_path="allergies[0].substance", who="dr_chen")
    store.put(derive(intake(), DOC))
    assert [f.field_path for f in store.pending()] == ["medications[0].name"]


def test_deciding_twice_does_not_overwrite_the_first_decision():
    store = MemoryStagingStore()
    store.put(derive(intake(), DOC))
    first = reject(store, document_id="988", field_path="allergies[0].substance", who="dr_chen")
    second = reject(store, document_id="988", field_path="allergies[0].substance", who="someone_else")
    assert first is not None and second is None


def test_a_rejected_fact_is_kept_not_deleted():
    """Guards: throwing away the most informative thing the pipeline produces — a labelled example of the model
    being wrong, which is an eval case."""
    store = MemoryStagingStore()
    store.put(derive(intake(), DOC))
    row = reject(store, document_id="988", field_path="allergies[0].substance", who="dr_chen")
    assert row.status is StagedStatus.rejected and row.decided_by == "dr_chen" and row.decided_at


def test_the_summary_is_counts_only():
    """Guards: clinical values reaching logs or traces through the queue summary (COMP-3)."""
    store = MemoryStagingStore()
    store.put(derive(intake(), DOC))
    s = queue_summary(store)
    assert s == {"pending": 2, "located": 2, "writable": 2}
    assert "Penicillin" not in str(s)


# ---------------------------------------------------------------- approval

def test_approval_writes_to_the_chart_then_records_the_decision():
    seen = {}

    def handler(req):
        seen["path"] = req.url.path
        seen["body"] = req.content.decode()
        return httpx.Response(200, json={"data": {"id": 846}})

    store = MemoryStagingStore()
    store.put(derive(intake(), DOC))
    row, res = run(approve(store, client(handler), puuid=PUUID, document_id="988",
                           field_path="allergies[0].substance", who="dr_chen", **ctx()))
    assert res.ok and row.status is StagedStatus.approved and row.decided_by == "dr_chen"
    assert seen["path"].endswith("/allergy") and "doc=988" in seen["body"]
    assert [f.field_path for f in store.pending()] == ["medications[0].name"]


def test_a_failed_write_leaves_the_fact_pending():
    """Guards: THE thing a review queue must never do — claim a record reached the chart when it did not."""
    store = MemoryStagingStore()
    store.put(derive(intake(), DOC))
    row, res = run(approve(store, client(lambda r: httpx.Response(403)), puuid=PUUID, document_id="988",
                           field_path="allergies[0].substance", who="dr_chen", **ctx()))
    assert not res.ok and row.status is StagedStatus.pending
    assert len(store.pending()) == 2


def test_approving_a_lab_value_reports_that_there_is_nowhere_to_write_it():
    """Guards: a silent no-op. OpenEMR has no lab-result write route, and the clinician must be told that
    rather than left believing the value landed."""
    store = MemoryStagingStore()
    report = LabReport(document_id="989", results=[
        LabResult(test_name="Potassium", value="5.1", citation=cite("results[0].value", "5.1"))])
    store.put(derive(report, LAB_DOC))
    row, res = run(approve(store, client(lambda r: httpx.Response(200, json={})), puuid=PUUID,
                           document_id="989", field_path="results[0].value", who="dr_chen", **ctx()))
    assert not res.ok and res.detail == "no_write_route:lab" and row.status is StagedStatus.pending


def test_approving_something_that_is_not_pending_is_a_no_op():
    store = MemoryStagingStore()
    row, res = run(approve(store, client(lambda r: httpx.Response(200, json={})), puuid=PUUID,
                           document_id="988", field_path="nope", who="dr_chen", **ctx()))
    assert row is None and res is None
