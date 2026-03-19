"""Pydantic models for rubric/extraction_rules.yaml."""

from typing import List, Optional

from pydantic import BaseModel, Field


class FastTextRules(BaseModel):
    min_chars_per_page: int = 100
    max_image_ratio: float = 0.50
    min_char_density: float = 0.02
    min_confidence: float = 0.5


class VisionRules(BaseModel):
    """Vision model IDs. Use only free/free-tier models (e.g. OpenRouter :free variant)."""
    budget_per_document_usd: float = 1.0
    model_cheap: str = "google/gemini-2.0-flash-exp:free"
    model_quality: str = "google/gemini-2.0-flash-exp:free"
    max_retries: int = 2


class ChunkingRules(BaseModel):
    """Chunking constitution: rules for RAG-ready LDUs (Phase 3)."""
    max_tokens_per_chunk: int = 512
    max_tokens_for_list: int = 512
    merge_short_text_chars: int = 80
    on_violation: str = "log"  # "raise" | "log"
    section_heading_patterns: List[str] = Field(default_factory=lambda: ["^\\d+[.)]\\s+[A-Z]", "^[A-Z][a-z]+.*:$"])
    cross_ref_patterns: List[str] = Field(
        default_factory=lambda: ["Table\\s+(\\d+)", "Figure\\s+(\\d+)", "see\\s+Table\\s+(\\d+)", "see\\s+Figure\\s+(\\d+)"]
    )


class PageIndexConfig(BaseModel):
    """PageIndex tree builder: section summaries via cheap LLM; optional key_entities, data_types."""

    summary_model_id: Optional[str] = None  # None = use vision.model_cheap
    max_input_tokens: int = 1024
    batch_size: int = 5
    key_entities_enabled: bool = False  # extract named entities per section (LLM/regex)
    data_types_from_ldu: bool = True  # derive data_types_present from LDU kinds in section
    output_dir: Optional[str] = None  # default .refinery/pageindex


class ExtractionRules(BaseModel):
    fast_text: FastTextRules = Field(default_factory=FastTextRules)
    confidence_escalation_threshold: float = 0.5
    vision: VisionRules = Field(default_factory=VisionRules)
    chunking: ChunkingRules = Field(default_factory=ChunkingRules)
    pageindex: PageIndexConfig = Field(default_factory=PageIndexConfig)
