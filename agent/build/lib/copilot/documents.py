"""Getting a clinical document into the chart and onto the screen.

One boundary: OpenEMR storage and page rendering. What the pages *say* is `extract.py`; where a value sits is
`locate.py`; what happens to the derived facts is `staging.py`. This module only gets the document in and the
pixels out.

Order matters and is not arbitrary. The source document is uploaded FIRST, before anything is extracted, because
a faithful copy is not a claim — storing the scan the front desk sent is not the agent asserting anything about
the patient. The document id that comes back is then the anchor every later citation points at, so extraction
without a stored document would produce facts with nothing to cite.

Two caps exist because a document is attacker-controlled input in the plainest sense: anyone who can upload to
the front desk chooses its size and page count, and a vision call is billed per page.
"""
from __future__ import annotations

import logging
from typing import Any, List, NamedTuple, Optional, Tuple

from . import locate
from .emr_write import EmrWriteClient
from .schemas import DocumentRef, DocumentType

log = logging.getLogger("agent")

# Where each document type lands in OpenEMR's category tree. Both exist in a stock install; see emr_write's
# category_path for why the spelling matters.
CATEGORY = {
    DocumentType.lab_pdf: "Lab Report",
    DocumentType.intake_form: "Patient Information",
}
MAX_BYTES = 20 * 1024 * 1024   # a scanned page is ~200 KB; 20 MB is a generous lab panel and a hard stop
MAX_PAGES = 10                 # vision is billed per page. Beyond this we extract a prefix and SAY SO (§below)
FILE_PREFIX = {DocumentType.lab_pdf: "lab", DocumentType.intake_form: "intake"}


class IngestError(Exception):
    """Ingestion failed in a way the caller must surface. Never swallowed into a partial success."""


class Pages(NamedTuple):
    """What the rest of the pipeline needs from the file itself."""
    images: List[bytes]                    # PNG per page, for the vision call
    words: List[List[locate.Word]]         # word boxes per page, for locating values
    page_count: int                        # pages in the document
    truncated: bool                        # True when page_count > MAX_PAGES and we only read a prefix
    sizes: List[Tuple[float, float]] = []  # (width, height) in PDF points per page read

    def size_of(self, page: int) -> Tuple[float, float]:
        """Page dimensions in points. The overlay needs these to place a bbox as a percentage of the rendered
        image, rather than assuming every page is US Letter or hard-coding the render scale in the browser."""
        return self.sizes[page - 1] if 1 <= page <= len(self.sizes) else (612.0, 792.0)


def page_count(pdf_bytes: bytes) -> int:
    import pdfplumber
    import io

    with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
        return len(pdf.pages)


def read_pages(pdf_bytes: bytes, max_pages: int = MAX_PAGES) -> Pages:
    """Render pages and collect their word boxes in one pass.

    Both halves are needed together and both are expensive, so they are produced once and handed on rather than
    re-derived per field. `truncated` is returned rather than logged: a document we only half-read must not look
    like a document we fully read, and the answer has to be able to say so."""
    import io
    import pdfplumber

    total = page_count(pdf_bytes)
    take = min(total, max_pages)
    images, words, sizes = [], [], []
    with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
        for n in range(1, take + 1):
            images.append(locate.render_page_png(pdf_bytes, n))
            words.append(locate.page_words(pdf_bytes, n))
            page = pdf.pages[n - 1]
            sizes.append((float(page.width), float(page.height)))
    if total > take:
        log.warning("document_truncated", extra={"pages": total, "read": take})
    return Pages(images=images, words=words, page_count=total, truncated=total > take, sizes=sizes)


async def store(client: EmrWriteClient, *, puuid: str, doc_type: DocumentType, data: bytes,
                **ctx: Any) -> DocumentRef:
    """Upload the source document to OpenEMR and return the reference later citations anchor to.

    Idempotent on content: re-ingesting identical bytes returns the existing document rather than a second copy,
    which is what the PRD means by round-tripping 'without creating duplicate or untraceable records'.

    Raises IngestError rather than returning a half-answer — a caller that got no document id has nothing to
    cite, and continuing would produce facts pointing at nothing."""
    if not data:
        raise IngestError("empty_document")
    if len(data) > MAX_BYTES:
        raise IngestError("document_too_large")
    if doc_type not in CATEGORY:
        raise IngestError(f"unsupported_doc_type:{doc_type}")

    pid = await client.resolve_pid(puuid, **ctx)
    if pid is None:
        # The write routes for documents take the numeric pid; without it there is nowhere to put the file.
        raise IngestError("patient_not_resolvable")

    res = await client.upload_document(pid, CATEGORY[doc_type], data,
                                       prefix=FILE_PREFIX[doc_type], **ctx)
    if not res.ok:
        raise IngestError(res.detail or "upload_failed")

    row = (res.data or {}).get("data") or {}
    doc_id = row.get("id")
    if doc_id is None:
        raise IngestError("no_document_id")

    return DocumentRef(
        document_id=str(doc_id),
        doc_type=doc_type,
        content_hash=str(row.get("hash") or ""),
        page_count=page_count(data),
        uploaded_at=row.get("docdate"),
    )


def locate_value(pages: Pages, value: str, *, page: Optional[int] = None,
                 label: Optional[str] = None):
    """The box for an extracted value, or None when the pages cannot say unambiguously.

    When the extractor reported which page a value came from, only that page is searched — a value confirmed on
    the wrong page is not confirmation. With no page hint, every page is searched and a value found on more than
    one of them stays unlocated, for the same reason `locate` refuses ambiguity within a page."""
    if page is not None:
        if 1 <= page <= len(pages.words):
            return locate.locate(value, pages.words[page - 1], label=label)
        return None

    found = [box for words in pages.words if (box := locate.locate(value, words, label=label)) is not None]
    return found[0] if len(found) == 1 else None
