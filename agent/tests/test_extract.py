"""Reading a document: what the model says, and what the page confirms.

The half worth testing hardest is `assemble`, which is pure — it decides whether a claim is grounded, and it
does so with no network, no key and no model.
"""
import asyncio
import json

import anthropic
import pytest
from anthropic.types import Message

from copilot.config import Settings
from copilot.deadline import Deadline
from copilot.documents import Pages
from copilot.extract import assemble, located_ratio, output_format, read_document
from copilot.locate import Word
from copilot.schemas import (AbnormalFlag, DocumentRef, DocumentType, IntakeForm, LabReport, SeenAllergy,
                             SeenIntakeForm, SeenLabReport, SeenLabResult, SeenValue, SourceType)

DOC = DocumentRef(document_id="988", doc_type=DocumentType.lab_pdf, content_hash="h", page_count=1)
INTAKE_DOC = DocumentRef(document_id="988", doc_type=DocumentType.intake_form, content_hash="h", page_count=1)


def row(y, *texts, page=1, x0=72.0, width=44.0, height=12.0, gap=8.0):
    out, x = [], x0
    for t in texts:
        out.append(Word(text=t, x0=x, top=y, x1=x + width, bottom=y + height, page=page))
        x += width + gap
    return out


def pages(*word_rows):
    return Pages(images=[], words=[list(word_rows)] if word_rows else [[]], page_count=1, truncated=False)


LAB_PAGE = Pages(images=[], words=[row(100, "Potassium", "5.1", "mmol/L") + row(120, "Creatinine", "0.9")],
                 page_count=1, truncated=False)


def settings():
    return Settings(public_issuer="http://e", fhir_base="http://e", oauth_public_base="http://e",
                    oauth_internal_base="http://e", openemr_public_origin="http://e", client_id="c",
                    client_secret="s", agent_public_url="http://a", hmac_key="k", audit_db_host=None,
                    audit_db_user=None, audit_db_password=None, audit_db_ca=None)


# ---------------------------------------------------------------- the schema handed to the model

@pytest.mark.parametrize("doc_type", list(DocumentType))
def test_the_model_is_never_offered_a_coordinate_field(doc_type):
    """Guards: THE invariant. A bbox the model can fill is a bbox it can get wrong, and a wrong box makes a
    wrong value look better evidenced than an honest gap. It is absent from the schema, not merely ignored."""
    assert "bbox" not in json.dumps(output_format(doc_type))


# ---------------------------------------------------------------- assemble: lab reports

def test_a_value_the_page_confirms_gets_a_box():
    seen = SeenLabReport(results=[SeenLabResult(test_name="Potassium", value="5.1", page=1,
                                                label_on_page="Potassium")])
    out = assemble(seen, DOC, LAB_PAGE)
    assert isinstance(out, LabReport)
    c = out.results[0].citation
    assert c.bbox is not None and c.source_type is SourceType.document and c.source_id == "988"
    assert c.field_or_chunk_id == "results[0].value" and c.quote_or_value == "5.1"


def test_a_value_the_page_cannot_confirm_keeps_its_value_and_loses_its_box():
    """Guards: dropping an unlocatable fact (it is still what the model read) or placing it approximately
    (which would dress a guess as evidence)."""
    seen = SeenLabReport(results=[SeenLabResult(test_name="Sodium", value="139", page=1, label_on_page="Sodium")])
    out = assemble(seen, DOC, LAB_PAGE)
    assert out.results[0].value == "139" and out.results[0].citation.bbox is None


def test_a_wrong_value_on_the_wrong_row_is_not_located():
    """Guards: THE failure locate.py exists for, now end to end. The model misreads creatinine as 5.1 — a value
    that IS on the page, in potassium's row. It must come back unlocated."""
    seen = SeenLabReport(results=[SeenLabResult(test_name="Creatinine", value="5.1", page=1,
                                                label_on_page="Creatinine")])
    assert assemble(seen, DOC, LAB_PAGE).results[0].citation.bbox is None


def test_values_are_carried_through_verbatim():
    """Guards: normalisation creeping in. '<0.01' must survive as '<0.01'."""
    seen = SeenLabReport(results=[SeenLabResult(test_name="TSH", value="<0.01", page=1)])
    assert assemble(seen, DOC, LAB_PAGE).results[0].value == "<0.01"


def test_an_unflagged_result_stays_unknown():
    seen = SeenLabReport(results=[SeenLabResult(test_name="TSH", value="1.2", page=1)])
    assert assemble(seen, DOC, LAB_PAGE).results[0].abnormal_flag is AbnormalFlag.unknown


def test_unreadable_regions_survive_into_the_stored_document():
    """Guards: a gap the model honestly reported being dropped on the way to storage."""
    seen = SeenLabReport(results=[], unreadable_regions=["page 1, lower third smudged"])
    assert assemble(seen, DOC, LAB_PAGE).unreadable_regions == ["page 1, lower third smudged"]


# ---------------------------------------------------------------- assemble: intake forms

def test_every_intake_fact_is_cited_individually():
    """Guards: one citation for the whole form. A physician needs the line, not the document."""
    page = Pages(images=[], words=[row(100, "Allergies", "Penicillin") + row(140, "Concern", "cough")],
                 page_count=1, truncated=False)
    seen = SeenIntakeForm(
        allergies=[SeenAllergy(substance="Penicillin", page=1, label_on_page="Allergies")],
        chief_concern=SeenValue(value="cough", page=1, label_on_page="Concern"),
    )
    out = assemble(seen, INTAKE_DOC, page)
    assert isinstance(out, IntakeForm)
    assert out.allergies[0].citation.bbox is not None
    assert out.chief_concern.citation.bbox is not None
    assert out.allergies[0].citation.field_or_chunk_id == "allergies[0].substance"


def test_a_blank_form_stays_blank():
    """Guards: inventing fields to satisfy the schema. A half-filled form is the normal case."""
    out = assemble(SeenIntakeForm(), INTAKE_DOC, pages())
    assert out.demographics.name is None and out.medications == [] and out.chief_concern is None


def test_an_allergy_with_no_written_reaction_keeps_none():
    page = Pages(images=[], words=[row(100, "Allergies", "Penicillin")], page_count=1, truncated=False)
    seen = SeenIntakeForm(allergies=[SeenAllergy(substance="Penicillin", page=1, label_on_page="Allergies")])
    assert assemble(seen, INTAKE_DOC, page).allergies[0].reaction is None


# ---------------------------------------------------------------- the ratio the gate reads

def test_located_ratio_counts_what_the_page_confirmed():
    """Guards: the gate's value_located rubric and §8's extraction-confidence line drifting apart."""
    seen = SeenLabReport(results=[
        SeenLabResult(test_name="Potassium", value="5.1", page=1, label_on_page="Potassium"),   # locatable
        SeenLabResult(test_name="Sodium", value="139", page=1, label_on_page="Sodium"),         # not on the page
    ])
    assert located_ratio(assemble(seen, DOC, LAB_PAGE)) == (1, 2)


# ---------------------------------------------------------------- the vision call

class FakeVision:
    max_retries = 0

    def __init__(self, reply):
        self._reply, self.calls, self.messages = reply, [], self

    def with_options(self, **_):
        return self

    async def create(self, **kwargs):
        self.calls.append(kwargs)
        if isinstance(self._reply, Exception):
            raise self._reply
        return self._reply


def a_message(text, stop="end_turn"):
    return Message.model_validate({"id": "m", "type": "message", "role": "assistant", "model": "claude-haiku-4-5",
                                   "content": [{"type": "text", "text": text}], "stop_reason": stop,
                                   "stop_sequence": None, "usage": {"input_tokens": 9, "output_tokens": 9}})


def test_pages_are_sent_as_images_with_the_documents_schema():
    """Guards: sending page text instead of pixels, which defeats the point of a vision model on a scan."""
    payload = SeenLabReport(results=[SeenLabResult(test_name="K", value="5.1", page=1)]).model_dump_json()
    fake = FakeVision(a_message(payload))
    parsed, meta = asyncio.run(read_document(fake, settings(), DocumentType.lab_pdf,
                                             [b"\x89PNG-one", b"\x89PNG-two"], Deadline(10.0)))
    assert parsed is not None and meta.reason is None
    content = fake.calls[0]["messages"][0]["content"]
    assert [c["type"] for c in content] == ["image", "image", "text"]
    assert "bbox" not in json.dumps(fake.calls[0]["output_config"])


@pytest.mark.parametrize("reply,reason", [
    (a_message("not json at all"), "unparseable"),
    (a_message("", stop="refusal"), "refusal"),
    (anthropic.APITimeoutError(request=None), "timeout"),
])
def test_every_failure_is_a_named_reason_not_an_exception(reply, reason):
    """Guards: an extraction failure reaching the physician as an error screen instead of a stated gap."""
    parsed, meta = asyncio.run(read_document(FakeVision(reply), settings(), DocumentType.lab_pdf,
                                             [b"png"], Deadline(10.0)))
    assert parsed is None and meta.reason == reason


def test_no_pages_and_no_time_are_refused_before_spending_anything():
    """Guards: paying for a vision call that cannot succeed."""
    parsed, meta = asyncio.run(read_document(FakeVision(None), settings(), DocumentType.lab_pdf,
                                             [], Deadline(10.0)))
    assert parsed is None and meta.reason == "no_pages"
    parsed, meta = asyncio.run(read_document(FakeVision(None), settings(), DocumentType.lab_pdf,
                                             [b"png"], Deadline(0.0)))
    assert parsed is None and meta.reason == "deadline"
