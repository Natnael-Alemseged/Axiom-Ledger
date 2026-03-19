# Axiom Ledger

Event-sourced lending pipeline: document intake, extraction, credit analysis, and downstream agents with an append-only event ledger.

## Quick Start
```bash
# 0. Python 3.14+: `asyncpg` is omitted automatically (no compatible build). Use 3.11–3.13 if you
#    need Postgres (EventStore.connect, datagen --db-url). Pipeline + InMemoryEventStore work on 3.14.

# 1. Install dependencies (use the SAME interpreter for pip and python — see troubleshooting below)
python -m pip install -r requirements.txt -r requirements-database.txt
# Optional: Docling layout extraction (large; includes PyTorch) — see Document Intelligence Refinery below
# python -m pip install -r requirements-refinery.txt
# Or core only: python -m pip install -r requirements.txt

# 2. Start PostgreSQL
docker run -d -e POSTGRES_PASSWORD=apex -e POSTGRES_DB=apex_ledger -p 5432:5432 postgres:16

# 3. Set environment
cp .env.example .env
# Edit .env — e.g. OPENROUTER_API_KEY (sk-or-v1-...) or a Google AI Studio key for direct Gemini

# 4. Generate all data (companies + documents + seed events → DB)
python datagen/generate_all.py --db-url postgresql://postgres:apex@localhost/apex_ledger

# 5. Validate schema (no DB needed)
python datagen/generate_all.py --skip-db --skip-docs --validate-only

# 6. Schema & generator tests
pytest tests/test_schema_and_generator.py -v

# 7. Event store (implement in ledger/event_store.py), then:
# pytest tests/test_event_store.py -v
```

**Troubleshooting (imports missing after `pip install`):** use `python -m pip install -r requirements.txt` so packages install into the **same** interpreter you run. If `.venv/bin/pip` and `.venv/bin/python` target different Python versions (broken venv), recreate the venv or relink `python`/`python3` to match `pip` (e.g. both `python3.14`).

## What Works Out of the Box
- Full event schema (45 event types) — `ledger/schema/events.py`
- Complete data generator (GAAP PDFs, Excel, CSV, 1,200+ seed events)
- Event simulator (all five agent pipelines, deterministic)
- Schema validator (validates all events against EVENT_REGISTRY)
- Starter tests (`test_schema_and_generator.py`): 10/10 passing

## Document Intelligence Refinery

`DocumentProcessingAgent` in `ledger/agents/stub_agents.py` is wired to a local adapter:
- `ledger/integrations/document_refinery_adapter.py`
- Vendor sources: `vendor/Document-Intelligence-Refinery`

The adapter:
- Imports refinery triage/extraction from the vendor package (`run_triage` + `run_extraction`)
- Normalizes extraction output to ledger `FinancialFacts`
- Enforces critical field handling (`field_confidence[field] = 0.0` and `extraction_notes`)
- Loads top-level `.env` automatically. **OpenRouter** keys (`sk-or-v1-...`) go in `OPENROUTER_API_KEY`; **Google** keys (often `AIza...`) use `GEMINI_API_KEY` / `GOOGLE_API_KEY`. Mis-labeled OpenRouter keys are not sent to Google’s API.
- Prefers the Gemini provider path when a **valid Google** key is present
- Falls back to fast-text extraction when optional layout/vision dependencies are not installed

### Setup for local integration runs

```bash
# 1) Core ledger + PDF validation
.venv/bin/python -m pip install -r requirements.txt

# 2) Full refinery stack (Docling layout, not only fast-text fallback)
#    Installs torch, docling, chromadb, sentence-transformers, etc. — first run can download models.
.venv/bin/python -m pip install -r requirements-refinery.txt
# Equivalent: .venv/bin/python -m pip install -e "vendor/Document-Intelligence-Refinery"
```

After installing the refinery extras (`requirements-refinery.txt`), extraction should use **`strategy=layout`** (see pipeline logs) instead of `fast_text_fallback` when Docling is available.

### Quick smoke test: adapter only

```bash
.venv/bin/python - <<'PY'
import asyncio
from ledger.integrations.document_refinery_adapter import extract_financial_facts

async def main():
    out = await extract_financial_facts(
        "documents/COMP-019/income_statement_2024.pdf",
        "income_statement",
    )
    print("strategy:", out["strategy_used"])
    print("revenue:", out["facts"].get("total_revenue"))
    print("net_income:", out["facts"].get("net_income"))
    print("notes:", out["facts"].get("extraction_notes"))

asyncio.run(main())
PY
```

### CLI: `scripts/run_pipeline.py` (document → optional credit)

From the repo root (with PDFs under `documents/<COMPANY>/`):

```bash
# Default: --phase all runs DocumentProcessingAgent then CreditAnalysisAgent on the same in-memory store.
# --llm auto: OpenRouter → Google Gemini → Anthropic → stub
.venv/bin/python scripts/run_pipeline.py --application APEX-0001 --company COMP-019

# OpenRouter (keys like sk-or-v1-... in OPENROUTER_API_KEY):
.venv/bin/python scripts/run_pipeline.py --app APEX-0001 --company COMP-019 --llm openrouter
# Optional: OPENROUTER_MODEL=google/gemini-2.0-flash-001

# Direct Google Gemini API (AI Studio key, not OpenRouter):
.venv/bin/python scripts/run_pipeline.py --app APEX-0001 --company COMP-019 --llm gemini

# Optional model override:
# GEMINI_MODEL=gemini-2.5-flash .venv/bin/python scripts/run_pipeline.py --app APEX-0001 --llm gemini

# Documents only (no credit analysis):
.venv/bin/python scripts/run_pipeline.py --app APEX-0001 --phase document --company COMP-019

# Trace flow + log file:
.venv/bin/python scripts/run_pipeline.py --app APEX-0001 --company COMP-019 -v --log-file pipeline.log

# Claude instead of Gemini:
.venv/bin/python scripts/run_pipeline.py --app APEX-0001 --company COMP-019 --llm anthropic

# Credit agent applicant id (defaults to same as --company):
.venv/bin/python scripts/run_pipeline.py --app APEX-0001 --company COMP-019 --applicant-id COMP-019
```

### End-to-end smoke test: `DocumentProcessingAgent` (inline)

Alternatively, run this in-memory smoke test:

```bash
.venv/bin/python - <<'PY'
import asyncio
from datetime import datetime
from types import SimpleNamespace
from ledger.agents.stub_agents import DocumentProcessingAgent
from ledger.event_store import InMemoryEventStore

class DummyMessages:
    async def create(self, model, max_tokens, system, messages):
        txt = '{"overall_confidence":0.86,"is_coherent":true,"anomalies":[],"critical_missing_fields":[],"reextraction_recommended":false,"auditor_notes":"Coherent extraction."}'
        return SimpleNamespace(content=[SimpleNamespace(text=txt)], usage=SimpleNamespace(input_tokens=120, output_tokens=60))

class DummyClient:
    def __init__(self): self.messages = DummyMessages()

async def main():
    store = InMemoryEventStore()
    app_id = "APEX-TEST-001"
    await store.append(f"docpkg-{app_id}", [{
        "event_type":"PackageCreated","event_version":1,
        "payload":{"package_id":app_id,"application_id":app_id,"required_documents":["application_proposal","income_statement","balance_sheet"],"created_at":datetime.now().isoformat()}
    }], expected_version=-1)
    uploads = []
    for doc_id, doc_type, file_name in [
        ("doc-prop","application_proposal","application_proposal.pdf"),
        ("doc-is","income_statement","income_statement_2024.pdf"),
        ("doc-bs","balance_sheet","balance_sheet_2024.pdf"),
    ]:
        uploads.append({
            "event_type":"DocumentUploaded","event_version":1,
            "payload":{"application_id":app_id,"document_id":doc_id,"document_type":doc_type,"document_format":"pdf","filename":file_name,"file_path":f"documents/COMP-019/{file_name}","file_size_bytes":1,"file_hash":doc_id,"fiscal_year":2024,"uploaded_at":datetime.now().isoformat(),"uploaded_by":"applicant"}
        })
    await store.append(f"loan-{app_id}", uploads, expected_version=-1)
    agent = DocumentProcessingAgent("agent-docproc-test","document_processing",store,registry=None,client=DummyClient())
    await agent.process_application(app_id)
    print([e["event_type"] for e in await store.load_stream(f"docpkg-{app_id}")])
    print([e["event_type"] for e in await store.load_stream(f"loan-{app_id}")])

asyncio.run(main())
PY
```

Expected event sequence:
- `docpkg-{id}`: `DocumentFormatValidated` (x3), `ExtractionStarted` (x2), `ExtractionCompleted` (x2), `QualityAssessmentCompleted`, `PackageReadyForAnalysis`
- `loan-{id}`: `CreditAnalysisRequested`

### How to test with your own `documents/` corpus

- Pick any company folder under `documents/COMP-*` with:
  - `application_proposal.pdf`
  - `income_statement_2024.pdf`
  - `balance_sheet_2024.pdf`
- Reuse the end-to-end smoke script above and only change the `file_path` folder
- For multiple companies, loop over folders and assert:
  - `ExtractionCompleted` exists for both income statement and balance sheet
  - `QualityAssessmentCompleted` exists
  - `PackageReadyForAnalysis` exists
  - `CreditAnalysisRequested` was appended to the loan stream

## Implementation roadmap

| Component | File | Track |
|-----------|------|--------|
| EventStore | `ledger/event_store.py` | Core persistence |
| ApplicantRegistryClient | `ledger/registry/client.py` | Registry |
| Domain aggregates | `ledger/domain/aggregates/` | Domain model |
| DocumentProcessingAgent | `ledger/agents/base_agent.py` | Documents |
| CreditAnalysisAgent | `ledger/agents/base_agent.py` | Credit (reference) |
| FraudDetectionAgent | `ledger/agents/base_agent.py` | Fraud |
| ComplianceAgent | `ledger/agents/base_agent.py` | Compliance |
| DecisionOrchestratorAgent | `ledger/agents/base_agent.py` | Orchestration |
| Projections + daemon | `ledger/projections/` | Read models |
| Upcasters | `ledger/upcasters.py` | Schema migration |
| MCP server | `ledger/mcp_server.py` | Tooling |

## Tests (suggested order)

```bash
pytest tests/test_schema_and_generator.py -v
pytest tests/test_event_store.py -v
pytest tests/test_domain.py -v
pytest tests/test_narratives.py -v
pytest tests/test_projections.py -v
pytest tests/test_mcp.py -v
```
