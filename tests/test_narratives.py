"""
tests/test_narratives.py
========================
Narrative gates (NARR-01, NARR-04) and deliverable smoke checks.

Run gate tests:
  pytest tests/test_narratives.py::test_narr01 tests/test_narratives.py::test_narr04 -v

OpenRouter: use --llm openrouter or set OPENROUTER_API_KEY in .env (auto picks OpenRouter).
Tests default to stub LLM (no API calls) unless PYTEST_LLM=openrouter.
"""
from __future__ import annotations

import asyncio
import importlib.util
import os
import sys
from datetime import datetime
from decimal import Decimal
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

ROOT = Path(__file__).resolve().parent.parent


def _load_pipeline_stub_llm():
    spec = importlib.util.spec_from_file_location(
        "run_pipeline_cli", ROOT / "scripts" / "run_pipeline.py"
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod.PipelineStubLLM, mod._make_llm_client, mod._default_model_for_backend, mod._resolve_llm_backend


def _llm_for_test():
    """Stub by default; set PYTEST_LLM=openrouter to exercise OpenRouter from .env."""
    mode = os.environ.get("PYTEST_LLM", "stub").lower()
    PipelineStubLLM, _make, _default, _resolve = _load_pipeline_stub_llm()
    if mode == "openrouter":
        backend = _resolve("openrouter")
        return _make(backend), _default(backend)
    return PipelineStubLLM(), "local-stub"


async def _seed_minimal_for_credit(store, app_id: str, applicant_id: str) -> None:
    """Minimum loan + docpkg streams so CreditAnalysisAgent can complete."""
    await store.append(
        f"loan-{app_id}",
        [
            {
                "event_type": "ApplicationSubmitted",
                "event_version": 1,
                "payload": {
                    "application_id": app_id,
                    "applicant_id": applicant_id,
                    "requested_amount_usd": "500000",
                    "loan_purpose": "working_capital",
                    "loan_term_months": 36,
                    "submission_channel": "WEB",
                    "contact_email": "t@example.com",
                    "contact_name": "T",
                    "submitted_at": datetime.now().isoformat(),
                    "application_reference": f"REF-{app_id}",
                },
            },
            {
                "event_type": "CreditAnalysisRequested",
                "event_version": 1,
                "payload": {
                    "application_id": app_id,
                    "requested_at": datetime.now().isoformat(),
                    "requested_by": "test",
                    "priority": "NORMAL",
                },
            },
        ],
        expected_version=-1,
    )
    await store.append(
        f"docpkg-{app_id}",
        [
            {
                "event_type": "ExtractionCompleted",
                "event_version": 1,
                "payload": {
                    "package_id": app_id,
                    "document_id": "doc-is",
                    "document_type": "income_statement",
                    "facts": {
                        "total_revenue": Decimal("2500000"),
                        "net_income": Decimal("200000"),
                        "total_assets": Decimal("5000000"),
                    },
                    "raw_text_length": 1000,
                    "tables_extracted": 1,
                    "processing_ms": 100,
                    "completed_at": datetime.now().isoformat(),
                },
            },
            {
                "event_type": "PackageReadyForAnalysis",
                "event_version": 1,
                "payload": {
                    "package_id": app_id,
                    "application_id": app_id,
                    "documents_processed": 2,
                    "has_quality_flags": False,
                    "quality_flag_count": 0,
                    "ready_at": datetime.now().isoformat(),
                },
            },
        ],
        expected_version=-1,
    )


@pytest.mark.asyncio
async def test_narr01():
    """
    NARR-01: Two CreditAnalysisAgent runs for the same application (concurrent tasks).
    Expected: exactly one CreditAnalysisCompleted on credit stream (serialized per app + skip duplicate).
    """
    from ledger.agents.credit_analysis_agent import CreditAnalysisAgent
    from ledger.event_store import InMemoryEventStore

    store = InMemoryEventStore()
    app_id = "NARR-01-TEST"
    await _seed_minimal_for_credit(store, app_id, "COMP-NARR01")
    client, model = _llm_for_test()

    a1 = CreditAnalysisAgent(
        "credit-a", "credit_analysis", store, None, client, model,
        applicant_id_override="COMP-NARR01",
    )
    a2 = CreditAnalysisAgent(
        "credit-b", "credit_analysis", store, None, client, model,
        applicant_id_override="COMP-NARR01",
    )
    await asyncio.gather(
        a1.process_application(app_id),
        a2.process_application(app_id),
    )
    credit = await store.load_stream(f"credit-{app_id}")
    completed = [e for e in credit if e.get("event_type") == "CreditAnalysisCompleted"]
    assert len(completed) == 1, f"Expected 1 CreditAnalysisCompleted, got {len(completed)}"


@pytest.mark.asyncio
async def test_narr04():
    """
    NARR-04: Montana jurisdiction triggers REG-003 hard block.
    Expected: ComplianceRuleFailed(REG-003), no DecisionGenerated, ApplicationDeclined with adverse action.
    """
    from ledger.agents.stub_agents import ComplianceAgent
    from ledger.event_store import InMemoryEventStore
    from ledger.registry.client import CompanyProfile

    class MTRegistry:
        async def get_company(self, company_id: str):
            return CompanyProfile(
                company_id=company_id,
                name="Montana Borrower LLC",
                industry="Retail",
                naics="444110",
                jurisdiction="MT",
                legal_type="LLC",
                founded_year=2020,
                employee_count=12,
                risk_segment="MEDIUM",
                trajectory="STABLE",
                submission_channel="WEB",
                ip_region="US",
            )

        async def get_compliance_flags(self, company_id: str, active_only: bool = False):
            return []

    app_id = "NARR-04-TEST"
    store = InMemoryEventStore()
    await store.append(
        f"loan-{app_id}",
        [
            {
                "event_type": "ApplicationSubmitted",
                "event_version": 1,
                "payload": {
                    "application_id": app_id,
                    "applicant_id": "COMP-MT-01",
                    "requested_amount_usd": "100000",
                    "loan_purpose": "working_capital",
                    "loan_term_months": 24,
                    "submission_channel": "WEB",
                    "contact_email": "x@example.com",
                    "contact_name": "X",
                    "submitted_at": datetime.now().isoformat(),
                    "application_reference": "REF-MT",
                },
            },
            {
                "event_type": "ComplianceCheckRequested",
                "event_version": 1,
                "payload": {
                    "application_id": app_id,
                    "requested_at": datetime.now().isoformat(),
                    "triggered_by_event_id": "fraud-sess",
                    "regulation_set_version": "2026-Q1-v1",
                    "rules_to_evaluate": ["REG-001", "REG-002", "REG-003"],
                },
            },
        ],
        expected_version=-1,
    )

    client, model = _llm_for_test()
    agent = ComplianceAgent(
        "compliance-narr04",
        "compliance",
        store,
        MTRegistry(),
        client,
        model,
    )
    await agent.process_application(app_id)

    loan = await store.load_stream(f"loan-{app_id}")
    assert not any(e.get("event_type") == "DecisionGenerated" for e in loan)

    declined = [e for e in loan if e.get("event_type") == "ApplicationDeclined"]
    assert len(declined) == 1
    dp = declined[0].get("payload") or {}
    assert dp.get("adverse_action_notice_required") is True

    comp = await store.load_stream(f"compliance-{app_id}")
    failed = [
        e for e in comp
        if e.get("event_type") == "ComplianceRuleFailed"
        and (e.get("payload") or {}).get("rule_id") == "REG-003"
    ]
    assert len(failed) >= 1
    assert (failed[0].get("payload") or {}).get("is_hard_block") is True


@pytest.mark.asyncio
async def test_deliverable_five_terminal_states():
    """
    At least 5 applications reach APPROVED or DECLINED (orchestrator stub path, in-memory).
    """
    from ledger.agents.stub_agents import DecisionOrchestratorAgent
    from ledger.event_store import InMemoryEventStore
    from ledger.schema.events import (
        ComplianceCheckCompleted,
        ComplianceVerdict,
        CreditAnalysisCompleted,
        CreditDecision,
        DecisionRequested,
        FraudScreeningCompleted,
        RiskTier,
    )

    client, model = _llm_for_test()
    terminal = 0
    for i in range(5):
        store = InMemoryEventStore()
        app_id = f"DELIVER-{i:03d}"
        sid = f"sess-prior-{i}"
        decision = CreditDecision(
            risk_tier=RiskTier.MEDIUM,
            recommended_limit_usd=Decimal("400000"),
            confidence=0.75,
            rationale="Test credit outcome.",
            key_concerns=[],
            data_quality_caveats=[],
            policy_overrides_applied=[],
        )
        credit_ev = CreditAnalysisCompleted(
            application_id=app_id,
            session_id=sid,
            decision=decision,
            model_version=model,
            model_deployment_id="dep-test",
            input_data_hash="abc",
            analysis_duration_ms=1,
            completed_at=datetime.now(),
        ).to_store_dict()
        fraud_ev = FraudScreeningCompleted(
            application_id=app_id,
            session_id=sid,
            fraud_score=0.15,
            risk_level="LOW",
            anomalies_found=0,
            recommendation="PROCEED",
            screening_model_version=model,
            input_data_hash="f",
            completed_at=datetime.now(),
        ).to_store_dict()
        comp_ev = ComplianceCheckCompleted(
            application_id=app_id,
            session_id=sid,
            rules_evaluated=6,
            rules_passed=6,
            rules_failed=0,
            rules_noted=1,
            has_hard_block=False,
            overall_verdict=ComplianceVerdict.CLEAR,
            completed_at=datetime.now(),
        ).to_store_dict()
        await store.append(f"credit-{app_id}", [credit_ev], expected_version=-1)
        await store.append(f"fraud-{app_id}", [fraud_ev], expected_version=-1)
        await store.append(f"compliance-{app_id}", [comp_ev], expected_version=-1)
        dr = DecisionRequested(
            application_id=app_id,
            requested_at=datetime.now(),
            all_analyses_complete=True,
            triggered_by_event_id=sid,
        ).to_store_dict()
        await store.append(f"loan-{app_id}", [dr], expected_version=-1)

        orch = DecisionOrchestratorAgent(
            "orch-deliver",
            "decision_orchestrator",
            store,
            None,
            client,
            model,
        )
        await orch.process_application(app_id)
        loan = await store.load_stream(f"loan-{app_id}")
        if any(
            e.get("event_type") in ("ApplicationApproved", "ApplicationDeclined")
            for e in loan
        ):
            terminal += 1

    assert terminal >= 5
