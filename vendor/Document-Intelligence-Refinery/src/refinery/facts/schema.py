"""Pydantic schema for a single fact row (key-value with provenance)."""

from typing import Any, Dict, Optional

from pydantic import BaseModel, Field


class FactRow(BaseModel):
    """A single key-value fact with provenance (doc_id, page_ref, bbox, content_hash, source_ldu_id)."""

    doc_id: str = ""
    page_ref: int = 0  # 0-based page index
    key: str = ""  # e.g. "revenue", "date", "quarter"
    value: Any = None  # text or numeric
    unit: Optional[str] = None  # e.g. "USD", "million"
    bbox: Optional[Dict[str, float]] = None  # x0, top, x1, bottom
    content_hash: Optional[str] = None
    source_ldu_id: Optional[str] = None
