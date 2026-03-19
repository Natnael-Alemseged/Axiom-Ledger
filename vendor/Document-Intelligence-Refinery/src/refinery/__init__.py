"""Refinery public API: models, agents (triage + extractor), strategies, chunking, pageindex, vector_store."""

from refinery.models import (
    DocumentProfile,
    LanguageInfo,
    Bbox,
    ExtractedDocument,
    ExtractedPage,
    ExtractedTable,
    ExtractedFigure,
    TextBlock,
    FontInfo,
    PageIndex,
    LDU,
    ProvenanceChain,
    SourceCitation,
)
from refinery.agents.triage import run_triage, save_profile, load_profile, load_profile_from_path
from refinery.agents.extractor import run_extraction
from refinery.triage.exceptions import RefineryTriageError
from refinery.chunking import ChunkingEngine, ChunkValidator, load_chunking_rules, RuleViolation, ValidationResult
from refinery.pageindex import (
    build_page_index_tree,
    SectionNode,
    pageindex_query,
    retrieval_with_pageindex,
    retrieval_without_pageindex,
    precision_at_k,
    recall_at_k,
)
from refinery.vector_store import VectorStore, ingest_document, CHROMA_DIR

__all__ = [
    "DocumentProfile",
    "LanguageInfo",
    "Bbox",
    "ExtractedDocument",
    "ExtractedPage",
    "ExtractedTable",
    "ExtractedFigure",
    "TextBlock",
    "FontInfo",
    "PageIndex",
    "LDU",
    "ProvenanceChain",
    "SourceCitation",
    "run_triage",
    "save_profile",
    "load_profile",
    "load_profile_from_path",
    "run_extraction",
    "RefineryTriageError",
    "ChunkingEngine",
    "ChunkValidator",
    "load_chunking_rules",
    "RuleViolation",
    "ValidationResult",
    "build_page_index_tree",
    "SectionNode",
    "pageindex_query",
    "retrieval_with_pageindex",
    "retrieval_without_pageindex",
    "precision_at_k",
    "recall_at_k",
    "VectorStore",
    "ingest_document",
    "CHROMA_DIR",
]
