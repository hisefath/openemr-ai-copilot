"""The Week 2 HTTP surface: upload a document, see what was read, decide what reaches the chart.

Kept in its own router rather than added to main.py, which already carries the Week 1 surface. Same boundary
rule as every other module here: this file owns HTTP shapes and nothing else, and calls modules that already
work and are already tested.

The patient always comes from the server-side session, never from the request body. A caller who can reach
these routes at all has a session for exactly one patient, and there is no parameter that can change it.

That last sentence was false for as long as these routes existed, and it is worth saying how rather than
quietly fixing it. `document_id` was such a parameter: it is OpenEMR's document row id, a small sequential
integer, and both caches behind these routes were addressed by it alone. A second session — the same
clinician, legitimately launched from a different chart — could read another patient's rendered pages and
extracted values, and could approve a fact from someone else's document into its own patient's chart, since
the row was looked up by document while the write went to the session's patient.

Both caches are now keyed by (patient_id, document_id) and every staging read takes a patient_id it will not
default. The scope is in the key rather than in a check at each route, because the check is the part a new
route forgets.
"""
from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional, Tuple

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


def _scope(request: Request, authorization: Optional[str]) -> Tuple[Any, str]:
    """The session and the patient every lookup on these routes is scoped to.

    The module docstring claims there is no parameter that can change the patient. document_id was one: it is
    OpenEMR's document row id, a small sequential integer, and the caches behind these routes were addressed by
    it alone. Returning the patient beside the session is what makes forgetting to scope a lookup awkward
    rather than invisible — the value is right there and the store will not accept a read without it.

    patient_id is Optional on Session because schedule sessions have none. Those cannot reach here (the
    resolver already demands kind == "patient"), but a patient session without a patient has no scope to
    enforce, so it is refused rather than keyed on None."""
    session = _session(request, authorization)
    if not session.patient_id:
        raise HTTPException(403, "no_patient_scope: Relaunch the Co-Pilot from a patient chart.")
    return session, session.patient_id


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
    session, patient_id = _scope(request, authorization)
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
    facts = staging.derive(extracted, doc, patient_id=patient_id,
                           confidence=located / total if total else 0.0)
    staged = app.staging.put(facts)

    # PRD §7 requires extraction confidence on the per-encounter log, and it was being computed here and then
    # thrown away — the route returned it to the browser but nothing recorded it. Counts and a ratio only: the
    # confidence is what fraction of what the model read could be located on the page, and the values
    # themselves are PHI. A run of low-confidence documents is the signal that a scan source has degraded.
    log.info("ingest", extra={
        "doc_type": kind.value, "pages": pages.page_count, "truncated": pages.truncated,
        "values_read": total, "values_located": located,
        "extraction_confidence": round(located / total, 3) if total else 0.0,
        "staged": staged, "llm_calls": meta.calls, "llm_cost_usd": round(meta.cost_usd, 6),
        "input_tokens": meta.tokens.get("input", 0), "output_tokens": meta.tokens.get("output", 0),
        "elapsed_s": round(INGEST_DEADLINE_S - deadline.remaining(), 2)})

    # Keyed by patient as well as document: the overlay serves rendered pages of a patient's own chart, and
    # document_id alone is a sequential integer that any live session could name.
    app.pages_cache[(patient_id, doc.document_id)] = pages
    # A later question in this session needs to know a document exists and what was read from it, so the
    # supervisor can route and so its citations are accepted by the verifier.
    app.session_docs[session.session_ref] = {"document": doc, "extracted": extracted, "pages": pages}
    return {
        "document": doc.model_dump(),
        "extraction": extracted.model_dump(),
        "located": located, "total": total, "truncated": pages.truncated,
        "pages": [{"page": i + 1, "width": w, "height": h} for i, (w, h) in enumerate(pages.sizes)],
        "staged": staged, "queue": staging.queue_summary(app.staging, patient_id, doc.document_id),
    }


@router.get("/documents/{document_id}/facts")
async def review_queue(request: Request, document_id: str, authorization: Optional[str] = None):
    _, patient_id = _scope(request, authorization or request.headers.get("authorization"))
    rows = request.app.state.staging.pending(patient_id, document_id)
    return {"document_id": document_id, "facts": [_public(f) for f in rows],
            "summary": staging.queue_summary(request.app.state.staging, patient_id, document_id)}


@router.post("/documents/{document_id}/facts/decision")
async def decide(request: Request, document_id: str, body: Dict[str, str],
                 authorization: Optional[str] = None):
    """Approve or reject one staged fact. Approval is the only path by which anything reaches a chart record."""
    session, patient_id = _scope(request, authorization or request.headers.get("authorization"))
    field_path, decision = body.get("field_path", ""), body.get("decision", "")
    if decision not in ("approve", "reject"):
        raise HTTPException(400, "decision must be approve or reject")

    app = request.app.state
    if decision == "reject":
        row = staging.reject(app.staging, patient_id=patient_id, document_id=document_id,
                             field_path=field_path, who=session.fhir_user)
        if row is None:
            raise HTTPException(404, "not_pending")
        return {"fact": _public(row), "written": False}

    # puuid is the write target and the lookup scope in one value, so the row approved and the chart written
    # to are the same patient's by construction rather than by a check that could drift apart.
    row, res = await staging.approve(app.staging, app.emr_write, puuid=patient_id,
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

    Served from the cache under this session's own patient rather than re-fetched, so a page image can only be
    obtained by a session for the patient it belongs to. A document belonging to anyone else simply does not
    match the key and answers 404, which is also the right answer to give: confirming that a document id
    exists is itself something a guessing caller should not learn. Page renders are never sent to Langfuse
    (§8); this is the browser's copy."""
    _, patient_id = _scope(request, authorization or request.headers.get("authorization"))
    pages = request.app.state.pages_cache.get((patient_id, document_id))
    if pages is None or not (1 <= page <= len(pages.images)):
        raise HTTPException(404, "page_not_available")
    return Response(content=pages.images[page - 1], media_type="image/png",
                    headers={"Cache-Control": "private, max-age=300"})
