"""Opening the citation gate to Week 2 sources without opening it to invention.

Week 1's rule: a rendered claim may only cite a record the server already holds. Week 2 adds two more kinds of
source, so the gate has to accept ids that are not FHIR ids — and the whole risk of that change is accidentally
turning a membership check into a regex.
"""
import pytest

from copilot import render
from copilot.schemas import (AnswerPlan, Citation, DocumentRef, DocumentType, EvidenceChunk, Intent, LabReport,
                             LabResult, RecordItem, Section, SourceType)
from copilot.verify import MALFORMED_ID, _denied

DOC = DocumentRef(document_id="988", doc_type=DocumentType.lab_pdf, content_hash="h", page_count=1)


def doc_citation(field="results[0].value", value="5.1"):
    return Citation(source_type=SourceType.document, source_id="988", page_or_section="1",
                    field_or_chunk_id=field, quote_or_value=value)


def guideline_chunk(cid="pen-01"):
    return EvidenceChunk(chunk_id=cid, text="Amoxicillin is a penicillin-class antibiotic.", title="Penicillin",
                         section="Cross-reactivity", score=0.9,
                         citation=Citation(source_type=SourceType.guideline, source_id=cid,
                                           page_or_section="Penicillin", field_or_chunk_id=cid,
                                           quote_or_value="…"))


def extracted_report():
    return LabReport(document_id="988",
                     results=[LabResult(test_name="Potassium", value="5.1", citation=doc_citation())])


def plan_citing(*ids):
    return AnswerPlan(intent=Intent.brief,
                      items=[RecordItem(kind="record", source_id=i, section=Section.safety) for i in ids])


# ---------------------------------------------------------------- namespacing

def test_a_document_field_and_a_chart_record_can_never_collide():
    """Guards: a document whose id happens to match a FHIR record id silently authorising the wrong citation."""
    chart = Citation(source_type=SourceType.chart, source_id="AllergyIntolerance/988", page_or_section="-",
                     field_or_chunk_id="-", quote_or_value="-")
    assert render.citation_key(doc_citation()) == "doc:988:results[0].value"
    assert render.citation_key(chart) == "AllergyIntolerance/988"
    assert render.citation_key(guideline_chunk().citation) == "guideline:pen-01"


def test_the_index_is_built_from_what_the_server_holds():
    """Guards: an index derived from the model's output, which would make the gate circular."""
    idx = render.evidence_index(extracted_report(), [guideline_chunk()])
    assert set(idx) == {"doc:988:results[0].value", "guideline:pen-01"}


# ---------------------------------------------------------------- the gate

def test_a_week_two_citation_the_server_holds_is_accepted():
    """Guards: the MALFORMED_ID regression — before this change every Week 2 citation was withheld, so a
    correctly grounded document fact rendered as nothing at all."""
    idx = render.evidence_index(extracted_report(), [guideline_chunk()])
    assert _denied(plan_citing("doc:988:results[0].value", "guideline:pen-01"), None, {}, idx) == []


def test_an_invented_document_id_of_the_right_shape_is_still_denied():
    """Guards: THE risk of this change. 'doc:988:results[7].value' is perfectly well-formed and refers to a
    field that does not exist. Shape must never be sufficient."""
    idx = render.evidence_index(extracted_report(), [])
    assert _denied(plan_citing("doc:988:results[7].value"), None, {}, idx) == ["doc:988:results[7].value"]


def test_an_invented_guideline_chunk_is_still_denied():
    """Guards: the model citing a plausible-sounding guideline that was never retrieved this turn."""
    idx = render.evidence_index(None, [guideline_chunk("pen-01")])
    assert _denied(plan_citing("guideline:pen-99"), None, {}, idx) == ["guideline:pen-99"]


def test_a_chunk_retrieved_on_a_previous_turn_is_not_citable_now():
    """Guards: evidence leaking across turns. The index is this turn's retrieval, not a growing allowlist."""
    assert _denied(plan_citing("guideline:pen-01"), None, {}, render.evidence_index(None, [])) == \
        ["guideline:pen-01"]


def test_a_bare_document_id_is_malformed_not_merely_unknown():
    """Guards: leaking raw model text into an audit row. An un-namespaced id is not a shape we issue, so it is
    recorded as the fixed marker rather than verbatim."""
    assert _denied(plan_citing("988"), None, {}, render.evidence_index(extracted_report(), [])) == [MALFORMED_ID]


@pytest.mark.parametrize("bad", ["doc:988", "guideline:", "doc::x", "Potassium is 5.1 mmol/L"])
def test_anything_that_is_not_a_shape_we_issue_becomes_the_fixed_marker(bad):
    """Guards: PHI reaching audit rows through a cited id (COMP-3)."""
    assert _denied(plan_citing(bad), None, {}, {}) == [MALFORMED_ID]


# ---------------------------------------------------------------- week 1 is unchanged

def test_week_one_citations_behave_exactly_as_before():
    """Guards: a regression in the Week 1 gate while widening it. A held record passes, an invented one is
    denied by its real id, and chart text is still a marker."""
    index = {"AllergyIntolerance/abc": ("allergies", object())}
    assert _denied(plan_citing("AllergyIntolerance/abc"), None, index) == []
    assert _denied(plan_citing("AllergyIntolerance/nope"), None, index) == ["AllergyIntolerance/nope"]
    assert _denied(plan_citing("penicillin allergy"), None, index) == [MALFORMED_ID]


def test_omitting_the_week_two_index_changes_nothing_for_week_one_callers():
    """Guards: the new parameter altering behaviour for every existing call site."""
    index = {"AllergyIntolerance/abc": ("allergies", object())}
    assert _denied(plan_citing("AllergyIntolerance/abc"), None, index, None) == []
