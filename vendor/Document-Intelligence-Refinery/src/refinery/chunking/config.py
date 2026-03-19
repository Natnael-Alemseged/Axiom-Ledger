"""Load chunking rules from rubric/extraction_rules.yaml or configs/extraction_rules.yaml."""

from pathlib import Path

from refinery.strategies.base import load_extraction_rules
from refinery.strategies.config_models import ChunkingRules, ExtractionRules


def load_chunking_rules(extraction_rules_path: Path | None = None) -> ChunkingRules:
    """Load ChunkingRules from extraction_rules.yaml (rubric or configs fallback)."""
    rules: ExtractionRules = load_extraction_rules(extraction_rules_path)
    return rules.chunking
