"""Where on the page a value actually appears.

The model reads the page and returns values. This module, independently, asks the page where those values are.
Coordinates never come from the model: a vision model is unreliable at precise boxes, and a drifting box is a
citation that points at the wrong thing while looking authoritative — which would make the model the source of
its own proof, the exact pattern Week 1 exists to avoid.

WHAT LOCATING DOES AND DOES NOT PROVE

Finding "5.1" on the page proves the string is there. It does not prove it is the "5.1" the model meant. A lab
row reads:

    Potassium   5.1   mmol/L   (3.5-5.1)

and "5.1" appears twice in it — once as the result, once inside potassium's own reference range — before we even
consider other rows. If the model extracts `Creatinine = 5.1`, a naive search attaches a confident box to real
ink and makes a wrong claim look *better evidenced* than an unlocated one.

So a match must survive two constraints:

  1. ROW. When the caller knows the field's label, the value must sit on the same visual row as that label.
     PDFs do not store rows, so a row is reconstructed from vertical overlap.
  2. UNIQUENESS. If more than one candidate survives, we return None. Ambiguity is reported as "could not be
     located", never resolved by picking the first.

Returning None is a real answer here, not a failure: §4 renders it as "extracted, could not be located on the
page", which is what makes an unsupported extraction visible.
"""
from __future__ import annotations

import io
import logging
import re
import shutil
from dataclasses import dataclass
from typing import List, Optional, Sequence

from .schemas import BBox

log = logging.getLogger("agent")

# A row is words whose vertical extents overlap by at least this fraction of the shorter word's height.
# Generous enough for sub/superscripts and mixed font sizes, tight enough not to merge adjacent rows.
ROW_OVERLAP = 0.35
RENDER_SCALE = 2.0          # 144 dpi: enough for OCR and for a VLM to read a mediocre scan
MIN_TEXT_CHARS = 20         # below this a "text layer" is page furniture, not content — OCR instead


@dataclass(frozen=True)
class Word:
    """One word and its box, in PDF points, origin top-left."""
    text: str
    x0: float
    top: float
    x1: float
    bottom: float
    page: int

    @property
    def height(self) -> float:
        return max(self.bottom - self.top, 0.1)

    def bbox(self) -> BBox:
        return BBox(page=self.page, x0=self.x0, y0=self.top, x1=self.x1, y1=self.bottom)


# Separators that cling to a word in a list — "Penicillin, Sulfa" extracts as the word "Penicillin," — and are
# typography rather than content. Deliberately NOT the full stop: stripping it would change "0.9" and "<0.01",
# where the character carries meaning.
_EDGE_PUNCT = ",;:"


def _norm(s: str) -> str:
    """Compare on content, not typography: collapse whitespace, drop case, and normalise the characters OCR and
    PDF extraction routinely disagree about."""
    s = s.replace("–", "-").replace("—", "-").replace("−", "-")   # en/em dash, minus sign
    s = s.replace(" ", " ").replace("’", "'")
    return re.sub(r"\s+", "", s).strip(_EDGE_PUNCT).strip().lower()


def _same_row(a: Word, b: Word) -> bool:
    overlap = min(a.bottom, b.bottom) - max(a.top, b.top)
    return overlap >= ROW_OVERLAP * min(a.height, b.height)


def render_page_png(pdf_bytes: bytes, page: int, scale: float = RENDER_SCALE) -> bytes:
    """Rasterise one 1-based page. Used for OCR here, and for the vision call in documents.py."""
    import pypdfium2 as pdfium

    doc = pdfium.PdfDocument(pdf_bytes)
    try:
        bitmap = doc[page - 1].render(scale=scale)
        buf = io.BytesIO()
        bitmap.to_pil().save(buf, format="PNG")
        return buf.getvalue()
    finally:
        doc.close()


def tesseract_available() -> bool:
    """Tesseract is an OS package, not a Python one — pytesseract only shells out to the binary. It therefore
    imports cleanly on a machine that cannot OCR at all, which is exactly how a scanned-document feature ships
    working locally and silently broken in production. Checked explicitly so that never happens quietly."""
    return shutil.which("tesseract") is not None


def _ocr_words(pdf_bytes: bytes, page: int, scale: float = RENDER_SCALE) -> List[Word]:
    """Word boxes from OCR, scaled back into PDF points so they are interchangeable with text-layer words."""
    if not tesseract_available():
        log.warning("ocr_unavailable", extra={"page": page})
        return []
    import pytesseract
    from PIL import Image

    png = render_page_png(pdf_bytes, page, scale)
    data = pytesseract.image_to_data(Image.open(io.BytesIO(png)), output_type=pytesseract.Output.DICT)
    words: List[Word] = []
    for i, text in enumerate(data["text"]):
        if not text.strip():
            continue
        try:
            conf = float(data["conf"][i])
        except (TypeError, ValueError):
            conf = -1.0
        if conf < 0:            # tesseract marks structural rows with conf -1; they carry no text
            continue
        x, y, w, h = (data[k][i] / scale for k in ("left", "top", "width", "height"))
        words.append(Word(text=text, x0=x, top=y, x1=x + w, bottom=y + h, page=page))
    return words


def page_words(pdf_bytes: bytes, page: int) -> List[Word]:
    """Every word on a 1-based page, from the PDF's own text layer where it has one, OCR where it does not.

    A scanned lab report usually has no text layer at all; a digitally-generated one usually does and is far more
    accurate than OCR would be. Preferring the text layer is therefore both cheaper and better, and the fallback
    is what makes the scanned case work at all."""
    import pdfplumber

    with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
        if page < 1 or page > len(pdf.pages):
            return []
        p = pdf.pages[page - 1]
        extracted = p.extract_words(use_text_flow=False, keep_blank_chars=False) or []
        if sum(len(w["text"]) for w in extracted) >= MIN_TEXT_CHARS:
            return [Word(text=w["text"], x0=float(w["x0"]), top=float(w["top"]),
                         x1=float(w["x1"]), bottom=float(w["bottom"]), page=page) for w in extracted]
    return _ocr_words(pdf_bytes, page)


def _spans(words: Sequence[Word], target: str, max_words: int = 8) -> List[List[Word]]:
    """Runs of consecutive same-row words whose joined text equals the target.

    Multi-word because a value is not always one word: "12.5 mg", "Not Detected", a date split by the extractor.
    Bounded because an unbounded scan over a dense page is quadratic for no benefit."""
    hits: List[List[Word]] = []
    for i in range(len(words)):
        joined = ""
        for j in range(i, min(i + max_words, len(words))):
            if j > i and not _same_row(words[j - 1], words[j]):
                break
            joined += _norm(words[j].text)
            if joined == target:
                hits.append(list(words[i:j + 1]))
                break
            if not target.startswith(joined):
                break
    return hits


def _union(span: Sequence[Word]) -> BBox:
    return BBox(page=span[0].page, x0=min(w.x0 for w in span), y0=min(w.top for w in span),
                x1=max(w.x1 for w in span), y1=max(w.bottom for w in span))


def locate(value: str, words: Sequence[Word], label: Optional[str] = None) -> Optional[BBox]:
    """The box for `value` on this page, or None when the page cannot say unambiguously.

    `label` is the field name as printed on the page ("Potassium", "Allergies"). When given, the value must sit
    on the same row as the label — which is what stops a creatinine result being "located" inside potassium's
    reference range. When omitted, the value must be unique on the page to count.

    None is returned for both "not found" and "found more than once". The caller renders both the same way,
    because a box we are not sure about is worse than no box: it dresses a guess as evidence.
    """
    target = _norm(value)
    if not target or not words:
        return None

    candidates = _spans(words, target)
    if not candidates:
        return None

    if label:
        label_spans = _spans(words, _norm(label))
        if label_spans:
            # The label is on the page, so it is AUTHORITATIVE. A value that is not on its row is not this
            # field's value, even when it is the only occurrence on the whole page. Falling back to "any match"
            # here is exactly what attaches a confident box to another row's number, which is the failure this
            # module exists to prevent — so the filter is applied even when it leaves nothing.
            candidates = [
                c for c in candidates
                if any(_same_row(c[0], lw) and c[0].x0 >= lw.x1     # value sits to the right of its own label
                       for span in label_spans for lw in span)
            ]

    return _union(candidates[0]) if len(candidates) == 1 else None
