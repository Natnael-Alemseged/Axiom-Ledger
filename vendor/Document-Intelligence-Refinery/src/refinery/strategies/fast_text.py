"""Strategy A: Fast text extraction via pdfplumber with multi-signal confidence."""

import re
from pathlib import Path
from typing import Any, List

import pdfplumber

from refinery.models import DocumentProfile, Bbox, ExtractedDocument, ExtractedPage, FontInfo, TextBlock
from refinery.triage import origin as origin_module
from refinery.triage.config import load_triage_rules
from refinery.strategies.config_models import ExtractionRules
from refinery.strategies.base import load_extraction_rules


def _page_area(page: Any) -> float:
    w = float(getattr(page, "width", 1) or 1)
    h = float(getattr(page, "height", 1) or 1)
    return w * h


def _readability_score(text: str) -> float:
    if not text or len(text) == 0:
        return 0.0
    words = re.findall(r"[a-zA-Z0-9]+", text)
    word_chars = sum(len(w) for w in words)
    return word_chars / len(text) if len(text) > 0 else 0.0


def _font_mapping_score(chars: List[Any]) -> float:
    if not chars:
        return 0.0
    valid = sum(1 for c in chars if (c.get("fontname") or c.get("fontname_fallback")) and c.get("adv") is not None)
    return valid / len(chars)


def _page_confidence(page: Any, rules: ExtractionRules, triage_rules: Any) -> float:
    chars = list(getattr(page, "chars", []) or [])
    images = list(getattr(page, "images", []) or [])
    area = _page_area(page)
    if area <= 0:
        return 0.0
    char_count = len(chars) or len(page.extract_text() or "")
    char_a = origin_module._char_area(chars)
    image_a = origin_module._image_area(images)
    char_density = char_a / area
    image_ratio = image_a / area
    if char_count < rules.fast_text.min_chars_per_page or image_ratio > rules.fast_text.max_image_ratio or char_density < rules.fast_text.min_char_density:
        return 0.0
    text = page.extract_text() or ""
    readability = _readability_score(text) if text else 0.0
    font_score = _font_mapping_score(chars) if chars else 0.0
    font_names = origin_module._font_names(chars)
    ocr_penalty = 0.7 if origin_module._has_ocr_fonts(font_names, triage_rules) else 1.0
    density_norm = min(1.0, (char_density - rules.fast_text.min_char_density) / 0.10)
    confidence = (density_norm * 0.35 + readability * 0.35 + font_score * 0.30) * ocr_penalty
    return min(1.0, max(0.0, confidence))


class FastTextExtractor:
    """Extract text via pdfplumber. Returns low confidence on scan-like or sparse pages."""

    def __init__(self, extraction_rules: ExtractionRules | None = None):
        self.rules = extraction_rules or load_extraction_rules()
        self._triage_rules = load_triage_rules()

    def extract(self, pdf_path: Path, profile: DocumentProfile) -> tuple[ExtractedDocument, float]:
        doc_id = profile.doc_id or ""
        pages_out: List[ExtractedPage] = []
        confidences: List[float] = []

        with pdfplumber.open(pdf_path) as pdf:
            for i, page in enumerate(pdf.pages):
                text = page.extract_text() or ""
                w = float(getattr(page, "width", 0) or 0)
                h = float(getattr(page, "height", 0) or 0)
                bbox = Bbox(x0=0, top=0, x1=w, bottom=h) if w and h else None
                font_info = None
                chars = list(getattr(page, "chars", []) or [])
                if chars:
                    fn = chars[0].get("fontname") or chars[0].get("fontname_fallback")
                    font_names = origin_module._font_names(chars)
                    font_info = FontInfo(
                        font_name=fn,
                        size=chars[0].get("size"),
                        is_ocr_font=origin_module._has_ocr_fonts(font_names, self._triage_rules),
                    )
                tb = TextBlock.from_text_bbox(text, bbox, page_index=i, font_info=font_info)
                ep = ExtractedPage(
                    page_index=i,
                    text_blocks=[tb],
                    tables=[],
                    figures=[],
                    reading_order=[f"p{i}-tb0"],
                )
                pages_out.append(ep)
                confidences.append(_page_confidence(page, self.rules, self._triage_rules))

        doc_confidence = min(confidences) if confidences else 0.0
        doc = ExtractedDocument(
            doc_id=doc_id,
            source_path=pdf_path,
            page_count=len(pages_out),
            strategy_used="fast_text",
            pages=pages_out,
            status="completed",
        )
        return doc, doc_confidence
