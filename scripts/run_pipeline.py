"""
Run ledger agents against an in-memory event store (no DB required).

Phases:
  document      — seed corpus + DocumentProcessingAgent (refinery extraction + quality LLM).
  all           — document + CreditAnalysisAgent.
  fraud         — FraudDetectionAgent (requires prior credit; FraudScreeningRequested on loan).
  compliance    — ComplianceAgent (requires prior fraud; ComplianceCheckRequested on loan).
  orchestrator  — DecisionOrchestratorAgent (loads seed or runs doc→credit→fraud→compliance if needed).
  full          — document → credit → fraud → compliance → orchestrator (all 5 agents).

Usage:
  .venv/bin/python scripts/run_pipeline.py --application APEX-0001 --phase all --company COMP-019
  .venv/bin/python scripts/run_pipeline.py --app APEX-0001 --phase document -v

LLM (--llm):
  auto       — OpenRouter key first, else Google Gemini key, else Anthropic, else stub (default).
  openrouter — require OPENROUTER_API_KEY (or sk-or-v1 key); quality + credit via OpenRouter.
  gemini     — require a real Google AI Studio key (GEMINI_API_KEY / GOOGLE_API_KEY, not OpenRouter).
  anthropic  — require ANTHROPIC_API_KEY (Claude).
  stub       — never call an API (deterministic JSON for quality + credit nodes).

  Optional: OPENROUTER_MODEL, GEMINI_MODEL (defaults in integration modules).

Logging:
  --log-level DEBUG / -v — adapter + agent DEBUG lines.
  --log-file pipeline.log — capture full flow (plain text; no ANSI in file).
  --no-color — plain stderr (also respect NO_COLOR=1).
  Colored stderr when TTY + colorlog installed (pip install colorlog).
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import sys
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from dotenv import load_dotenv

load_dotenv(ROOT / ".env")


def _configure_logging(level: str, log_file: str | None, *, color: bool | None) -> None:
    from ledger.logging_config import configure_apex_logging

    lvl = getattr(logging, level.upper(), logging.INFO)
    configure_apex_logging(lvl, log_file=log_file, color=color)


class PipelineStubLLM:
    """
    Local stub with the same ``messages.create`` surface as cloud clients:
    returns JSON for document quality vs credit prompts based on system text.
    """

    class Messages:
        async def create(self, model, max_tokens, system, messages):
            sys = (system or "").lower()
            if "financial document quality analyst" in sys:
                txt = (
                    '{"overall_confidence":0.86,"is_coherent":true,"anomalies":[],'
                    '"critical_missing_fields":[],"reextraction_recommended":false,'
                    '"auditor_notes":"Stub quality assessment (--llm stub or auto with no API keys)."}'
                )
            elif "commercial credit analyst" in sys:
                txt = """{
  "risk_tier": "MEDIUM",
  "recommended_limit_usd": 400000,
  "confidence": 0.72,
  "rationale": "Stub credit decision for offline pipeline. Use --llm openrouter / --llm gemini or set API keys.",
  "key_concerns": ["Applicant registry data is placeholder — tune applicant_id and registry client for real runs."],
  "data_quality_caveats": [],
  "policy_overrides_applied": []
}"""
            elif "financial fraud analyst" in sys:
                txt = """{
  "anomalies": [],
  "rationale": "Stub fraud analysis — no anomalies flagged."
}"""
            elif "compliance reporting assistant" in sys:
                txt = "Stub compliance summary: regulatory checks completed per policy."
            elif "senior loan officer" in sys or "synthesizing multi-agent" in sys:
                txt = """{
  "recommendation": "APPROVE",
  "approved_amount_usd": 400000,
  "confidence": 0.78,
  "executive_summary": "Stub orchestrator: credit, fraud, and compliance inputs support a routine approval subject to standard conditions.",
  "key_risks": ["Monitor financial reporting as conditions warrant."],
  "conditions": ["Monthly financial reporting required"]
}"""
            else:
                txt = "{}"
            return SimpleNamespace(
                content=[SimpleNamespace(text=txt)],
                usage=SimpleNamespace(input_tokens=120, output_tokens=80),
            )

    def __init__(self):
        self.messages = self.Messages()


def _resolve_llm_backend(llm_mode: str) -> str:
    from ledger.integrations.llm_key_utils import effective_google_generative_key
    from ledger.integrations.openrouter_llm import resolve_openrouter_api_key

    if llm_mode == "stub":
        return "stub"
    if llm_mode == "openrouter":
        if not resolve_openrouter_api_key():
            raise SystemExit(
                "OPENROUTER_API_KEY or OPENROUTER_KEY is required for --llm openrouter "
                "(OpenRouter keys look like sk-or-v1-...). "
                "Or use --llm auto if the key is only in .env under GEMINI_API_KEY by mistake."
            )
        return "openrouter"
    if llm_mode == "gemini":
        if not effective_google_generative_key():
            raise SystemExit(
                "A Google AI Studio / Gemini API key is required for --llm gemini "
                "(GEMINI_API_KEY or GOOGLE_API_KEY, typically starting with AIza). "
                "OpenRouter keys belong in OPENROUTER_API_KEY — use --llm openrouter instead."
            )
        return "gemini"
    if llm_mode == "anthropic":
        if not os.environ.get("ANTHROPIC_API_KEY"):
            raise SystemExit(
                "ANTHROPIC_API_KEY is required for --llm anthropic "
                "(or use --llm openrouter / --llm auto / --llm stub)."
            )
        return "anthropic"
    if llm_mode == "auto":
        if resolve_openrouter_api_key():
            return "openrouter"
        if effective_google_generative_key():
            return "gemini"
        if os.environ.get("ANTHROPIC_API_KEY"):
            return "anthropic"
        return "stub"
    raise SystemExit(f"Unknown --llm mode: {llm_mode}")


def _verify_llm_dependencies(backend: str) -> None:
    """Exit with a clear message if optional LLM packages are missing."""
    if backend == "gemini":
        try:
            import google.generativeai  # noqa: F401
        except ImportError:
            import sys

            exe = sys.executable
            ver = f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}"
            raise SystemExit(
                "Gemini backend requires the google-generativeai package for this interpreter.\n"
                f"  Running: {exe} (Python {ver})\n"
                "  Install into the SAME interpreter you use to run this script:\n"
                f"    {exe} -m pip install 'google-generativeai>=0.8.0,<1.0'\n"
                "    # or: python -m pip install -r requirements.txt\n"
                "  If pip showed packages as satisfied but import still fails, your `pip` and `python`\n"
                "  likely point to different versions (broken venv). Fix: recreate the venv, or align\n"
                "  .venv/bin/python and .venv/bin/pip (both should use the same python3.x)."
            )
    if backend == "anthropic":
        try:
            import anthropic  # noqa: F401
        except ImportError:
            raise SystemExit(
                "Anthropic backend requires the anthropic package.\n"
                "  .venv/bin/pip install -r requirements.txt"
            )
    if backend == "openrouter":
        try:
            import httpx  # noqa: F401
        except ImportError:
            raise SystemExit(
                "OpenRouter backend requires httpx (pulled in by anthropic).\n"
                "  .venv/bin/python -m pip install httpx"
            )


def _make_llm_client(backend: str):
    if backend == "stub":
        return PipelineStubLLM()
    if backend == "gemini":
        from ledger.integrations.gemini_llm import GeminiShimClient

        return GeminiShimClient()
    if backend == "anthropic":
        from anthropic import AsyncAnthropic

        return AsyncAnthropic(api_key=os.environ["ANTHROPIC_API_KEY"])
    if backend == "openrouter":
        from ledger.integrations.openrouter_llm import OpenRouterShimClient

        return OpenRouterShimClient()
    raise ValueError(f"Unknown LLM backend: {backend}")


def _seed_jsonl_record_matches_application(rec: dict, application_id: str) -> bool:
    """Match seed_events.jsonl lines to an application (stream suffix or payload)."""
    sid = rec.get("stream_id") or ""
    if sid.endswith(f"-{application_id}"):
        return True
    pl = rec.get("payload") or {}
    if pl.get("application_id") == application_id:
        return True
    return False


async def load_seed_jsonl_for_application(store, application_id: str, path: Path) -> int:
    """
    Replay matching lines from a JSONL seed file into the in-memory store (preserves file order / OCC).
    Returns number of events appended.
    """
    if not path.is_file():
        return 0
    n = 0
    with path.open(encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not _seed_jsonl_record_matches_application(rec, application_id):
                continue
            stream_id = rec.get("stream_id")
            if not stream_id:
                continue
            ev = {
                "event_type": rec["event_type"],
                "event_version": rec.get("event_version", 1),
                "payload": dict(rec.get("payload") or {}),
            }
            ver = await store.stream_version(stream_id)
            await store.append(stream_id, [ev], expected_version=ver)
            n += 1
    return n


async def _application_streams_empty(store, application_id: str) -> bool:
    for prefix in ("loan", "docpkg", "credit", "fraud", "compliance"):
        if await store.load_stream(f"{prefix}-{application_id}"):
            return False
    return True


async def _has_credit_analysis_completed(store, application_id: str) -> bool:
    ev = await store.load_stream(f"credit-{application_id}")
    return any(e.get("event_type") == "CreditAnalysisCompleted" for e in ev)


async def _has_fraud_completed(store, application_id: str) -> bool:
    ev = await store.load_stream(f"fraud-{application_id}")
    return any(e.get("event_type") == "FraudScreeningCompleted" for e in ev)


async def _has_compliance_completed(store, application_id: str) -> bool:
    ev = await store.load_stream(f"compliance-{application_id}")
    return any(e.get("event_type") == "ComplianceCheckCompleted" for e in ev)


async def ensure_orchestrator_prerequisites(
    store,
    application_id: str,
    company: str,
    applicant_id: str,
    llm_backend: str,
) -> None:
    """
    Orchestrator requires DecisionRequested on the loan stream (written by ComplianceAgent when
    there is no hard block). If missing, load data/seed_events.jsonl for this application_id, or
    run the same prerequisite chain as ``--phase full`` (or fraud/compliance only if credit is
    already complete).
    """
    log = logging.getLogger(__name__)

    def _has_decision_requested(loan_events: list) -> bool:
        return any(e.get("event_type") == "DecisionRequested" for e in loan_events)

    loan = await store.load_stream(f"loan-{application_id}")
    if _has_decision_requested(loan):
        return

    seed_path = ROOT / "data" / "seed_events.jsonl"
    n = await load_seed_jsonl_for_application(store, application_id, seed_path)
    if n:
        log.info(
            "orchestrator_prereq: loaded %s seed events from %s for application_id=%s",
            n,
            seed_path,
            application_id,
        )
    loan = await store.load_stream(f"loan-{application_id}")
    if _has_decision_requested(loan):
        return

    if await _application_streams_empty(store, application_id):
        log.info(
            "orchestrator_prereq: empty store — running document → credit → fraud → compliance "
            "before orchestrator (application_id=%s)",
            application_id,
        )
        await run_document_phase(store, application_id, company, llm_backend)
        await _ensure_application_submitted(store, application_id, applicant_id)
        await run_credit_phase(store, application_id, applicant_id, llm_backend)
        await run_fraud_phase(store, application_id, llm_backend)
        await run_compliance_phase(store, application_id, llm_backend)
    else:
        if not await _has_credit_analysis_completed(store, application_id):
            raise SystemExit(
                "Orchestrator phase needs prior credit analysis (and usually document processing). "
                "Run: --phase full\n"
                f"  or: --phase all --application {application_id}  then --phase fraud, compliance, orchestrator."
            )
        if not await _has_fraud_completed(store, application_id):
            log.info("orchestrator_prereq: running fraud + compliance (credit already complete)")
            await run_fraud_phase(store, application_id, llm_backend)
            await run_compliance_phase(store, application_id, llm_backend)
        elif not await _has_compliance_completed(store, application_id):
            log.info("orchestrator_prereq: running compliance only (fraud already complete)")
            await run_compliance_phase(store, application_id, llm_backend)
        else:
            raise SystemExit(
                "Loan stream has no DecisionRequested but credit/fraud/compliance streams look complete. "
                "Compliance may have hard-declined (ApplicationDeclined instead of DecisionRequested). "
                "Check compliance output or use a different application / company."
            )

    loan = await store.load_stream(f"loan-{application_id}")
    if not _has_decision_requested(loan):
        declined = any(e.get("event_type") == "ApplicationDeclined" for e in loan)
        raise SystemExit(
            "Orchestrator requires DecisionRequested on loan-* (from ComplianceAgent when there is no "
            f"hard compliance block). application_id={application_id}: DecisionRequested missing, "
            f"ApplicationDeclined on loan={declined}."
        )


def _default_model_for_backend(backend: str) -> str:
    if backend == "gemini":
        from ledger.integrations.gemini_llm import default_gemini_model

        return default_gemini_model()
    if backend == "openrouter":
        from ledger.integrations.openrouter_llm import default_openrouter_model

        return default_openrouter_model()
    if backend == "anthropic":
        return "claude-sonnet-4-20250514"
    return "local-stub"


def _resolve_doc_paths(company_folder: Path) -> dict[str, Path]:
    mapping = {
        "application_proposal": company_folder / "application_proposal.pdf",
        "income_statement": company_folder / "income_statement_2024.pdf",
        "balance_sheet": company_folder / "balance_sheet_2024.pdf",
    }
    missing = [k for k, p in mapping.items() if not p.is_file()]
    if missing:
        raise FileNotFoundError(
            f"Missing PDFs under {company_folder} for: {', '.join(missing)}. "
            f"Expected: {', '.join(str(mapping[m]) for m in missing)}"
        )
    return mapping


async def _seed_document_phase(
    store,
    application_id: str,
    company: str,
) -> None:
    """Append minimal events so DocumentProcessingAgent.validate_inputs succeeds."""
    corp = ROOT / "documents" / company
    paths = _resolve_doc_paths(corp)
    log = logging.getLogger(__name__)
    log.info(
        "seed_doc_phase application_id=%s company=%s corpus=%s",
        application_id,
        company,
        corp,
    )

    await store.append(
        f"docpkg-{application_id}",
        [
            {
                "event_type": "PackageCreated",
                "event_version": 1,
                "payload": {
                    "package_id": application_id,
                    "application_id": application_id,
                    "required_documents": [
                        "application_proposal",
                        "income_statement",
                        "balance_sheet",
                    ],
                    "created_at": datetime.now().isoformat(),
                },
            }
        ],
        expected_version=-1,
    )

    uploads = []
    for doc_type, path in paths.items():
        abs_path = path.resolve()
        doc_id = f"doc-{doc_type}-{application_id[:8]}"
        uploads.append(
            {
                "event_type": "DocumentUploaded",
                "event_version": 1,
                "payload": {
                    "application_id": application_id,
                    "document_id": doc_id,
                    "document_type": doc_type,
                    "document_format": "pdf",
                    "filename": path.name,
                    "file_path": str(abs_path),
                    "file_size_bytes": path.stat().st_size,
                    "file_hash": doc_id,
                    "fiscal_year": 2024,
                    "uploaded_at": datetime.now().isoformat(),
                    "uploaded_by": "run_pipeline",
                },
            }
        )
    await store.append(f"loan-{application_id}", uploads, expected_version=-1)
    log.info("seed_doc_phase_done loan_events=%s docpkg_events=1", len(uploads))


async def run_document_phase(
    store,
    application_id: str,
    company: str,
    llm_backend: str,
) -> None:
    from ledger.agents.stub_agents import DocumentProcessingAgent

    await _seed_document_phase(store, application_id, company)

    client = _make_llm_client(llm_backend)
    model = _default_model_for_backend(llm_backend)
    log = logging.getLogger(__name__)
    log.info("document_phase llm_backend=%s model=%s", llm_backend, model)

    agent = DocumentProcessingAgent(
        "agent-docproc-cli",
        "document_processing",
        store,
        registry=None,
        client=client,
        model=model,
    )
    await agent.process_application(application_id)

    docpkg = await store.load_stream(f"docpkg-{application_id}")
    loan = await store.load_stream(f"loan-{application_id}")
    log.info("--- summary: docpkg-%s ---", application_id)
    log.info("event_types=%s", [e["event_type"] for e in docpkg])
    log.info("--- summary: loan-%s (after document) ---", application_id)
    log.info("event_types=%s", [e["event_type"] for e in loan])


async def _ensure_application_submitted(
    store,
    application_id: str,
    applicant_id: str,
) -> None:
    """Append ApplicationSubmitted if missing so LoanApplicationAggregate and credit agent have context."""
    stream_id = f"loan-{application_id}"
    loan_events = await store.load_stream(stream_id)
    if any(e.get("event_type") == "ApplicationSubmitted" for e in loan_events):
        return
    ver = await store.stream_version(stream_id)
    await store.append(
        stream_id,
        [
            {
                "event_type": "ApplicationSubmitted",
                "event_version": 1,
                "payload": {
                    "application_id": application_id,
                    "applicant_id": applicant_id,
                    "requested_amount_usd": "500000",
                    "loan_purpose": "working_capital",
                    "loan_term_months": 36,
                    "submission_channel": "WEB",
                    "contact_email": "applicant@example.com",
                    "contact_name": "Applicant",
                    "submitted_at": datetime.now().isoformat(),
                    "application_reference": f"REF-{application_id}",
                },
            }
        ],
        expected_version=ver,
    )


async def run_credit_phase(
    store,
    application_id: str,
    applicant_id: str,
    llm_backend: str,
) -> None:
    from ledger.agents.credit_analysis_agent import CreditAnalysisAgent

    log = logging.getLogger(__name__)
    client = _make_llm_client(llm_backend)
    model = _default_model_for_backend(llm_backend)
    log.info(
        "credit_phase applicant_id=%s llm_backend=%s model=%s",
        applicant_id,
        llm_backend,
        model,
    )

    agent = CreditAnalysisAgent(
        "agent-credit-cli",
        "credit_analysis",
        store,
        registry=None,
        client=client,
        model=model,
        applicant_id_override=applicant_id,
    )
    await agent.process_application(application_id)

    credit = await store.load_stream(f"credit-{application_id}")
    loan = await store.load_stream(f"loan-{application_id}")
    log.info("--- summary: credit-%s ---", application_id)
    log.info("event_types=%s", [e["event_type"] for e in credit])
    log.info("--- summary: loan-%s (after credit) ---", application_id)
    log.info("event_types=%s", [e["event_type"] for e in loan])


async def run_fraud_phase(
    store,
    application_id: str,
    llm_backend: str,
) -> None:
    from ledger.agents.stub_agents import FraudDetectionAgent

    log = logging.getLogger(__name__)
    client = _make_llm_client(llm_backend)
    model = _default_model_for_backend(llm_backend)
    log.info("fraud_phase llm_backend=%s model=%s", llm_backend, model)
    agent = FraudDetectionAgent(
        "agent-fraud-cli",
        "fraud_detection",
        store,
        registry=None,
        client=client,
        model=model,
    )
    await agent.process_application(application_id)
    fraud = await store.load_stream(f"fraud-{application_id}")
    loan = await store.load_stream(f"loan-{application_id}")
    log.info("--- summary: fraud-%s ---", application_id)
    log.info("event_types=%s", [e["event_type"] for e in fraud])
    log.info("--- summary: loan-%s (after fraud) ---", application_id)
    log.info("event_types=%s", [e["event_type"] for e in loan])


async def run_compliance_phase(
    store,
    application_id: str,
    llm_backend: str,
) -> None:
    from ledger.agents.stub_agents import ComplianceAgent

    log = logging.getLogger(__name__)
    client = _make_llm_client(llm_backend)
    model = _default_model_for_backend(llm_backend)
    log.info("compliance_phase llm_backend=%s model=%s", llm_backend, model)
    agent = ComplianceAgent(
        "agent-compliance-cli",
        "compliance",
        store,
        registry=None,
        client=client,
        model=model,
    )
    await agent.process_application(application_id)
    comp = await store.load_stream(f"compliance-{application_id}")
    loan = await store.load_stream(f"loan-{application_id}")
    log.info("--- summary: compliance-%s ---", application_id)
    log.info("event_types=%s", [e["event_type"] for e in comp])
    log.info("--- summary: loan-%s (after compliance) ---", application_id)
    log.info("event_types=%s", [e["event_type"] for e in loan])


async def run_orchestrator_phase(
    store,
    application_id: str,
    llm_backend: str,
) -> None:
    from ledger.agents.stub_agents import DecisionOrchestratorAgent

    log = logging.getLogger(__name__)
    client = _make_llm_client(llm_backend)
    model = _default_model_for_backend(llm_backend)
    log.info("orchestrator_phase llm_backend=%s model=%s", llm_backend, model)
    agent = DecisionOrchestratorAgent(
        "agent-orch-cli",
        "decision_orchestrator",
        store,
        registry=None,
        client=client,
        model=model,
    )
    await agent.process_application(application_id)
    loan = await store.load_stream(f"loan-{application_id}")
    log.info("--- summary: loan-%s (after orchestrator) ---", application_id)
    log.info("event_types=%s", [e["event_type"] for e in loan])


async def main() -> None:
    p = argparse.ArgumentParser(description="Run Axiom Ledger pipeline (in-memory store).")
    p.add_argument("--application", "--app", dest="application", required=True, help="Application id (stream suffix)")
    p.add_argument(
        "--phase",
        default="all",
        choices=(
            "document",
            "all",
            "fraud",
            "compliance",
            "orchestrator",
            "full",
        ),
        help="Pipeline phase: document | all (doc+credit) | fraud | compliance | orchestrator | full (5 agents)",
    )
    p.add_argument(
        "--company",
        default="COMP-019",
        help="Folder under documents/<COMPANY> with required PDFs",
    )
    p.add_argument(
        "--applicant-id",
        default=None,
        help="Registry / credit applicant id (default: same as --company, e.g. COMP-019)",
    )
    p.add_argument(
        "--llm",
        default="auto",
        choices=("auto", "stub", "openrouter", "gemini", "anthropic"),
        help="auto: OpenRouter → Google Gemini → Anthropic → stub (default)",
    )
    p.add_argument("--log-level", default="INFO", help="DEBUG, INFO, WARNING, …")
    p.add_argument("--log-file", default=None, help="Optional log file path")
    p.add_argument(
        "--no-color",
        action="store_true",
        help="Disable ANSI colors on stderr (file logs are always plain)",
    )
    p.add_argument("-v", "--verbose", action="store_true", help="Shorthand for --log-level DEBUG")
    args = p.parse_args()

    _configure_logging(
        "DEBUG" if args.verbose else args.log_level,
        args.log_file,
        color=False if args.no_color else None,
    )

    log = logging.getLogger(__name__)
    llm_backend = _resolve_llm_backend(args.llm)
    _verify_llm_dependencies(llm_backend)
    if args.llm == "auto":
        from ledger.integrations.llm_key_utils import effective_google_generative_key
        from ledger.integrations.openrouter_llm import resolve_openrouter_api_key

        log.info(
            "llm auto: selected backend=%s (openrouter=%s google_gemini=%s anthropic=%s)",
            llm_backend,
            "set" if resolve_openrouter_api_key() else "not set",
            "set" if effective_google_generative_key() else "not set",
            "set" if os.environ.get("ANTHROPIC_API_KEY") else "not set",
        )

    applicant_id = args.applicant_id or args.company

    log.info(
        "run_pipeline start application_id=%s phase=%s company=%s applicant_id=%s cwd=%s",
        args.application,
        args.phase,
        args.company,
        applicant_id,
        ROOT,
    )

    from ledger.event_store import InMemoryEventStore

    store = InMemoryEventStore()

    if args.phase == "document":
        await run_document_phase(store, args.application, args.company, llm_backend)
    elif args.phase == "all":
        await run_document_phase(store, args.application, args.company, llm_backend)
        await _ensure_application_submitted(store, args.application, applicant_id)
        await run_credit_phase(store, args.application, applicant_id, llm_backend)
    elif args.phase == "fraud":
        await run_fraud_phase(store, args.application, llm_backend)
    elif args.phase == "compliance":
        await run_compliance_phase(store, args.application, llm_backend)
    elif args.phase == "orchestrator":
        await ensure_orchestrator_prerequisites(
            store, args.application, args.company, applicant_id, llm_backend
        )
        await run_orchestrator_phase(store, args.application, llm_backend)
    elif args.phase == "full":
        await run_document_phase(store, args.application, args.company, llm_backend)
        await _ensure_application_submitted(store, args.application, applicant_id)
        await run_credit_phase(store, args.application, applicant_id, llm_backend)
        await run_fraud_phase(store, args.application, llm_backend)
        await run_compliance_phase(store, args.application, llm_backend)
        await run_orchestrator_phase(store, args.application, llm_backend)
    else:
        raise SystemExit(f"Unhandled phase: {args.phase}")

    log.info("run_pipeline finished ok application_id=%s", args.application)


if __name__ == "__main__":
    asyncio.run(main())
