"""Getting a document into the chart and its pixels out.

The upload happens before any extraction, so these tests care most about what happens when it does NOT succeed:
a caller left holding facts with no document to cite is the failure worth preventing.
"""
import asyncio

import httpx
import pytest

from copilot.deadline import Deadline
from copilot.documents import (CATEGORY, IngestError, MAX_BYTES, Pages, locate_value, page_count, read_pages,
                               store)
from copilot.emr_write import EmrWriteClient, content_filename
from copilot.locate import Word
from copilot.schemas import DocumentType

FHIR_BASE = "http://emr.test/apis/default/fhir"
PUUID = "a2c40eca-61ca-4d75-bbee-35756d77a9eb"


def _pdf(*page_texts: str) -> bytes:
    """A real, parseable PDF with one text line per page."""
    objs, kids = [], []
    n = 3
    for text in page_texts:
        content = f"BT /F1 12 Tf 72 720 Td ({text}) Tj ET".encode()
        objs.append(b"%d 0 obj<</Type/Page/Parent 2 0 R/MediaBox[0 0 612 792]/Contents %d 0 R"
                    b"/Resources<</Font<</F1 99 0 R>>>>>>endobj\n" % (n, n + 1))
        objs.append(b"%d 0 obj<</Length %d>>stream\n%s\nendstream endobj\n" % (n + 1, len(content), content))
        kids.append(b"%d 0 R" % n)
        n += 2
    head = (b"%PDF-1.4\n1 0 obj<</Type/Catalog/Pages 2 0 R>>endobj\n"
            b"2 0 obj<</Type/Pages/Kids[" + b" ".join(kids) + b"]/Count %d>>endobj\n" % len(kids))
    tail = b"99 0 obj<</Type/Font/Subtype/Type1/BaseFont/Helvetica>>endobj\ntrailer<</Root 1 0 R>>\n%%EOF\n"
    return head + b"".join(objs) + tail


ONE_PAGE = _pdf("Potassium 5.1 mmol/L Creatinine 0.9 mg/dL")
TWO_PAGE = _pdf("Potassium 5.1 mmol/L", "Sodium 139 mmol/L")


def ctx():
    return dict(token="t", deadline=Deadline(30.0), correlation_id="cid")


def client(handler):
    return EmrWriteClient(httpx.AsyncClient(transport=httpx.MockTransport(handler)), FHIR_BASE)


def run(coro):
    return asyncio.run(coro)


def ok_handler(doc_id=988, prefix="lab"):
    """Resolves a pid, accepts an upload, then lists the document back."""
    posted = []

    def handler(req):
        if req.url.path.endswith(f"/api/patient/{PUUID}"):
            return httpx.Response(200, json={"data": {"pid": 1}})
        if req.method == "POST":
            posted.append(1)
            return httpx.Response(200, json=True)
        rows = [{"filename": content_filename(prefix, ONE_PAGE), "id": doc_id, "hash": "h",
                 "docdate": "2026-09-23"}] if posted else []
        return httpx.Response(200, json={"data": rows})
    return handler


# ---------------------------------------------------------------- storing

def test_storing_returns_the_anchor_every_later_citation_points_at():
    """Guards: extraction proceeding with no document id, producing facts that cite nothing."""
    ref = run(store(client(ok_handler()), puuid=PUUID, doc_type=DocumentType.lab_pdf, data=ONE_PAGE, **ctx()))
    assert ref.document_id == "988" and ref.doc_type is DocumentType.lab_pdf and ref.page_count == 1


def test_both_document_types_have_a_category_that_exists_in_openemr():
    """Guards: an unmapped type silently landing the file in an unreachable category."""
    assert set(CATEGORY) == {DocumentType.lab_pdf, DocumentType.intake_form}
    assert all(CATEGORY.values())


@pytest.mark.parametrize("data,reason", [
    (b"", "empty_document"),
    (b"x" * (MAX_BYTES + 1), "document_too_large"),
])
def test_obviously_bad_input_is_refused_before_any_network_call(data, reason):
    """Guards: an attacker-controlled upload reaching OpenEMR or a per-page vision bill at all. The front desk
    chooses the file; the agent chooses whether to spend anything on it."""
    called = []

    def handler(req):
        called.append(1)
        return httpx.Response(200, json={})

    with pytest.raises(IngestError) as e:
        run(store(client(handler), puuid=PUUID, doc_type=DocumentType.lab_pdf, data=data, **ctx()))
    assert reason in str(e.value) and called == []


def test_an_unresolvable_patient_stops_ingestion():
    """Guards: uploading a document to the wrong pid, or to none. Document routes take the numeric pid."""
    def handler(req):
        return httpx.Response(200, json={"data": {}})       # no pid in the payload
    with pytest.raises(IngestError, match="patient_not_resolvable"):
        run(store(client(handler), puuid=PUUID, doc_type=DocumentType.lab_pdf, data=ONE_PAGE, **ctx()))


def test_a_failed_upload_raises_rather_than_returning_a_half_answer():
    """Guards: the silent orphan reaching the caller as success. A 200 that lists nothing back means the
    document is uncategorised and unciteable, which is not a stored document."""
    def handler(req):
        if req.url.path.endswith(f"/api/patient/{PUUID}"):
            return httpx.Response(200, json={"data": {"pid": 1}})
        if req.method == "POST":
            return httpx.Response(200, json=True)
        return httpx.Response(200, json={"data": []})       # nothing listed back
    with pytest.raises(IngestError, match="uploaded_but_not_listed"):
        run(store(client(handler), puuid=PUUID, doc_type=DocumentType.lab_pdf, data=ONE_PAGE, **ctx()))


def test_re_ingesting_the_same_bytes_does_not_create_a_second_document():
    """Guards: the PRD's 'without creating duplicate or untraceable records'."""
    handler = ok_handler()
    c = client(handler)
    first = run(store(c, puuid=PUUID, doc_type=DocumentType.lab_pdf, data=ONE_PAGE, **ctx()))
    second = run(store(c, puuid=PUUID, doc_type=DocumentType.lab_pdf, data=ONE_PAGE, **ctx()))
    assert first.document_id == second.document_id


# ---------------------------------------------------------------- reading pages

def test_pages_come_back_as_images_and_word_boxes_together():
    """Guards: rendering and word extraction drifting apart, so a box refers to a page that was never shown."""
    pages = read_pages(TWO_PAGE)
    assert pages.page_count == 2 and not pages.truncated
    assert len(pages.images) == 2 and len(pages.words) == 2
    assert all(img.startswith(b"\x89PNG") for img in pages.images)
    assert any(w.text == "Sodium" for w in pages.words[1])


def test_a_long_document_is_truncated_visibly_not_silently():
    """Guards: a 200-page upload quietly costing 200 vision calls, or a half-read document looking fully read."""
    pages = read_pages(TWO_PAGE, max_pages=1)
    assert pages.truncated is True and pages.page_count == 2 and len(pages.images) == 1


def test_page_count_is_the_document_not_what_we_read():
    assert page_count(TWO_PAGE) == 2


# ---------------------------------------------------------------- locating across pages

def _pages(*rows):
    return Pages(images=[], words=list(rows), page_count=len(rows), truncated=False)


def test_a_page_hint_restricts_the_search_to_that_page():
    """Guards: confirming a value on the wrong page, which is not confirmation."""
    p = _pages([Word("5.1", 100, 100, 130, 112, 1)], [Word("5.1", 100, 100, 130, 112, 2)])
    assert locate_value(p, "5.1", page=2).page == 2
    assert locate_value(p, "5.1", page=3) is None


def test_a_value_on_two_pages_without_a_hint_stays_unlocated():
    """Guards: picking the first page. Ambiguity across pages is ambiguity, same as within one."""
    p = _pages([Word("5.1", 100, 100, 130, 112, 1)], [Word("5.1", 100, 100, 130, 112, 2)])
    assert locate_value(p, "5.1") is None


def test_a_value_on_exactly_one_page_is_located_without_a_hint():
    p = _pages([Word("139", 100, 100, 130, 112, 1)], [Word("5.1", 100, 100, 130, 112, 2)])
    assert locate_value(p, "139").page == 1
