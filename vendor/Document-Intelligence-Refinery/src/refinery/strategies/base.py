"""Extractor protocol and extraction config (rubric/extraction_rules.yaml)."""

from pathlib import Path
from typing import Protocol, runtime_checkable

from refinery.models import DocumentProfile, ExtractedDocument
from refinery.strategies.config_models import ExtractionRules


@runtime_checkable
class Extractor(Protocol):
    """Protocol for extraction strategies. extract returns (document, confidence)."""

    def extract(self, pdf_path: Path, profile: DocumentProfile) -> tuple[ExtractedDocument, float]:
        """Run extraction. Low confidence triggers escalation."""
        ...


def _default_extraction_config_path() -> Path:
    return Path.cwd() / "rubric" / "extraction_rules.yaml"


def load_extraction_rules(path: Path | None = None) -> ExtractionRules:
    """Load extraction rules from rubric/extraction_rules.yaml (or configs/ for backward compat)."""
    try:
        import yaml
    except ImportError:
        return ExtractionRules()

    config_path = path or _default_extraction_config_path()
    if not config_path.exists():
        config_path = Path.cwd() / "configs" / "extraction_rules.yaml"
    if not config_path.exists():
        return ExtractionRules()
    data = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    if not data:
        return ExtractionRules()
    return ExtractionRules.model_validate(data)
