"""Provenance and logical document units: PageIndex, LDU, ProvenanceChain, Citation."""

from typing import Any, Dict, List, Literal, Optional

from pydantic import BaseModel, Field, computed_field


class PageIndex(BaseModel):
    """Reference to a page or a hierarchical section (0-based index, optional label, optional children)."""

    index: int = 0
    label: Optional[str] = None  # e.g. "1", "i", "Appendix A"
    children: List["PageIndex"] = Field(default_factory=list)  # recursive: sub-pages or subsections


# Allow recursive PageIndex
PageIndex.model_rebuild()


class SourceCitation(BaseModel):
    """Single source citation with the excerpt text needed for audit verification."""

    document_name: str = ""
    page_number: Optional[int] = None  # primary page (1-based for display)
    page_numbers: List[int] = Field(default_factory=list)  # all pages when chunk spans multiple
    bbox: Optional[Dict[str, float]] = None  # x0, top, x1, bottom
    content_hash: Optional[str] = None
    text: Optional[str] = None  # retrieved excerpt or fact summary shown to the audit judge


class ProvenanceChain(BaseModel):
    """Chain of content hashes for audit and deduplication, with source and span metadata.
    Every answer must include a list of citations (document_name, page_number, bbox, content_hash, text)."""

    hashes: List[str] = Field(default_factory=list)
    source_strategy: Optional[str] = None
    source_document: Optional[str] = None  # doc_id or path for traceability
    document_name: Optional[str] = None  # primary display name (alias from source_document)
    page_number: Optional[int] = None  # primary page for citation (1-based)
    page_numbers: List[int] = Field(default_factory=list)  # all pages when chain spans multiple
    bbox: Optional[Dict[str, float]] = None  # overall span (x0, top, x1, bottom) for the chain
    citations: List[SourceCitation] = Field(default_factory=list)  # list of source citations per answer


LDUKind = Literal["text", "table", "figure", "heading", "list", "other"]


class LDU(BaseModel):
    """Logical Document Unit: a coherent chunk (text, table, figure) with optional provenance."""

    kind: LDUKind = "text"
    content: Any = None  # text str, or table data, or figure ref
    page_index: Optional[PageIndex] = None
    page_refs: List[int] = Field(default_factory=list)  # page indices the chunk spans (for multi-page chunks)
    token_count: int = 0
    bbox: Optional[Dict[str, float]] = None  # x0, top, x1, bottom
    content_hash: Optional[str] = None
    parent_section: Optional[str] = None  # e.g. "Chapter 3", "Appendix A"
    chunk_id: str = ""  # stable id for dedup, retrieval, referencing
    metadata: Dict[str, Any] = Field(default_factory=dict)

    @computed_field
    @property
    def chunk_type(self) -> LDUKind:
        """Spec-compliant alias for kind (chunk_type in API)."""
        return self.kind
