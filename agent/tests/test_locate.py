"""Locating extracted values on the page.

The interesting cases are all about NOT locating: a value that appears twice, a value that appears inside some
other field's reference range, a value on the wrong row. Getting those wrong attaches a confident box to real ink
and makes a wrong claim look better evidenced than an honest "could not be located".
"""
import pytest

from copilot.locate import Word, locate, page_words, tesseract_available


def row(y, *texts, page=1, x0=72.0, width=44.0, height=12.0, gap=8.0):
    """Lay words out left to right on one visual row."""
    out, x = [], x0
    for t in texts:
        out.append(Word(text=t, x0=x, top=y, x1=x + width, bottom=y + height, page=page))
        x += width + gap
    return out


# A lab report shaped like the ones that cause trouble: the result and the reference range share a value.
LAB = (row(100, "Potassium", "5.1", "mmol/L", "3.5", "-", "5.1")
       + row(120, "Creatinine", "0.9", "mg/dL", "0.6-1.3")
       + row(140, "Sodium", "139", "mmol/L", "135-145"))


def test_a_unique_value_is_located():
    """Guards: the basic case failing quietly and everything rendering as unlocated."""
    box = locate("139", LAB, label="Sodium")
    assert box is not None and box.page == 1 and box.y0 == 140


def test_a_value_that_appears_twice_is_not_located_without_a_label():
    """Guards: picking the first match. '5.1' is both potassium's result and the top of its reference range, so
    with no label to disambiguate, the page genuinely cannot say which one is meant."""
    assert locate("5.1", LAB) is None


def test_the_label_row_picks_the_result_not_another_row():
    """Guards: THE failure this module exists for. The model extracts Creatinine = 5.1 — a wrong value that does
    appear on the page, in potassium's row. Locating it against the creatinine label must find nothing, so the
    claim renders as unlocated rather than as verified."""
    assert locate("5.1", LAB, label="Creatinine") is None


def test_a_value_is_located_on_its_own_label_row():
    """Guards: over-tightening the row rule until nothing ever locates."""
    box = locate("0.9", LAB, label="Creatinine")
    assert box is not None and box.y0 == 120


def test_a_value_before_its_label_does_not_count():
    """Guards: matching leftwards. On a lab row or a form the value sits to the right of its label; a match to
    the left of it is some other column."""
    words = row(100, "5.1", "Potassium")           # value printed before the label
    assert locate("5.1", words, label="Potassium") is None


def test_a_multi_word_value_is_located_as_one_box():
    """Guards: values that are not one word — '12.5 mg', 'Not Detected', a split date."""
    words = row(100, "Result", "Not", "Detected")
    box = locate("Not Detected", words, label="Result")
    assert box is not None and box.x0 < box.x1


def test_a_value_absent_from_the_page_is_not_located():
    """Guards: FM — the model inventing a value and the server dressing it as evidence."""
    assert locate("7.7", LAB, label="Potassium") is None


def test_words_on_different_rows_are_not_one_span():
    """Guards: a row rule loose enough to join vertically adjacent text into a false match."""
    words = row(100, "Not") + row(140, "Detected")
    assert locate("Not Detected", words) is None


@pytest.mark.parametrize("written,printed", [
    ("3.5-5.1", "3.5–5.1"),     # en dash, routine in typeset reports
    ("MMOL/L", "mmol/L"),            # case
    ("5.1 ", "5.1"),                 # stray whitespace from the extractor
])
def test_typographic_differences_do_not_prevent_a_match(written, printed):
    """Guards: a real match missed because the PDF used an en dash or different case, which would push the
    located ratio down and make the gate's value_located floor unmeetable for cosmetic reasons."""
    words = row(100, "Range", printed)
    assert locate(written, words, label="Range") is not None


def test_a_list_separator_clinging_to_a_word_does_not_prevent_a_match():
    """Guards: a real bug the eval set caught. An intake form printing "Penicillin, Sulfa" extracts the WORD
    "Penicillin," with the comma attached, so locating "Penicillin" failed and a correctly-read allergy rendered
    as unlocated. A comma is typography, not content."""
    words = row(100, "Allergies", "Penicillin,", "Sulfa")
    assert locate("Penicillin", words, label="Allergies") is not None
    assert locate("Sulfa", words, label="Allergies") is not None


def test_a_full_stop_is_not_stripped_because_it_carries_meaning():
    """Guards: over-correcting the above. Stripping '.' would change '0.9' and '<0.01', where the character is
    part of the value."""
    words = row(100, "Creatinine", "0.9") + row(120, "TSH", "<0.01")
    assert locate("0.9", words, label="Creatinine") is not None
    assert locate("<0.01", words, label="TSH") is not None
    assert locate("09", words, label="Creatinine") is None


def test_empty_inputs_are_handled():
    """Guards: a crash on a blank page or an empty extracted value."""
    assert locate("", LAB) is None
    assert locate("5.1", []) is None


# ---------------------------------------------------------------- real PDF, real coordinates

MINIMAL_PDF = (
    b"%PDF-1.4\n"
    b"1 0 obj<</Type/Catalog/Pages 2 0 R>>endobj\n"
    b"2 0 obj<</Type/Pages/Kids[3 0 R]/Count 1>>endobj\n"
    b"3 0 obj<</Type/Page/Parent 2 0 R/MediaBox[0 0 612 792]/Contents 4 0 R"
    b"/Resources<</Font<</F1 5 0 R>>>>>>endobj\n"
    b"4 0 obj<</Length 74>>stream\n"
    b"BT /F1 12 Tf 72 720 Td (Potassium 5.1 mmol/L Creatinine 0.9 mg/dL) Tj ET\n"
    b"endstream endobj\n"
    b"5 0 obj<</Type/Font/Subtype/Type1/BaseFont/Helvetica>>endobj\n"
    b"trailer<</Root 1 0 R/Size 6>>\n%%EOF\n"
)


def test_words_come_back_from_a_real_pdf_with_real_coordinates():
    """Guards: the text-layer path silently returning nothing, which would send every digital PDF down the OCR
    fallback and make locating depend on a binary that may not be installed."""
    words = page_words(MINIMAL_PDF, 1)
    assert words, "no words extracted from the text layer"
    texts = [w.text for w in words]
    assert "Potassium" in texts and "5.1" in texts
    assert all(w.x1 > w.x0 and w.bottom > w.top and w.page == 1 for w in words)


def test_locating_works_end_to_end_on_a_real_pdf():
    """Guards: coordinates that parse but do not line up, so nothing ever matches its label."""
    words = page_words(MINIMAL_PDF, 1)
    assert locate("0.9", words, label="Creatinine") is not None


def test_a_page_that_does_not_exist_is_empty_not_an_error():
    """Guards: a page-count mismatch between the extractor and the renderer crashing ingestion."""
    assert page_words(MINIMAL_PDF, 99) == []


def test_ocr_availability_is_checkable():
    """Guards: the deployment failure where pytesseract imports fine and the tesseract binary is absent, so OCR
    fails at call time — working in the demo video and broken in the app a grader opens."""
    assert isinstance(tesseract_available(), bool)
