"""The review queue: where a fact read off a document waits for a clinician.

The property this protects is short. **A vision model reading a smudged scan cannot alter a chart on its own.**
Extraction writes here; only a clinician's approval writes to OpenEMR.

That is not the same as refusing to write. Staging is a control ON writing, not a substitute for it — the
approval path is real, exercised, and demonstrated, and a fact that is approved lands in the chart carrying the
document id, page and field it came from. A review queue nobody can act on would be an excuse dressed as a
safeguard.

WHY THIS TABLE IS NOT THE AUDIT TABLE

`deploy/sql/copilot_audit.sql` grants the agent INSERT and nothing else — no SELECT, no UPDATE — because an audit
log that the writer can amend is not evidence. Staging needs SELECT to render the queue and UPDATE to record a
decision, and unlike the audit log it holds clinical values and clears rows once resolved rather than retaining
them for six years. Different rights, different retention, different data: its own table, its own DDL, its own
grant. MySQL grants are per-table, so the connection machinery is reused; a second grant, not a second user.

Rejected rows are kept, not deleted. A rejected extraction is the most informative thing the pipeline produces:
it is a labelled example of the model being wrong, which is an eval case.
"""
from __future__ import annotations

import json
import logging
import time
from typing import Any, Dict, List, Optional, Protocol, Sequence, Tuple

from . import extract as extract_mod
from .emr_write import EmrWriteClient, WriteResult
from .schemas import (Citation, DocumentRef, IntakeForm, LabReport, StagedFact, StagedStatus)

log = logging.getLogger("agent")

# Which staged facts have somewhere to go. Lab values deliberately do not: OpenEMR 8.5 has no lab-result write
# route at all (procedures are GET-only, there is no FHIR Observation write), so they stay staged and cited
# against the document. That is an upstream limitation, stated rather than worked around.
WRITABLE = ("allergy", "medication", "medical_problem")


class StagingUnavailable(Exception):
    """The queue could not be read or written. Surfaced, never swallowed: a fact that silently failed to stage
    is a fact the clinician will never be asked about."""


def provenance(citation: Citation) -> str:
    """The line that travels into the chart record's `comments`, and back out of it on read.

    Deliberately terse and machine-parseable: it has to survive a round trip through a free-text column and
    still point at one field on one page of one document."""
    return f"doc={citation.source_id} page={citation.page_or_section} field={citation.field_or_chunk_id}"


def derive(extracted: Any, doc: DocumentRef, *, confidence: float = 0.0) -> List[StagedFact]:
    """Turn an extracted document into the facts a clinician will be asked to approve.

    Keyed on (document_id, field_path) so re-ingesting the same document cannot produce a second pending row for
    the same fact — the PRD's 'without creating duplicate or untraceable records' applies to derived facts, not
    only to the file."""
    facts: List[StagedFact] = []

    def add(kind: str, payload: Dict[str, str], citation: Citation) -> None:
        facts.append(StagedFact(
            document_id=doc.document_id, field_path=citation.field_or_chunk_id, fact_kind=kind,
            payload={**payload, "comments": provenance(citation)}, citation=citation,
            confidence=confidence if citation.bbox is not None else min(confidence, 0.5),
        ))

    if isinstance(extracted, IntakeForm):
        for a in extracted.allergies:
            add("allergy", {"title": a.substance, **({"reaction": a.reaction} if a.reaction else {})}, a.citation)
        for m in extracted.medications:
            add("medication", {"drug": m.name, **({"dosage": m.dose} if m.dose else {})}, m.citation)
    elif isinstance(extracted, LabReport):
        for r in extracted.results:
            add("lab", {"test": r.test_name, "value": r.value, **({"unit": r.unit} if r.unit else {})},
                r.citation)
    return facts


class StagingStore(Protocol):
    def put(self, facts: Sequence[StagedFact]) -> int: ...
    def pending(self, document_id: Optional[str] = None) -> List[StagedFact]: ...
    def decide(self, document_id: str, field_path: str, status: StagedStatus, who: str) -> Optional[StagedFact]: ...


class MemoryStagingStore:
    """Keyed exactly as the table is, so behaviour here and in MySQL cannot drift."""

    def __init__(self) -> None:
        self._rows: Dict[Tuple[str, str], StagedFact] = {}

    def put(self, facts: Sequence[StagedFact]) -> int:
        added = 0
        for f in facts:
            key = (f.document_id, f.field_path)
            if key in self._rows:      # idempotent: re-ingest must not resurrect a decision already made
                continue
            self._rows[key] = f
            added += 1
        return added

    def pending(self, document_id: Optional[str] = None) -> List[StagedFact]:
        return [f for (doc, _), f in sorted(self._rows.items())
                if f.status is StagedStatus.pending and (document_id is None or doc == document_id)]

    def decide(self, document_id: str, field_path: str, status: StagedStatus, who: str) -> Optional[StagedFact]:
        row = self._rows.get((document_id, field_path))
        if row is None or row.status is not StagedStatus.pending:
            return None            # deciding twice is not an error, but it must not overwrite the first decision
        updated = row.model_copy(update={"status": status, "decided_by": who,
                                         "decided_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())})
        self._rows[(document_id, field_path)] = updated
        return updated


async def approve(store: StagingStore, client: EmrWriteClient, *, puuid: str, document_id: str,
                  field_path: str, who: str, **ctx: Any) -> Tuple[Optional[StagedFact], Optional[WriteResult]]:
    """A clinician accepts a fact: write it to the chart, and only then record the decision.

    Order matters. Marking approved before the write succeeds would leave the queue claiming a record reached
    the chart when it did not, which is the one thing a review queue must never do."""
    row = next((f for f in store.pending(document_id) if f.field_path == field_path), None)
    if row is None:
        return None, None
    if row.fact_kind not in WRITABLE:
        # Nothing to write to. The fact stays pending and cited against the document, and the UI says why.
        return row, WriteResult(False, None, detail=f"no_write_route:{row.fact_kind}")

    res = await client.write_record(puuid, row.fact_kind, dict(row.payload), **ctx)
    if not res.ok:
        return row, res
    return store.decide(document_id, field_path, StagedStatus.approved, who), res


def reject(store: StagingStore, *, document_id: str, field_path: str, who: str) -> Optional[StagedFact]:
    """A clinician rejects a fact. Nothing is written, and the row is kept — see the module docstring."""
    return store.decide(document_id, field_path, StagedStatus.rejected, who)


def queue_summary(store: StagingStore, document_id: Optional[str] = None) -> Dict[str, int]:
    """Counts only. Safe to log and trace; the values themselves never leave the panel."""
    rows = store.pending(document_id)
    return {"pending": len(rows),
            "located": sum(1 for r in rows if r.citation.bbox is not None),
            "writable": sum(1 for r in rows if r.fact_kind in WRITABLE)}
