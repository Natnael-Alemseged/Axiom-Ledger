"""Load triage rules from configs/triage_rules.yaml."""

from pathlib import Path
from typing import List

from pydantic import BaseModel, Field

# When running from project root, configs/ is under cwd.
def _default_config_path() -> Path:
    return Path.cwd() / "configs" / "triage_rules.yaml"


class CharDensityRules(BaseModel):
    low: float = 0.02
    high: float = 0.15


class ImageRatioRules(BaseModel):
    high: float = 0.25


class TableRatioRules(BaseModel):
    high: float = 0.20


class FigureRatioRules(BaseModel):
    high: float = 0.25


class TriageRules(BaseModel):
    """Triage thresholds loaded from YAML."""

    char_density: CharDensityRules = Field(default_factory=CharDensityRules)
    image_ratio: ImageRatioRules = Field(default_factory=ImageRatioRules)
    table_ratio: TableRatioRules = Field(default_factory=TableRatioRules)
    figure_ratio: FigureRatioRules = Field(default_factory=FigureRatioRules)
    ocr_font_indicators: List[str] = Field(
        default_factory=lambda: ["T3", "OCR-A", "OCR-B", "Identity-H"]
    )
    empty_page_char_density_max: float = 0.005


def load_triage_rules(path: Path | None = None) -> TriageRules:
    """Load triage rules from YAML. Uses defaults if file missing or path None."""
    try:
        import yaml
    except ImportError:
        return TriageRules()

    config_path = path or _default_config_path()
    if not config_path.exists():
        return TriageRules()

    data = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    if not data:
        return TriageRules()
    return TriageRules.model_validate(data)
