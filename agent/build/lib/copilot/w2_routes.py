"""The Week 2 HTTP surface: upload a document, see what was read, decide what reaches the chart.

Kept in its own router rather than added to main.py, which already carries the Week 1 surface. Same boundary
rule as every other module here: this file owns HTTP shapes and nothing else, and calls modules that already
work and are already tested.

The patient always comes from the server-side session, never from the request body. A caller who can reach
these routes at all has a session for exactly one patient, and there is no parameter that can change it.
"""
from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, File, Form, HTTPException, Request, Response, UploadFile

from . import documents, extract, staging
from .deadline import Deadline
from .schemas import DocumentType, StagedStatus

log = logging.getLogger("agent")
router = APIRouter(prefix="/api/session", tags=["week2"])

INGEST_DEADLINE_S = 90.0   # not the 9 s question budget: a vision call over a multi-page scan cannot fit in it


def _ctx(request: Request, session, deadline: Deadline) -> Dict[str, Any]:
    from . import observability as obs
    return {"token": session.access_token, "deadline": deadline, "correlation_id": obs.correlation_id.get()}


def _session(request: Request, authorization: Optional[str]):
    return request.app.state.session_resolver(request, authorization, "patient")


def _public(fact) -> Dict[str, Any]:
    """What the panel is allowed to see about a staged fact. Values included — this is the review queue, and a
    clinician cannot approve what they cannot read — but never the session handle or the access token."""
    return {
        "document_id": fact.document_id,
        "field_path": fact.field_path,
        "fact_kind": fact.fact_kind,
        "payload": fact.payload,
        "status": fact.status.value,
        "confidence": fact.confidence,
        "located": fact.citation.bbox is not None,
        "citation": fact.citation.model_dump(),
    }


@router.post("/documents")
async def attach_and_extract(request: Request, file: UploadFile = File(...), doc_type: str = Form(...),
                             authorization: Optional[str] = None):
    """Store the document, read it, locate what was read, and stage the facts for review.

    Order is deliberate and is the design's central claim in one function: the source document is stored FIRST,
    because a faithful copy is not an assertion about the patient; extraction happens second; and nothing
    reaches a chart record here at all. Only an approval does that."""
    authorization = authorization or request.headers.get("authorization")
    session = _session(request, authorization)
    try:
        kind = DocumentType(doc_type)
    except ValueError:
        raise HTTPException(400, f"unsupported_doc_type: {doc_type}")

    data = await file.read()
    deadline = Deadline(INGEST_DEADLINE_S)
    app = request.app.state

    try:
        doc = await documents.store(app.emr_write, puuid=session.patient_id, doc_type=kind, data=data,
                                    **_ctx(request, session, deadline))
    except documents.IngestError as e:
        raise HTTPException(400, f"ingest_failed: {e}")

    pages = documents.read_pages(data)
    seen, meta = await extract.read_document(app.llm, app.settings, kind, pages.images, deadline)
    if seen is None:
        # A failed read is a stated outcome, not an error screen: the document IS stored and citable, and the
        # panel says extraction did not complete rather than implying the upload failed.
        return {"document": doc.model_dump(), "extraction": None, "reason": meta.reason,
                "truncated": pages.truncated, "staged": 0}

    extracted = extract.assemble(seen, doc, pages)
    located, total = extract.located_ratio(extracted)
    facts = staging.derive(extracted, doc, confidence=located / total if total else 0.0)
    staged = app.staging.put(facts)

    app.pages_cache[doc.document_id] = pages     # so the overlay can serve the page it drew boxes on
    # A later question in this session needs to know a document exists and what was read from it, so the
    # supervisor can route and so its citations are accepted by the verifier.
    app.session_docs[session.session_ref] = {"document": doc, "extracted": extracted, "pages": pages}
    return {
        "document": doc.model_dump(),
        "extraction": extracted.model_dump(),
        "located": located, "total": total, "truncated": pages.truncated,
        "pages": [{"page": i + 1, "width": w, "height": h} for i, (w, h) in enumerate(pages.sizes)],
        "staged": staged, "queue": staging.queue_summary(app.staging, doc.document_id),
    }


@router.get("/documents/{document_id}/facts")
async def review_queue(request: Request, document_id: str, authorization: Optional[str] = None):
    _session(request, authorization or request.headers.get("authorization"))
    rows = request.app.state.staging.pending(document_id)
    return {"document_id": document_id, "facts": [_public(f) for f in rows],
            "summary": staging.queue_summary(request.app.state.staging, document_id)}


@router.post("/documents/{document_id}/facts/decision")
async def decide(request: Request, document_id: str, body: Dict[str, str],
                 authorization: Optional[str] = None):
    """Approve or reject one staged fact. Approval is the only path by which anything reaches a chart record."""
    session = _session(request, authorization or request.headers.get("authorization"))
    field_path, decision = body.get("field_path", ""), body.get("decision", "")
    if decision not in ("approve", "reject"):
        raise HTTPException(400, "decision must be approve or reject")

    app = request.app.state
    if decision == "reject":
        row = staging.reject(app.staging, document_id=document_id, field_path=field_path,
                             who=session.fhir_user)
        if row is None:
            raise HTTPException(404, "not_pending")
        return {"fact": _public(row), "written": False}

    row, res = await staging.approve(app.staging, app.emr_write, puuid=session.patient_id,
                                     document_id=document_id, field_path=field_path, who=session.fhir_user,
                                     **_ctx(request, session, Deadline(INGEST_DEADLINE_S)))
    if row is None:
        raise HTTPException(404, "not_pending")
    if res is None or not res.ok:
        # The fact stays pending. Saying "approved" for a record the chart never received is the one thing this
        # queue must never do, so the failure is reported and the row is left for another try.
        return {"fact": _public(row), "written": False, "reason": res.detail if res else "unknown"}
    return {"fact": _public(row), "written": True, "record": (res.data or {}).get("data")}


@router.get("/documents/{document_id}/page/{page}.png")
async def page_image(request: Request, document_id: str, page: int, authorization: Optional[str] = None):
    """The rendered page the overlay draws boxes on.

    Served from this session's own cache rather than re-fetched, so a page image can only be obtained by the
    session that uploaded it. Page renders are never sent to Langfuse (§8); this is the browser's copy."""
    _session(request, authorization or request.headers.get("authorization"))
    pages = request.app.state.pages_cache.get(document_id)
    if pages is None or not (1 <= page <= len(pages.images)):
        raise HTTPException(404, "page_not_available")
    return Response(content=pages.images[page - 1], media_type="image/png",
                    headers={"Cache-Control": "private, max-age=300"})
