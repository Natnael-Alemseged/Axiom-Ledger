"""Triage agent orchestration. Uses config, origin and layout heuristics, sampling."""

import hashlib
import json
import re
from pathlib import Path
from typing import Dict, List, Optional

from refinery.models import DocumentProfile, LanguageInfo
from refinery.triage.config import load_triage_rules
from refinery.triage.exceptions import RefineryTriageError
from refinery.triage.layout import detect_layout
from refinery.triage.origin import detect_origin

REFINERY_PROFILES_DIR = Path(".refinery/profiles")
SAMPLE_PAGE_THRESHOLD = 10
SAMPLE_FIRST = 3
SAMPLE_MIDDLE = 2
SAMPLE_LAST = 3


def _sample_pages(pages: list, page_count: int):
    """Return pages to analyze: all if <= threshold, else first 3 + middle 2 + last 3."""
    if page_count <= SAMPLE_PAGE_THRESHOLD:
        return pages
    result = []
    result.extend(pages[:SAMPLE_FIRST])
    mid = page_count // 2
    result.extend(pages[mid - SAMPLE_MIDDLE // 2 : mid + (SAMPLE_MIDDLE - SAMPLE_MIDDLE // 2)])
    result.extend(pages[-SAMPLE_LAST :])
    return result


def _detect_language(text: str) -> LanguageInfo:
    """Run langdetect on text; return unknown if empty or failure."""
    text = (text or "").strip()
    if len(text) < 20:
        return LanguageInfo(code="unknown", confidence=0.0)
    try:
        import langdetect
        lang = langdetect.detect(text)
        return LanguageInfo(code=lang or "unknown", confidence=0.9)
    except Exception:
        return LanguageInfo(code="unknown", confidence=0.0)

def _detect_domain(text: str, domain_strategy: Optional[object]) -> str:
    """Use provided strategy or default keyword classifier."""
    if domain_strategy is not None and hasattr(domain_strategy, "classify"):
        return domain_strategy.classify(text or "")
    
    # Default: robust keyword scoring with thresholds
    if not text or len(text) < 100:  # Ignore very short or empty text
        return "general"
    
    # Normalize text: lowercase, remove punctuation for better matching
    text_norm = re.sub(r'[^\w\s]', '', text.lower())  # Remove punctuation
    
    # Domain-specific keyword lists (expanded for robustness, focused on corpus like financial reports)
    domains: Dict[str, list[str]] = {
        "financial": [
            "balance", "revenue", "invoice", "amount", "payment", "transaction", "bank", "capital", 
            "asset", "financial", "atm", "pos", "p2p", "interoperable", "qr", "wallet", "billion", 
            "birr", "fiscal", "report", "annual", "board", "shareholders", "profit", "expense", 
            "income", "settlement", "clearing", "scheme", "dispute", "card", "pin", "personalization"
        ],
        "legal": [
            "whereas", "hereby", "agreement", "party", "contract", "clause", "regulation", "law", 
            "court", "dispute", "liability", "terms", "conditions", "warranty", "indemnity"
        ],
        "technical": [
            "api", "implementation", "config", "code", "system", "platform", "integration", 
            "development", "testing", "project", "iso", "instant", "real-time", "gateway", "recon"
        ],
        "medical": [
            "patient", "diagnosis", "treatment", "health", "disease", "symptom", "medicine", 
            "hospital", "doctor", "therapy", "surgery"
        ]
    }
    
    # Score each domain by counting unique keyword matches (avoids overcounting repeats)
    scores: Dict[str, int] = {}
    for domain, keywords in domains.items():
        matches = sum(1 for kw in set(keywords) if kw in text_norm)  # Unique keywords
        scores[domain] = matches
    
    # Pick the domain with the highest score if above threshold (e.g., at least 3 matches)
    max_domain = max(scores, key=scores.get)
    if scores[max_domain] >= 3:
        return max_domain
    return "general"


def save_profile(profile: DocumentProfile, doc_id: str) -> Path:
    """Write profile to .refinery/profiles/{doc_id}.json. Creates directory if needed."""
    out_dir = REFINERY_PROFILES_DIR
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / f"{doc_id}.json"
    path.write_text(profile.model_dump_json(indent=2), encoding="utf-8")
    return path


def load_profile(doc_id: str, profiles_dir: Optional[Path] = None) -> DocumentProfile:
    """Load a saved profile from .refinery/profiles/{doc_id}.json."""
    dir_path = profiles_dir or REFINERY_PROFILES_DIR
    path = dir_path / f"{doc_id}.json"
    if not path.exists():
        raise FileNotFoundError(f"Profile not found: {path}")
    data = json.loads(path.read_text(encoding="utf-8"))
    if "source_path" in data and data["source_path"]:
        data["source_path"] = Path(data["source_path"])
    return DocumentProfile.model_validate(data)


def load_profile_from_path(profile_path: Path) -> DocumentProfile:
    """Load a profile from an arbitrary path."""
    data = json.loads(profile_path.read_text(encoding="utf-8"))
    if "source_path" in data and data["source_path"]:
        data["source_path"] = Path(data["source_path"])
    return DocumentProfile.model_validate(data)


def run_triage(
    pdf_path: Path,
    doc_id: Optional[str] = None,
    domain_strategy: Optional[object] = None,
    save: bool = True,
    config_path: Optional[Path] = None,
) -> DocumentProfile:
    """Run triage on a PDF: origin, layout, language, domain; return DocumentProfile."""
    try:
        import pdfplumber
    except ImportError:
        raise RefineryTriageError("pdfplumber is required for triage")

    try:
        with pdfplumber.open(pdf_path) as pdf:
            page_count = len(pdf.pages)
            if doc_id is None:
                doc_id = hashlib.sha256(pdf_path.read_bytes()).hexdigest()

            rules = load_triage_rules(config_path)
            pages_to_use = _sample_pages(pdf.pages, page_count)

            origin_type, classification_notes = detect_origin(pdf, pages_to_use, rules)
            layout_complexity = detect_layout(pages_to_use, rules)

            # Extract text from same pages for language/domain
            text_parts = []
            for p in pages_to_use:
                try:
                    t = p.extract_text()
                    if t:
                        text_parts.append(t)
                except Exception:
                    pass
            full_text = "\n".join(text_parts) if text_parts else ""

            language = _detect_language(full_text)
            domain_hint = _detect_domain(full_text, domain_strategy)

            profile = DocumentProfile(
                origin_type=origin_type,
                layout_complexity=layout_complexity,
                language=language,
                domain_hint=domain_hint,
                status="ok",
                doc_id=doc_id,
                source_path=pdf_path,
                page_count=page_count,
                classification_notes=classification_notes,
            )
            if save:
                save_profile(profile, doc_id)
            return profile
    except RefineryTriageError:
        raise
    except Exception as e:
        raise RefineryTriageError(f"Could not open PDF: {e}") from e
