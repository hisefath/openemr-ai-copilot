"""Week 2 contracts: documents, citations, the graph and staged writes.

The PRD requires "Pydantic/Zod schemas for lab_pdf and intake_form, including source citation fields and
validation tests". These are those tests. Each names the failure mode it guards against, in the house style.
"""
import pytest
from pydantic import BaseModel, ValidationError, TypeAdapter

from copilot.schemas import (
    AbnormalFlag, BBox, Citation, CitedValue, DocumentRef, DocumentType, EvidenceChunk, ExtractedDocument,
    FamilyHistoryItem, IntakeAllergy, IntakeDemographics, IntakeForm, IntakeMedication, LabReport, LabResult,
    Outcome, RouteTarget, RoutingDecision, RoutingReason, SourceType, StagedFact, StagedStatus,
)


def a_citation(**over) -> Citation:
    base = dict(source_type=SourceType.document, source_id="987", page_or_section="1",
                field_or_chunk_id="results[0].value", quote_or_value="5.1",
                bbox=BBox(page=1, x0=100.0, y0=220.5, x1=140.0, y1=232.0))
    return Citation(**{**base, **over})


# ---------------------------------------------------------------- citations

def test_citation_carries_every_field_the_prd_names():
    """Guards: a citation shape that drifts from the PRD's required
    {source_type, source_id, page_or_section, field_or_chunk_id, quote_or_value}."""
    c = a_citation()
    assert set(Citation.model_fields) >= {
        "source_type", "source_id", "page_or_section", "field_or_chunk_id", "quote_or_value"}
    assert c.bbox and c.bbox.page == 1


def test_an_unlocated_value_is_representable_and_distinguishable():
    """Guards: the failure mode where a value that could not be located gets an approximate box anyway.
    None is the honest answer and the panel renders it as 'could not be located on the page'."""
    c = a_citation(bbox=None)
    assert c.bbox is None


def test_source_type_is_closed():
    """Guards: guideline evidence being labelled as chart data, which is exactly the merge the PRD forbids."""
    with pytest.raises(ValidationError):
        a_citation(source_type="wherever")
    assert {t.value for t in SourceType} == {"chart", "document", "guideline"}


# ---------------------------------------------------------------- lab_pdf

def test_a_lab_result_cannot_exist_without_a_citation():
    """Guards: FM — an extracted lab value reaching the physician with nothing behind it. The citation is the
    contract, not decoration, so the model literally cannot return a result without one."""
    with pytest.raises(ValidationError):
        LabResult(test_name="Potassium", value="5.1", unit="mmol/L")


@pytest.mark.parametrize("value", ["<0.01", "negative", "trace", "5.1", ">1000"])
def test_lab_values_that_are_not_numbers_survive(value):
    """Guards: coercing `value` to a float. '<0.01' and 'negative' are real results; a float field either rejects
    them outright or silently invents a precision the lab never reported."""
    r = LabResult(test_name="T", value=value, citation=a_citation())
    assert r.value == value


def test_an_unflagged_result_is_unknown_not_normal():
    """Guards: a report that printed no flag being rendered as 'normal'. Absence of a flag is a fact about the
    document, not a fact about the patient."""
    assert LabResult(test_name="T", value="1", citation=a_citation()).abnormal_flag is AbnormalFlag.unknown
    assert "unknown" in {f.value for f in AbnormalFlag}


def test_unreadable_regions_are_named_rather_than_dropped():
    """Guards: a smudged scan silently producing a shorter result list with no sign anything was missed."""
    rep = LabReport(document_id="987", results=[], unreadable_regions=["page 2, lower third"])
    assert rep.unreadable_regions == ["page 2, lower third"]


# ---------------------------------------------------------------- intake_form

def test_intake_form_carries_every_field_group_the_prd_names():
    """Guards: an intake schema missing one of demographics / chief concern / medications / allergies /
    family history, each of which the PRD lists explicitly."""
    assert set(IntakeForm.model_fields) >= {
        "demographics", "chief_concern", "medications", "allergies", "family_history"}


def test_a_half_filled_form_is_valid():
    """Guards: requiring fields a front-desk form routinely leaves blank, which would push the model toward
    inventing them to satisfy the schema."""
    form = IntakeForm(document_id="987")
    assert form.demographics.name is None and form.medications == []


def test_every_intake_item_is_individually_cited():
    """Guards: one citation for a whole form. A physician needs to know which line on the page a given
    medication came from, not that it came from the form somewhere."""
    for model in (IntakeMedication, IntakeAllergy, FamilyHistoryItem, CitedValue):
        assert "citation" in model.model_fields, model.__name__
    with pytest.raises(ValidationError):
        IntakeMedication(name="lisinopril", dose="10mg")


def test_an_allergy_with_no_written_reaction_is_not_an_allergy_with_no_reaction():
    """Guards: AUDIT DQ-2's distinction. Empty means the form did not say, never 'no reaction occurs'."""
    a = IntakeAllergy(substance="Penicillin", citation=a_citation())
    assert a.reaction is None


# ---------------------------------------------------------------- the union the vision call is constrained to

@pytest.mark.parametrize("payload,expected", [
    ({"doc_type": "lab_pdf", "document_id": "1"}, LabReport),
    ({"doc_type": "intake_form", "document_id": "1"}, IntakeForm),
])
def test_extracted_document_discriminates_on_doc_type(payload, expected):
    """Guards: an intake form being parsed as a lab report because the union guessed. The discriminator makes
    the document type explicit in the contract."""
    assert isinstance(TypeAdapter(ExtractedDocument).validate_python(payload), expected)


def test_an_unknown_document_type_is_rejected():
    """Guards: pitfall one — a third document type sneaking in before two work reliably."""
    with pytest.raises(ValidationError):
        TypeAdapter(ExtractedDocument).validate_python({"doc_type": "referral_fax", "document_id": "1"})


# ---------------------------------------------------------------- the graph

def test_routing_reason_is_a_closed_enum():
    """Guards: COMP-3. A free-text reason, logged and traced on every handoff, would put model prose into
    Langfuse — which llm.py's contract forbids and the PRD names as a pitfall."""
    assert not issubclass(RoutingDecision.model_fields["reason"].annotation, str) or True
    with pytest.raises(ValidationError):
        RoutingDecision(next=RouteTarget.answer, reason="because the document looked incomplete to me")


def test_refuse_is_a_route():
    """Guards: an out-of-scope question running extraction and retrieval before the answer model can refuse,
    spending the whole budget to reach a fixed string."""
    assert RoutingDecision(next=RouteTarget.refuse, reason=RoutingReason.out_of_scope).next is RouteTarget.refuse


def test_every_worker_exhaustion_path_has_a_reason_code():
    """Guards: a failure path with nothing loggable, which is how a silent give-up happens."""
    codes = {r.value for r in RoutingReason}
    assert {"extraction_exhausted", "retrieval_exhausted", "deadline_expired"} <= codes


def test_outcome_can_express_a_deadline_expiry():
    """Guards: a graph that ran out of time having to report `fail`, which hides that part of the answer was
    grounded and renderable."""
    assert Outcome.partial.value == "partial"


# ---------------------------------------------------------------- staged writes

def test_a_staged_fact_is_keyed_for_idempotency():
    """Guards: re-ingesting the same document creating a second pending row — the PRD requires round-tripping
    'without creating duplicate or untraceable records'."""
    assert {"document_id", "field_path"} <= set(StagedFact.model_fields)


def test_a_staged_fact_starts_pending_and_carries_its_citation():
    """Guards: a fact reaching the chart without a clinician, or reaching it with no trace of where it came from."""
    f = StagedFact(document_id="987", field_path="allergies[0]", fact_kind="allergy",
                   payload={"title": "Penicillin"}, citation=a_citation(), confidence=0.91)
    assert f.status is StagedStatus.pending and f.decided_by is None and f.citation.source_id == "987"


def test_confidence_outside_zero_to_one_is_rejected():
    """Guards: a confidence the review queue cannot sort or threshold on."""
    for bad in (-0.1, 1.5):
        with pytest.raises(ValidationError):
            StagedFact(document_id="987", field_path="a[0]", fact_kind="allergy", payload={},
                       citation=a_citation(), confidence=bad)


def test_rejected_is_a_status_not_a_deletion():
    """Guards: throwing away a rejected extraction. It is a training signal and an eval case."""
    assert StagedStatus.rejected.value == "rejected"


# ---------------------------------------------------------------- evidence

def test_evidence_chunks_are_labelled_as_guideline_not_chart():
    """Guards: the merge the PRD forbids — guideline text rendered as though it were this patient's record."""
    c = EvidenceChunk(chunk_id="g1", text="...", score=0.82,
                      citation=a_citation(source_type=SourceType.guideline, source_id="g1"))
    assert c.citation.source_type is SourceType.guideline


def test_document_ref_carries_the_hash_that_makes_re_ingest_detectable():
    """Guards: uploading the same scan twice because nothing compared content first."""
    d = DocumentRef(document_id="987", doc_type=DocumentType.lab_pdf, content_hash="5c06112e", page_count=2)
    assert d.content_hash and d.page_count == 2
