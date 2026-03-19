"""ExtractedDocument, ExtractedPage, TextBlock, ExtractedTable, ExtractedFigure, Bbox - extraction output schema."""

import hashlib
import json
from pathlib import Path
from typing import Any, Dict, List, Literal, Optional

from pydantic import BaseModel, Field


class Bbox(BaseModel):
    """Bounding box in points (x0, top, x1, bottom)."""

    x0: float = 0.0
    top: float = 0.0
    x1: float = 0.0
    bottom: float = 0.0


class FontInfo(BaseModel):
    """Font metadata for a text block."""

    font_name: Optional[str] = None
    size: Optional[float] = None
    is_ocr_font: bool = False


def _content_hash(text: str, bbox: Optional[Bbox] = None) -> str:
    payload = text
    if bbox is not None:
        payload += json.dumps(bbox.model_dump(), sort_keys=True)
    return hashlib.md5(payload.encode("utf-8")).hexdigest()


class TextBlock(BaseModel):
    """Extracted text with bbox and provenance."""

    text: str = ""
    bbox: Optional[Bbox] = None
    page_index: int = 0
    content_hash: str = ""
    font_info: Optional[FontInfo] = None

    @classmethod
    def from_text_bbox(cls, text: str, bbox: Optional[Bbox], page_index: int = 0, font_info: Optional[FontInfo] = None) -> "TextBlock":
        return cls(text=text, bbox=bbox, page_index=page_index, content_hash=_content_hash(text, bbox), font_info=font_info)


class ExtractedTable(BaseModel):
    """Structured table with provenance."""

    data: List[List[Any]] = Field(default_factory=list)
    bbox: Optional[Bbox] = None
    page_index: int = 0
    content_hash: str = ""
    caption: Optional[str] = None

    @classmethod
    def from_data_bbox(cls, data: List[List[Any]], bbox: Optional[Bbox], page_index: int = 0, caption: Optional[str] = None) -> "ExtractedTable":
        payload = json.dumps(data, sort_keys=True)
        if bbox is not None:
            payload += json.dumps(bbox.model_dump(), sort_keys=True)
        h = hashlib.md5(payload.encode("utf-8")).hexdigest()
        return cls(data=data, bbox=bbox, page_index=page_index, content_hash=h, caption=caption)


class ExtractedFigure(BaseModel):
    """Figure with optional caption and image ref."""

    bbox: Optional[Bbox] = None
    page_index: int = 0
    caption: Optional[str] = None
    image_ref: Optional[str] = None


class ExtractedPage(BaseModel):
    """One page's extracted content with reading order (element IDs)."""

    page_index: int = 0
    text_blocks: List[TextBlock] = Field(default_factory=list)
    tables: List[ExtractedTable] = Field(default_factory=list)
    figures: List[ExtractedFigure] = Field(default_factory=list)
    reading_order: List[str] = Field(default_factory=list)


ExtractionStatus = Literal["completed", "truncated_budget", "partial_failure"]


class ExtractedDocument(BaseModel):
    """Normalized document produced by any extractor."""

    doc_id: str = ""
    source_path: Optional[Path] = None
    page_count: int = 0
    strategy_used: str = ""
    pages: List[ExtractedPage] = Field(default_factory=list)
    status: ExtractionStatus = "completed"
    metadata: Dict[str, Any] = Field(default_factory=dict)
