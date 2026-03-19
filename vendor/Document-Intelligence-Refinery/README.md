# Document Intelligence Refinery

Triage and multi-strategy extraction for PDFs: origin/layout/domain detection and confidence-gated extraction (FastText → Layout → Vision).

## Setup

Using [uv](https://docs.astral.sh/uv/):

```bash
uv sync
```

This creates a virtual environment (`.venv`) and installs dependencies. Locked dependencies are in `uv.lock`.

## Run tests

```bash
uv run pytest tests/ -v
```

Unit tests cover Triage Agent classification (origin, layout, domain) and extraction confidence scoring.

## Project layout

- **`src/refinery/models/`** — Pydantic schemas: `DocumentProfile`, `ExtractedDocument`, `LDU`, `PageIndex`, `ProvenanceChain`, `SourceCitation`, extraction schema (Bbox, TextBlock, ExtractedTable, etc.).
- **`src/refinery/agents/`** — **Triage** (`triage.py`), **Extractor** (`extractor.py`), **Chunker** (`chunker.py`: ChunkingEngine + ChunkValidator), **Indexer** (`indexer.py`: PageIndex builder), **Query Agent** (`query_agent.py`: LangGraph with pageindex_navigate, semantic_search, structured_query), **Audit** (`audit.py`: verify_claim).
- **`src/refinery/strategies/`** — Extraction strategies: FastText, Layout (Docling), Vision (OpenRouter).
- **`src/refinery/pageindex/`** — PageIndex tree (hierarchical sections, key_entities, data_types_present), builder, retrieval with/without PageIndex.
- **`src/refinery/vector_store/`** — ChromaDB + sentence-transformers (default `all-MiniLM-L6-v2`).
- **`src/refinery/facts/`** — FactTable extractor and SQLite store for financial/legal key-value facts.
- **`rubric/extraction_rules.yaml`** — Extraction thresholds, chunking, pageindex (key_entities_enabled, data_types_from_ldu).
- **`.refinery/profiles/`** — DocumentProfile JSON.
- **`.refinery/extractions/`** — ExtractedDocument JSON.
- **`.refinery/pageindex/`** — PageIndex trees (JSON) per doc_id.
- **`.refinery/vector_store/`** — ChromaDB persistence.
- **`.refinery/fact_store.db`** — SQLite fact table (when fact extraction is used).
- **`.refinery/example_qa/`** — Example Q&A with full ProvenanceChain (from `scripts/build_artifacts.py --example-qa-out`).

## Triage a PDF (CLI)

```bash
uv run python -m refinery path/to/document.pdf
```

Or the console script (after `uv sync`):

```bash
uv run refinery-triage path/to/document.pdf
```

Options: `--no-save`, `--json`, `--doc-id ID`.

## Extraction

Run extraction after triage (profile is loaded from `.refinery/profiles/{doc_id}.json` if not passed):

```python
from pathlib import Path
from refinery import run_triage, run_extraction, load_profile

# Triage (saves profile to .refinery/profiles/)
profile = run_triage(Path("document.pdf"))

# Extract (uses strategy from profile; escalates on low confidence)
doc = run_extraction(Path("document.pdf"), profile=profile)
# Result saved to .refinery/extractions/{doc_id}.json and logged to extraction_ledger.jsonl
```

Or with an existing profile:

```python
profile = load_profile("existing_doc_id")
doc = run_extraction(Path("document.pdf"), profile=profile)
```

## Usage from Python

```python
from pathlib import Path
from refinery import run_triage, DocumentProfile, run_extraction

profile: DocumentProfile = run_triage(Path("document.pdf"))
# Profile is also saved to .refinery/profiles/{doc_id}.json when doc_id is set or derived

doc = run_extraction(Path("document.pdf"), profile=profile)
```

## Configuration

- **Triage**: `configs/triage_rules.yaml` (char density, image/table/figure ratios, OCR font indicators).
- **Extraction**: `rubric/extraction_rules.yaml` (fast_text thresholds, confidence_escalation_threshold, vision budget and models). See `DOMAIN_NOTES.md` for rationale.

## Vision API keys

Set at least one in `.env` (or environment) for VisionExtractor and fallbacks:

- `OPENROUTER_API_KEY` or `OPENROUTER_KEY`
- `GROQ_API_KEY`
- `GOOGLE_API_KEY` or `GEMINI_API_KEY`
- `SAMBANOVA_KEY`

Default vision model is free-tier (`google/gemini-2.0-flash-exp:free`). Provider fallback order: OpenRouter → Groq → Google → SambaNova.

## Query agent and provenance

After extraction, chunking, and PageIndex build, run the **query agent** (LangGraph with pageindex_navigate, semantic_search, structured_query). Every answer includes a **ProvenanceChain** (SourceCitation: document_name, page_number, bbox, content_hash).

```python
from refinery.agents.query_agent import run_query, invoke_query_agent
result = run_query("What are the revenue figures?", doc_id="my_doc_id")
print(result["answer"], result["provenance_chain"].citations)
state = invoke_query_agent("What are the revenue figures?", doc_id="my_doc_id")
```

Pipeline: extraction → chunking → build PageIndex (`refinery.agents.indexer.build_page_index`) → run query.

## Audit Mode

```python
from refinery.agents import verify_claim
out = verify_claim("The report states revenue was $4.2B in Q3", doc_id="report_2024")
# out["status"] in ("verified", "not_found", "unverifiable"); out["citation"] when verified
```

## Example Q&A and artifacts

- Build PageIndex trees: `uv run python scripts/build_artifacts.py --extractions-dir .refinery/extractions --output-dir .refinery/pageindex`
- Example Q&A with ProvenanceChain: `uv run python scripts/build_artifacts.py --example-qa-out .refinery/example_qa/example_qa.json`
- Precision (PageIndex vs vector): `uv run python scripts/measure_retrieval_precision.py --extraction .refinery/extractions/DOC_ID.json --labeled scripts/labeled_queries.json`

## Environment variables

- `OPENROUTER_API_KEY` or `OPENROUTER_KEY` — Vision and PageIndex summaries
- `REFINERY_CHROMA_DIR` — ChromaDB path (default `.refinery/vector_store`)
- `REFINERY_FACT_DB` — Fact store path (default `.refinery/fact_store.db`)

## Deploy in under 10 minutes

1. `uv sync`
2. Set `OPENROUTER_API_KEY` if using Vision/PageIndex summaries
3. Triage: `uv run python -m refinery path/to/document.pdf`; then run extraction and build PageIndex
4. Docker: `docker build -t refinery .` then `docker run --rm -v $(pwd)/.refinery:/app/.refinery -e OPENROUTER_API_KEY=xxx refinery path/to/doc.pdf`

## Artifacts & rubric checklist (Master Thinker)

To satisfy the full rubric you need **artifacts** (not just code):

| Requirement | How to satisfy |
|-------------|----------------|
| **12-document corpus** (min 3 per class) | Put PDFs in `data/` (or similar). Run `uv run python scripts/onboard_documents.py data/ --max-docs 12` to triage + extract into `.refinery/profiles/` and `.refinery/extractions/`. |
| **12 PageIndex trees** | `uv run python scripts/build_artifacts.py --extractions-dir .refinery/extractions --output-dir .refinery/pageindex` (run after you have ≥12 extractions). |
| **12 example Q&A with ProvenanceChain** | `uv run python scripts/build_artifacts.py --example-qa-out .refinery/example_qa/example_qa.json` (requires vector store populated). |
| **Precision evidence (PageIndex vs naive)** | Add `relevant_chunk_ids` to `scripts/labeled_queries.json`, run `uv run python scripts/measure_retrieval_precision.py --extraction .refinery/extractions/DOC_ID.json --labeled scripts/labeled_queries.json` and record the delta in REPORT.md. |
| **Audit LLM judge** | `verify_claim_with_judge()` calls an LLM when multiple evidence items exist to decide supported vs unverifiable. Set `OPENROUTER_API_KEY`. |
| **Fact extraction depth** | Current implementation uses regex + domain trigger. For richer tables, extend `refinery.facts.extractor` with an optional LLM pass. |

### Verified 3 steps to generate artifacts (Master Thinker)

**Step 1 — Process 12+ documents (triage + extraction)**  
The CLI (`python -m refinery`) only runs **triage** and does not run extraction. To get JSON into `.refinery/extractions/`, use the batch script (triage + extraction in one go):

```bash
uv run python scripts/onboard_documents.py data/ --max-docs 12
```

This writes profiles to `.refinery/profiles/` and extractions to `.refinery/extractions/`. Option `--no-save` runs without writing (dry run).

**Step 2 — Build PageIndex and example Q&A**  
Build trees and ingest LDUs into the vector store, then generate 12 example Q&A with full ProvenanceChain:

```bash
uv run python scripts/build_artifacts.py --extractions-dir .refinery/extractions --output-dir .refinery/pageindex --ingest --max-docs 12
uv run python scripts/build_artifacts.py --example-qa-out .refinery/example_qa/example_qa.json --max-docs 12
```

Or in one run (build + ingest + example Q&A): add both `--ingest` and `--example-qa-out` to the first command.

**Step 3 — Precision measurement and REPORT.md**  
Run the precision script using a real extraction file (replace `{DOC_ID}` with the stem of a JSON in `.refinery/extractions/`, e.g. `305dd23363bce7af508e746ed3f83cca8a9c6ea9838cef21baf19934ef924a62`), then record the delta in REPORT.md:

```bash
uv run python scripts/measure_retrieval_precision.py --extraction .refinery/extractions/{DOC_ID}.json --labeled scripts/labeled_queries.json
```

A positive delta (e.g. +15.00%) demonstrates that PageIndex traversal improves retrieval over naive vector search.
