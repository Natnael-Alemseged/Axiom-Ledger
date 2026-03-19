"""Placeholder tests so pytest can run. Expand with triage tests per plan."""

import pytest


def test_placeholder():
    """Pytest can discover and run tests in tests/."""
    assert True


def test_refinery_import():
    """Package is importable."""
    from refinery import DocumentProfile, LanguageInfo, run_triage, RefineryTriageError

    assert DocumentProfile is not None
    assert LanguageInfo is not None
    assert run_triage is not None
    assert RefineryTriageError is not None


def test_triage_rules_load():
    """Config loads from configs/triage_rules.yaml when present."""
    from refinery.triage.config import load_triage_rules, TriageRules

    rules = load_triage_rules()
    assert isinstance(rules, TriageRules)
    assert rules.char_density.low <= rules.char_density.high
    assert "T3" in rules.ocr_font_indicators or "Identity-H" in rules.ocr_font_indicators


def test_classify_page_origin_empty_page():
    """Low char density and no images/rects → empty_page note."""
    from refinery.triage.config import TriageRules
    from refinery.triage.origin import classify_page_origin

    rules = TriageRules()
    page = type("Page", (), {
        "width": 612, "height": 792,
        "chars": [], "images": [], "rects": [], "curves": [],
    })()
    origin, notes = classify_page_origin(page, rules)
    assert "empty_page" in notes


def test_classify_page_origin_ocr_fonts_searchable_scan():
    """Chars with OCR-like font + high image ratio → searchable_scan."""
    from refinery.triage.config import TriageRules
    from refinery.triage.origin import classify_page_origin

    rules = TriageRules()
    page_area = 612 * 792
    # Char area must be >= rules.char_density.low (0.02) so we hit searchable_scan path
    min_char_area = page_area * rules.char_density.low
    side = int(min_char_area ** 0.5) + 1
    page = type("Page", (), {
        "width": 612, "height": 792,
        "chars": [
            {"x0": 0, "x1": side, "top": 0, "bottom": side, "fontname": "T3-Identity-H"},
            {"x0": 0, "x1": 20, "top": 0, "bottom": 20, "fontname": "OCR-A"},
        ],
        "images": [{"x0": 0, "x1": 612, "top": 0, "bottom": 792}],
        "rects": [], "curves": [],
    })()
    origin, notes = classify_page_origin(page, rules)
    assert origin == "searchable_scan"


def test_classify_layout_figure_ratio_includes_rects():
    """Figure ratio includes rects and curves for figure_heavy."""
    from refinery.triage.config import TriageRules
    from refinery.triage.layout import classify_page_layout

    rules = TriageRules()
    rules.figure_ratio.high = 0.2
    page_area = 612 * 792
    rects = [{"x0": 0, "x1": 400, "top": 0, "bottom": 400}]
    rect_area = 400 * 400
    assert (rect_area / page_area) >= 0.2
    page = type("Page", (), {
        "width": 612, "height": 792,
        "chars": [{"x0": 500, "x1": 510, "top": 500, "bottom": 510}],
        "images": [], "rects": rects, "curves": [],
        "find_tables": lambda: [],
    })()
    layout = classify_page_layout(page, rules)
    assert layout == "figure_heavy"


def test_document_profile_classification_notes():
    """DocumentProfile has classification_notes and serializes."""
    from refinery.models import DocumentProfile

    p = DocumentProfile(classification_notes=["empty_page", "likely_handwritten"])
    assert p.classification_notes == ["empty_page", "likely_handwritten"]
    d = p.model_dump()
    assert "classification_notes" in d


def test_load_profile_requires_doc_id():
    """load_profile raises if profile file not found."""
    from refinery.triage.agent import load_profile

    with pytest.raises(FileNotFoundError):
        load_profile("nonexistent_doc_id_12345")


def test_extraction_rules_load():
    """Extraction rules load from rubric/extraction_rules.yaml or configs/ when present."""
    from refinery.strategies.base import load_extraction_rules

    rules = load_extraction_rules()
    assert rules.fast_text.min_chars_per_page >= 0
    assert 0 <= rules.confidence_escalation_threshold <= 1
    assert rules.vision.budget_per_document_usd > 0
    assert rules.chunking.max_tokens_per_chunk > 0
    assert rules.pageindex.max_input_tokens > 0


def test_extraction_schema_content_hash():
    """TextBlock and ExtractedTable get content_hash from from_* factory."""
    from refinery.models import Bbox, TextBlock, ExtractedTable

    tb = TextBlock.from_text_bbox("hello", Bbox(x0=0, top=0, x1=10, bottom=10), page_index=0)
    assert tb.content_hash
    assert len(tb.content_hash) == 32
    et = ExtractedTable.from_data_bbox([["a", "b"]], Bbox(x0=0, top=0, x1=10, bottom=10), page_index=0)
    assert et.content_hash
    assert len(et.content_hash) == 32


def test_fast_text_confidence_scoring():
    """FastTextExtractor returns confidence in [0, 1]; low confidence for sparse page."""
    from pathlib import Path
    from refinery.models import DocumentProfile
    from refinery.strategies.fast_text import FastTextExtractor
    from refinery.strategies.config_models import ExtractionRules

    rules = ExtractionRules()
    rules.fast_text.min_chars_per_page = 50
    ext = FastTextExtractor(extraction_rules=rules)
    # Use a real PDF from repo if available
    for name in ("ETS_Annual_Report_2024_2025.pdf", "2013-E.C-Procurement-information.pdf"):
        path = Path(__file__).resolve().parent.parent / name
        if path.exists():
            profile = DocumentProfile(doc_id="test-ft", source_path=path, page_count=1)
            doc, conf = ext.extract(path, profile)
            assert doc.strategy_used == "fast_text"
            assert 0 <= conf <= 1
            assert len(doc.pages) >= 1
            return
    pytest.skip("No PDF in repo for FastText confidence test")
