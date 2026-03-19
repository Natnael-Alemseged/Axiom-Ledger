#!/usr/bin/env python3
"""
Build PageIndex trees for corpus documents and generate example Q&A with ProvenanceChain.

Usage:
  uv run python scripts/build_artifacts.py
  uv run python scripts/build_artifacts.py --extractions-dir .refinery/extractions --output-dir .refinery/pageindex
  uv run python scripts/build_artifacts.py --example-qa-out .refinery/example_qa/example_qa.json

Expects .refinery/extractions/*.json (ExtractedDocument). Builds .refinery/pageindex/{doc_id}.json.
If --example-qa-out is set, runs query agent on each doc to produce example Q&A (3 per doc or 12 total
across 4 classes) with full provenance_chain. Document classes: Native Financial, Scanned Financial,
Legal/Procurement, Technical/General.
"""

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from refinery.models import ExtractedDocument, LDU
from refinery.chunking import ChunkingEngine
from refinery.pageindex import build_page_index_tree
from refinery.agents.indexer import build_page_index
from refinery.agents.query_agent import run_query
from refinery.vector_store import VectorStore, ingest_document


DOCUMENT_CLASSES = [
    "Native Financial",
    "Scanned Financial",
    "Legal/Procurement",
    "Technical/General",
]

EXAMPLE_QUESTIONS_BY_CLASS = {
    "Native Financial": [
        "What are the revenue figures?",
        "What are the capital expenditure projections for Q3?",
        "What is the year-over-year growth?",
    ],
    "Scanned Financial": [
        "What financial summary is provided?",
        "What are the key numbers in this document?",
        "What period does this report cover?",
    ],
    "Legal/Procurement": [
        "What are the procurement terms?",
        "What is the contract value?",
        "What are the key dates?",
    ],
    "Technical/General": [
        "What is the main topic of this document?",
        "What key findings are reported?",
        "What methodology is described?",
    ],
}


def load_extraction(path: Path) -> ExtractedDocument:
    data = json.loads(path.read_text(encoding="utf-8"))
    return ExtractedDocument.model_validate(data)


def main() -> None:
    parser = argparse.ArgumentParser(description="Build PageIndex trees and example Q&A artifacts")
    parser.add_argument("--extractions-dir", type=Path, default=Path(".refinery/extractions"), help="Directory with ExtractedDocument JSON files")
    parser.add_argument("--output-dir", type=Path, default=Path(".refinery/pageindex"), help="Output directory for PageIndex JSON trees")
    parser.add_argument("--example-qa-out", type=Path, default=None, help="Output path for example Q&A JSON (e.g. .refinery/example_qa/example_qa.json)")
    parser.add_argument("--ingest", action="store_true", help="Ingest LDUs to vector store after building PageIndex")
    parser.add_argument("--max-docs", type=int, default=12, help="Max documents to process (default 12)")
    args = parser.parse_args()

    extractions_dir = args.extractions_dir
    if not extractions_dir.exists():
        print(f"Extractions dir not found: {extractions_dir}", file=sys.stderr)
        sys.exit(1)

    extraction_files = list(extractions_dir.glob("*.json"))[: args.max_docs]
    if not extraction_files:
        print(f"No JSON files in {extractions_dir}", file=sys.stderr)
        sys.exit(1)

    output_dir = args.output_dir
    output_dir.mkdir(parents=True, exist_ok=True)

    engine = ChunkingEngine()
    pageindex_count = 0
    doc_ids = []

    for path in extraction_files:
        doc_id = path.stem
        try:
            doc = load_extraction(path)
            ldus = engine.chunk(doc)
            if not ldus:
                continue
            roots = build_page_index(doc_id, ldus, persist_dir=output_dir, ingest_to_vector_store=args.ingest)
            pageindex_count += 1
            doc_ids.append(doc_id)
        except Exception as e:
            print(f"Error processing {path}: {e}", file=sys.stderr)

    print(f"Built {pageindex_count} PageIndex trees under {output_dir}")

    if args.example_qa_out and doc_ids:
        args.example_qa_out.parent.mkdir(parents=True, exist_ok=True)
        example_qa = []
        # Assign doc_ids to classes in round-robin (or use first 3 per class if we have 12 docs)
        per_class = max(1, len(doc_ids) // len(DOCUMENT_CLASSES))
        for i, doc_class in enumerate(DOCUMENT_CLASSES):
            class_docs = doc_ids[i * per_class : (i + 1) * per_class] or doc_ids[:1]
            questions = EXAMPLE_QUESTIONS_BY_CLASS.get(doc_class, ["What is this document about?"] * 3)
            for di, doc_id in enumerate(class_docs[:3]):
                q = questions[di % len(questions)]
                try:
                    result = run_query(q, doc_id=doc_id)
                    pc = result.get("provenance_chain")
                    citations = []
                    if pc and hasattr(pc, "citations"):
                        for c in pc.citations:
                            citations.append({
                                "document_name": c.document_name,
                                "page_number": c.page_number,
                                "page_numbers": getattr(c, "page_numbers", []),
                                "bbox": c.bbox,
                                "content_hash": c.content_hash,
                                "text": getattr(c, "text", None),
                            })
                    example_qa.append({
                        "question": q,
                        "answer": result.get("answer", ""),
                        "document_class": doc_class,
                        "document_id": doc_id,
                        "provenance_chain": citations,
                    })
                except Exception as e:
                    example_qa.append({
                        "question": q,
                        "answer": f"(Error: {e})",
                        "document_class": doc_class,
                        "document_id": doc_id,
                        "provenance_chain": [],
                    })
        args.example_qa_out.write_text(json.dumps(example_qa, indent=2), encoding="utf-8")
        print(f"Wrote {len(example_qa)} example Q&A to {args.example_qa_out}")


if __name__ == "__main__":
    main()
