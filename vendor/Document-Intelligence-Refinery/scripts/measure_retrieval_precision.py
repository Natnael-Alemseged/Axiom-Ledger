#!/usr/bin/env python3
"""
Measure retrieval precision with vs without PageIndex.

Usage:
  uv run python scripts/measure_retrieval_precision.py --extraction .refinery/extractions/DOC_ID.json --labeled scripts/labeled_queries.json
  uv run python scripts/measure_retrieval_precision.py --ldus .refinery/ldus/DOC_ID.json --pageindex .refinery/pageindex/DOC_ID.json --labeled scripts/labeled_queries.json

Labeled queries JSON format:
  [
    {"query": "capital expenditure Q3", "relevant_chunk_ids": ["doc_chunk_0", "doc_chunk_1"]},
    {"query": "interoperable transactions", "relevant_chunk_ids": ["doc_chunk_5"]}
  ]

Reports precision@3, recall@3 for both paths and the delta.
"""

import argparse
import json
import sys
from pathlib import Path

# Add src to path when run as script
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from refinery.models import ExtractedDocument, LDU
from refinery.chunking import ChunkingEngine
from refinery.pageindex import (
    build_page_index_tree,
    retrieval_with_pageindex,
    retrieval_without_pageindex,
    precision_at_k,
    recall_at_k,
)
from refinery.pageindex.models import SectionNode


def load_ldus_from_extraction(extraction_path: Path, doc_id: str) -> list[LDU]:
    """Run chunking on saved ExtractedDocument to get LDUs."""
    data = json.loads(extraction_path.read_text(encoding="utf-8"))
    doc = ExtractedDocument.model_validate(data)
    engine = ChunkingEngine()
    return engine.chunk(doc)


def load_ldus_from_json(ldus_path: Path) -> list[LDU]:
    """Load LDUs from JSON array (model_dump output)."""
    data = json.loads(ldus_path.read_text(encoding="utf-8"))
    return [LDU.model_validate(item) for item in data]


def load_pageindex_from_json(path: Path) -> list[SectionNode]:
    """Load section nodes from .refinery/pageindex/{doc_id}.json or .refinery/page_index/{doc_id}.json."""
    data = json.loads(path.read_text(encoding="utf-8"))
    # May be list of roots (tree) or flat list
    nodes = [SectionNode.model_validate(n) for n in data]
    return nodes


def load_labeled_queries(path: Path) -> list[dict]:
    """Load [{"query": str, "relevant_chunk_ids": [str]}, ...]."""
    data = json.loads(path.read_text(encoding="utf-8"))
    return data if isinstance(data, list) else data.get("queries", [])


def main() -> None:
    parser = argparse.ArgumentParser(description="Measure retrieval precision with vs without PageIndex")
    parser.add_argument("--extraction", type=Path, help="Path to ExtractedDocument JSON (run chunking to get LDUs)")
    parser.add_argument("--ldus", type=Path, help="Path to saved LDUs JSON (skip chunking)")
    parser.add_argument("--pageindex", type=Path, help="Path to PageIndex section nodes JSON")
    parser.add_argument("--labeled", type=Path, required=True, help="Path to labeled_queries.json")
    parser.add_argument("--doc-id", type=str, default=None, help="doc_id when using --extraction")
    parser.add_argument("--k", type=int, default=3, help="Precision@k and Recall@k (default 3)")
    args = parser.parse_args()

    if args.extraction:
        if not args.doc_id:
            args.doc_id = args.extraction.stem
        ldus = load_ldus_from_extraction(args.extraction, args.doc_id)
        section_nodes = build_page_index_tree(ldus, args.doc_id)
    elif args.ldus:
        ldus = load_ldus_from_json(args.ldus)
        section_nodes = load_pageindex_from_json(args.pageindex) if args.pageindex else []
    else:
        print("Error: provide --extraction or --ldus", file=sys.stderr)
        sys.exit(1)

    labeled = load_labeled_queries(args.labeled)
    if not labeled:
        print("No labeled queries found.", file=sys.stderr)
        sys.exit(1)

    # Use a simple embed fn for measurement (no API): hash-based placeholder so script runs without sentence-transformers
    def stub_embed(text: str) -> list[float]:
        import hashlib
        h = hashlib.sha256(text.encode()).digest()
        return [float((b - 128) / 128) for b in h[:64]]  # 64-dim placeholder

    try:
        from refinery.vector_store import VectorStore
        store = VectorStore()
        embed_fn = store.get_embed_fn()
    except Exception:
        embed_fn = stub_embed

    k = args.k
    prec_with, rec_with = [], []
    prec_without, rec_without = [], []

    for item in labeled:
        query = item.get("query", "")
        relevant = set(item.get("relevant_chunk_ids", []))
        if not query or not relevant:
            continue
        ret_with = retrieval_with_pageindex(query, section_nodes, ldus, embed_fn, top_k_sections=3, top_k=k)
        ret_without = retrieval_without_pageindex(query, ldus, embed_fn, top_k=k)
        prec_with.append(precision_at_k(ret_with, relevant, k))
        rec_with.append(recall_at_k(ret_with, relevant, k))
        prec_without.append(precision_at_k(ret_without, relevant, k))
        rec_without.append(recall_at_k(ret_without, relevant, k))

    n = len(prec_with)
    if n == 0:
        print("No valid labeled queries.")
        return
    avg_prec_with = sum(prec_with) / n
    avg_rec_with = sum(rec_with) / n
    avg_prec_without = sum(prec_without) / n
    avg_rec_without = sum(rec_without) / n
    print(f"Labeled queries: {n}")
    print(f"Without PageIndex:  precision@{k} = {avg_prec_without:.2%},  recall@{k} = {avg_rec_without:.2%}")
    print(f"With PageIndex:     precision@{k} = {avg_prec_with:.2%},  recall@{k} = {avg_rec_with:.2%}")
    print(f"Delta: precision {avg_prec_with - avg_prec_without:+.2%}, recall {avg_rec_with - avg_rec_without:+.2%}")


if __name__ == "__main__":
    main()
