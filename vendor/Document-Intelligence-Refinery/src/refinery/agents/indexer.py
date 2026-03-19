"""PageIndex builder agent: build hierarchical section tree from LDUs and optionally ingest to vector store."""

from pathlib import Path
from typing import List, Optional

from refinery.models import LDU
from refinery.pageindex import build_page_index_tree
from refinery.pageindex.models import SectionNode
from refinery.strategies.base import load_extraction_rules
from refinery.strategies.config_models import ExtractionRules
from refinery.vector_store import VectorStore, ingest_document


def build_page_index(
    doc_id: str,
    ldus: List[LDU],
    rules: Optional[ExtractionRules] = None,
    persist_dir: Optional[Path] = None,
    ingest_to_vector_store: bool = False,
) -> List[SectionNode]:
    """
    Build PageIndex tree from LDUs and optionally persist and ingest.
    Persists to persist_dir or .refinery/pageindex by default.
    """
    rules = rules or load_extraction_rules()
    out_dir = persist_dir or Path(".refinery/pageindex")
    persist_path = out_dir / f"{doc_id}.json"
    roots = build_page_index_tree(ldus, doc_id, rules=rules, persist_path=persist_path)
    if ingest_to_vector_store and ldus:
        ingest_document(doc_id, ldus)
    return roots
