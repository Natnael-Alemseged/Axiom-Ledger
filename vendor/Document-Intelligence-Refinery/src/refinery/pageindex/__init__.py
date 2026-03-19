"""PageIndex: section tree with LLM summaries for retrieval."""

from refinery.pageindex.builder import build_page_index_tree
from refinery.pageindex.models import SectionNode, flatten_section_nodes
from refinery.pageindex.query import (
    pageindex_query,
    precision_at_k,
    recall_at_k,
    retrieval_with_pageindex,
    retrieval_without_pageindex,
    traverse_sections,
)

__all__ = [
    "build_page_index_tree",
    "SectionNode",
    "flatten_section_nodes",
    "pageindex_query",
    "traverse_sections",
    "retrieval_with_pageindex",
    "retrieval_without_pageindex",
    "precision_at_k",
    "recall_at_k",
]
