"""ExtractionRouter: confidence-gated escalation (A -> B -> C) and persistence."""

import time
from pathlib import Path
from typing import Optional

from refinery.models import DocumentProfile, ExtractedDocument
from refinery.triage.agent import load_profile
from refinery.strategies.base import load_extraction_rules
from refinery.strategies.fast_text import FastTextExtractor
from refinery.strategies.layout_docling import LayoutExtractor
from refinery.strategies.vision_openrouter import VisionExtractor
from refinery.strategies.ledger import log_extraction

REFINERY_EXTRACTIONS_DIR = Path(".refinery/extractions")


def _ensure_extractions_dir() -> Path:
    REFINERY_EXTRACTIONS_DIR.mkdir(parents=True, exist_ok=True)
    return REFINERY_EXTRACTIONS_DIR


def run_extraction(
    pdf_path: Path,
    profile: Optional[DocumentProfile] = None,
    doc_id: Optional[str] = None,
    save: bool = True,
    extraction_rules_path: Optional[Path] = None,
) -> ExtractedDocument:
    """
    Run extraction using the appropriate strategy with confidence-gated escalation.
    If profile is None, load from .refinery/profiles/{doc_id}.json.
    Persists to .refinery/extractions/{doc_id}.json and logs to extraction_ledger.jsonl.
    """
    if profile is None:
        if doc_id is None:
            import hashlib
            doc_id = hashlib.sha256(pdf_path.read_bytes()).hexdigest()
        profile = load_profile(doc_id)
    else:
        doc_id = profile.doc_id or ""

    rules = load_extraction_rules(extraction_rules_path)
    threshold = rules.confidence_escalation_threshold
    strategies_used: list[str] = []
    doc: ExtractedDocument | None = None
    confidence = 0.0
    cost_usd = 0.0
    start = time.perf_counter()

    if "likely_handwritten" in (profile.classification_notes or []):
        vision = VisionExtractor(extraction_rules=rules)
        doc, confidence = vision.extract(pdf_path, profile)
        strategies_used.append("vision")
        cost_usd = doc.metadata.get("vision_cost_usd", 0.0)
    elif profile.estimated_extraction_cost == "needs_vision_model":
        vision = VisionExtractor(extraction_rules=rules)
        doc, confidence = vision.extract(pdf_path, profile)
        strategies_used.append("vision")
        cost_usd = doc.metadata.get("vision_cost_usd", 0.0)
    elif profile.estimated_extraction_cost == "needs_layout_model" or profile.layout_complexity in (
        "multi_column",
        "table_heavy",
        "figure_heavy",
        "mixed",
    ):
        layout = LayoutExtractor(extraction_rules=rules)
        doc, confidence = layout.extract(pdf_path, profile)
        strategies_used.append("layout")
        if confidence < threshold:
            vision = VisionExtractor(extraction_rules=rules)
            doc, confidence = vision.extract(pdf_path, profile)
            strategies_used.append("vision")
            cost_usd = doc.metadata.get("vision_cost_usd", 0.0)
    else:
        fast = FastTextExtractor(extraction_rules=rules)
        doc, confidence = fast.extract(pdf_path, profile)
        strategies_used.append("fast_text")
        if confidence < threshold:
            layout = LayoutExtractor(extraction_rules=rules)
            doc, confidence = layout.extract(pdf_path, profile)
            strategies_used.append("layout")
            if confidence < threshold:
                vision = VisionExtractor(extraction_rules=rules)
                doc, confidence = vision.extract(pdf_path, profile)
                strategies_used.append("vision")
                cost_usd = doc.metadata.get("vision_cost_usd", 0.0)

    if doc is None:
        doc = ExtractedDocument(
            doc_id=doc_id,
            source_path=pdf_path,
            page_count=0,
            strategy_used="->".join(strategies_used) or "none",
            pages=[],
            status="partial_failure",
        )
    else:
        doc.strategy_used = "->".join(strategies_used) if strategies_used else doc.strategy_used

    time_ms = (time.perf_counter() - start) * 1000
    log_extraction(
        doc_id=doc_id,
        strategy=doc.strategy_used,
        confidence=confidence,
        cost_usd=cost_usd,
        time_ms=time_ms,
        status=doc.status,
    )
    if save:
        out_dir = _ensure_extractions_dir()
        out_path = out_dir / f"{doc_id}.json"
        out_path.write_text(doc.model_dump_json(indent=2), encoding="utf-8")
    return doc
