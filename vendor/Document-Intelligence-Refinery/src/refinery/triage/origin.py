"""Origin type detection: digital vs scanned, searchable_scan, form_fillable, empty/handwritten flags."""

from pathlib import Path
from typing import Any, List, Literal, Optional

from refinery.triage.config import TriageRules, load_triage_rules

OriginType = Literal[
    "native_digital", "scanned_image", "searchable_scan", "mixed", "form_fillable"
]


def _char_area(chars: List[Any]) -> float:
    """Sum of character bounding box areas."""
    total = 0.0
    for c in chars:
        x0, x1 = c.get("x0", 0), c.get("x1", 0)
        top, bottom = c.get("top", 0), c.get("bottom", 0)
        total += (x1 - x0) * (bottom - top)
    return total


def _image_area(images: List[Any]) -> float:
    """Sum of image bounding box areas."""
    total = 0.0
    for im in images:
        x0, x1 = im.get("x0", 0), im.get("x1", 0)
        top, bottom = im.get("top", 0), im.get("bottom", 0)
        total += (x1 - x0) * (bottom - top)
    return total


def _rect_area(rects: List[Any]) -> float:
    """Sum of rect areas (x1-x0)*(bottom-top)."""
    total = 0.0
    for r in rects:
        x0, x1 = r.get("x0", 0), r.get("x1", 0)
        top, bottom = r.get("top", 0), r.get("bottom", 0)
        total += (x1 - x0) * (bottom - top)
    return total


def _curve_area(curves: List[Any]) -> float:
    """Sum of curve bounding box areas (approximate)."""
    total = 0.0
    for c in curves:
        x0, x1 = c.get("x0", 0), c.get("x1", 0)
        top, bottom = c.get("top", 0), c.get("bottom", 0)
        total += (x1 - x0) * (bottom - top)
    return total


def _font_names(chars: List[Any]) -> set[str]:
    """Unique font names from chars (normalized to uppercase for matching)."""
    names = set()
    for c in chars:
        fn = c.get("fontname") or c.get("fontname_fallback")
        if fn:
            names.add(str(fn).strip())
    return names


def _has_ocr_fonts(font_names: set[str], rules: TriageRules) -> bool:
    """True if any font name contains an OCR indicator (T3, OCR-A, Identity-H, etc.)."""
    upper = {f.upper() for f in font_names}
    for indicator in rules.ocr_font_indicators:
        ind_upper = indicator.upper()
        if any(ind_upper in f for f in upper):
            return True
    return False


def _is_form_fillable(pdf: Any) -> bool:
    """Check PDF catalog for AcroForm (pdfminer)."""
    try:
        doc = getattr(pdf, "doc", None)
        if doc is None:
            return False
        catalog = getattr(doc, "catalog", None) or {}
        acro = catalog.get("AcroForm")
        if acro is None:
            return False
        return True
    except Exception:
        return False


def classify_page_origin(
    page: Any,
    rules: TriageRules,
) -> tuple[OriginType, List[str]]:
    """
    Classify a single page's origin type and any notes (empty_page, likely_handwritten).
    page: pdfplumber page-like object with .chars, .images, .rects, .curves, .width, .height.
    """
    notes: List[str] = []
    width = float(getattr(page, "width", 1) or 1)
    height = float(getattr(page, "height", 1) or 1)
    page_area = width * height
    if page_area <= 0:
        return "mixed", ["invalid_page_area"]

    chars = list(getattr(page, "chars", []) or [])
    images = list(getattr(page, "images", []) or [])
    rects = list(getattr(page, "rects", []) or [])
    curves = list(getattr(page, "curves", []) or [])

    char_a = _char_area(chars)
    image_a = _image_area(images)
    rect_a = _rect_area(rects)
    curve_a = _curve_area(curves)
    total_figure_a = image_a + rect_a + curve_a

    char_density = char_a / page_area
    image_ratio = image_a / page_area
    figure_ratio = total_figure_a / page_area

    # Empty / low-content: low char density and no figures
    if char_density <= rules.empty_page_char_density_max and total_figure_a <= 0:
        return "mixed", notes + ["empty_page"]
    if char_density < rules.char_density.low and total_figure_a <= 0:
        # Some text but very low density and no images/rects → likely handwritten or sparse
        if char_a > 0:
            notes.append("likely_handwritten")
        return "mixed", notes

    # Digital scan (searchable_scan): chars with OCR fonts + significant image/figure content
    font_names = _font_names(chars)
    if char_density >= rules.char_density.low and _has_ocr_fonts(font_names, rules):
        if image_ratio >= rules.image_ratio.high or figure_ratio >= rules.figure_ratio.high:
            return "searchable_scan", notes
        return "searchable_scan", notes

    # High image ratio + low char density → scanned (no OCR layer or no chars)
    if image_ratio >= rules.image_ratio.high and char_density < rules.char_density.low:
        return "scanned_image", notes

    # High char density + rich (non-OCR) fonts → native digital
    if char_density >= rules.char_density.high and not _has_ocr_fonts(font_names, rules):
        return "native_digital", notes

    # Default
    return "mixed", notes


def aggregate_origin_type(
    page_results: List[tuple[OriginType, List[str]]],
    form_fillable: bool,
) -> tuple[OriginType, List[str]]:
    """Aggregate per-page origin and notes to doc-level. >10% scanned/searchable_scan → mixed."""
    if form_fillable:
        all_notes = []
        for _, n in page_results:
            all_notes.extend(n)
        return "form_fillable", all_notes

    n = len(page_results)
    if n == 0:
        return "mixed", []

    counts: dict[OriginType, int] = {}
    all_notes: List[str] = []
    for orig, notes in page_results:
        counts[orig] = counts.get(orig, 0) + 1
        all_notes.extend(notes)

    scanned_like = counts.get("scanned_image", 0) + counts.get("searchable_scan", 0)
    if scanned_like > 0 and (scanned_like / n) > 0.10:
        return "mixed", all_notes
    if counts.get("searchable_scan", 0) == n:
        return "searchable_scan", all_notes
    if counts.get("scanned_image", 0) == n:
        return "scanned_image", all_notes
    if counts.get("native_digital", 0) == n:
        return "native_digital", all_notes
    # Majority or mixed
    best = max(counts, key=counts.get)  # type: ignore
    return best, all_notes


def detect_origin(
    pdf: Any,
    pages: List[Any],
    rules: Optional[TriageRules] = None,
) -> tuple[OriginType, List[str]]:
    """
    Detect document-level origin type from a list of pages.
    pdf: pdfplumber PDF (for AcroForm check). pages: list of page objects to analyze.
    """
    rules = rules or load_triage_rules()
    form_fillable = _is_form_fillable(pdf)
    page_results = [classify_page_origin(p, rules) for p in pages]
    return aggregate_origin_type(page_results, form_fillable)
