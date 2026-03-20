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


async def _seed_credit_with_missing_ebitda(store, app_id: str, applicant_id: str) -> None:
    await store.append(
        f"loan-{app_id}",
        [
            {
                "event_type": "ApplicationSubmitted",
                "event_version": 1,
                "payload": {
                    "application_id": app_id,
                    "applicant_id": applicant_id,
                    "requested_amount_usd": "450000",
                    "loan_purpose": "equipment",
                    "loan_term_months": 36,
                    "submission_channel": "WEB",
                    "contact_email": "narr02@example.com",
                    "contact_name": "Narr02",
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
                    "document_id": "doc-is-missing-ebitda",
                    "document_type": "income_statement",
                    "facts": {
                        "total_revenue": Decimal("1800000"),
                        "net_income": Decimal("120000"),
                        "ebitda": None,
                        "total_assets": Decimal("3600000"),
                        "extraction_notes": ["ebitda"],
                    },
                    "raw_text_length": 500,
                    "tables_extracted": 1,
                    "processing_ms": 100,
                    "completed_at": datetime.now().isoformat(),
                },
            },
            {
                "event_type": "QualityAssessmentCompleted",
                "event_version": 1,
                "payload": {
                    "package_id": app_id,
                    "document_id": "doc-is-missing-ebitda",
                    "overall_confidence": 0.79,
                    "is_coherent": True,
                    "anomalies": [],
                    "critical_missing_fields": ["ebitda"],
                    "reextraction_recommended": False,
                    "auditor_notes": "EBITDA missing from extracted table.",
                    "assessed_at": datetime.now().isoformat(),
                },
            },
            {
                "event_type": "PackageReadyForAnalysis",
                "event_version": 1,
                "payload": {
                    "package_id": app_id,
                    "application_id": app_id,
                    "documents_processed": 1,
                    "has_quality_flags": True,
                    "quality_flag_count": 1,
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
async def test_narr02():
    """
    NARR-02: missing EBITDA should propagate as quality caveats and cap confidence.
    """
    from ledger.agents.credit_analysis_agent import CreditAnalysisAgent
    from ledger.event_store import InMemoryEventStore

    store = InMemoryEventStore()
    app_id = "NARR-02-TEST"
    await _seed_credit_with_missing_ebitda(store, app_id, "COMP-044")
    client, model = _llm_for_test()

    agent = CreditAnalysisAgent(
        "credit-narr02", "credit_analysis", store, None, client, model,
        applicant_id_override="COMP-044",
    )
    await agent.process_application(app_id)
    credit = await store.load_stream(f"credit-{app_id}")
    completed = [e for e in credit if e.get("event_type") == "CreditAnalysisCompleted"]
    assert len(completed) == 1
    decision = (completed[0].get("payload") or {}).get("decision") or {}
    assert float(decision.get("confidence") or 0.0) <= 0.75
    assert isinstance(decision.get("data_quality_caveats"), list)
    assert len(decision.get("data_quality_caveats") or []) > 0


@pytest.mark.asyncio
async def test_narr03():
    """
    NARR-03: Fraud agent crash after load_facts and recovery replay.
    """
    from ledger.agents.stub_agents import FraudDetectionAgent
    from ledger.event_store import InMemoryEventStore

    store = InMemoryEventStore()
    app_id = "NARR-03-TEST"
    await store.append(
        f"loan-{app_id}",
        [
            {
                "event_type": "ApplicationSubmitted",
                "event_version": 1,
                "payload": {
                    "application_id": app_id,
                    "applicant_id": "COMP-057",
                    "requested_amount_usd": "1100000",
                    "loan_purpose": "growth",
                    "loan_term_months": 48,
                    "submission_channel": "WEB",
                    "contact_email": "narr03@example.com",
                    "contact_name": "Narr03",
                    "submitted_at": datetime.now().isoformat(),
                    "application_reference": f"REF-{app_id}",
                },
            },
            {
                "event_type": "FraudScreeningRequested",
                "event_version": 1,
                "payload": {
                    "application_id": app_id,
                    "requested_at": datetime.now().isoformat(),
                    "triggered_by_event_id": "credit-session",
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
                    "document_id": "doc-fraud",
                    "document_type": "income_statement",
                    "facts": {"total_revenue": Decimal("1200000"), "net_income": Decimal("90000"), "total_assets": Decimal("2600000")},
                    "raw_text_length": 300,
                    "tables_extracted": 1,
                    "processing_ms": 90,
                    "completed_at": datetime.now().isoformat(),
                },
            }
        ],
        expected_version=-1,
    )

    client, model = _llm_for_test()
    crashed = FraudDetectionAgent("fraud-crash", "fraud_detection", store, None, client, model)
    crashed._simulate_crash_after_node("load_facts")
    with pytest.raises(Exception):
        await crashed.process_application(app_id)
    crashed_session = crashed.session_id

    failed_stream = await store.load_stream(f"agent-fraud_detection-{crashed_session}")
    failed_events = [e for e in failed_stream if e.get("event_type") == "AgentSessionFailed"]
    assert len(failed_events) == 1
    fp = failed_events[0].get("payload") or {}
    assert fp.get("recoverable") is True
    assert fp.get("last_successful_node") == "load_facts"

    recovered = FraudDetectionAgent("fraud-recovered", "fraud_detection", store, None, client, model)
    ctx = await recovered.recover_from_session(app_id, crashed_session)
    assert str(ctx.get("last_successful_node", "")).startswith("load_")
    await recovered.process_application(app_id)

    fraud_events = await store.load_stream(f"fraud-{app_id}")
    completed = [e for e in fraud_events if e.get("event_type") == "FraudScreeningCompleted"]
    assert len(completed) == 1

    recovered_stream = await store.load_stream(f"agent-fraud_detection-{recovered.session_id}")
    started = next((e for e in recovered_stream if e.get("event_type") == "AgentSessionStarted"), None)
    assert started is not None
    assert str(((started.get("payload") or {}).get("context_source") or "")).startswith("prior_session_replay:")
    assert any(e.get("event_type") == "AgentSessionRecovered" for e in recovered_stream)

    load_facts_nodes = [
        e
        for e in (failed_stream + recovered_stream)
        if e.get("event_type") == "AgentNodeExecuted"
        and ((e.get("payload") or {}).get("node_name") == "load_document_facts")
    ]
    assert len(load_facts_nodes) == 1


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
async def test_narr05():
    """
    NARR-05: orchestrator declines, human overrides to approve with constrained amount and conditions.
    """
    from types import SimpleNamespace
    from ledger.agents.stub_agents import DecisionOrchestratorAgent
    from ledger.event_store import InMemoryEventStore
    from ledger.schema.events import (
        ApplicationApproved,
        ComplianceCheckCompleted,
        ComplianceVerdict,
        CreditAnalysisCompleted,
        CreditDecision,
        DecisionRequested,
        FraudScreeningCompleted,
        HumanReviewCompleted,
        RiskTier,
    )

    class Narr05LLM:
        class Messages:
            async def create(self, model, max_tokens, system, messages):
                text = """{
  "recommendation": "DECLINE",
  "approved_amount_usd": null,
  "confidence": 0.82,
  "executive_summary": "Decline recommendation due to declining trajectory and leverage.",
  "key_risks": ["Declining revenue trajectory", "High leverage"],
  "conditions": []
}"""
                return SimpleNamespace(
                    content=[SimpleNamespace(text=text)],
                    usage=SimpleNamespace(input_tokens=120, output_tokens=80),
                )

        def __init__(self):
            self.messages = self.Messages()

    app_id = "NARR-05-TEST"
    sid = "sess-prior-n05"
    store = InMemoryEventStore()
    credit_ev = CreditAnalysisCompleted(
        application_id=app_id,
        session_id=sid,
        decision=CreditDecision(
            risk_tier=RiskTier.HIGH,
            recommended_limit_usd=Decimal("950000"),
            confidence=0.82,
            rationale="Declining revenue and leverage.",
            key_concerns=["Revenue trend down 8% YoY", "Leverage elevated"],
            data_quality_caveats=[],
            policy_overrides_applied=[],
        ),
        model_version="narr05-model",
        model_deployment_id="dep-n05",
        input_data_hash="n05",
        analysis_duration_ms=1,
        completed_at=datetime.now(),
    ).to_store_dict()
    fraud_ev = FraudScreeningCompleted(
        application_id=app_id,
        session_id=sid,
        fraud_score=0.22,
        risk_level="LOW",
        anomalies_found=0,
        recommendation="PROCEED",
        screening_model_version="narr05-model",
        input_data_hash="n05-f",
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
        "orch-narr05", "decision_orchestrator", store, None, Narr05LLM(), "narr05-model"
    )
    await orch.process_application(app_id)

    loan = await store.load_stream(f"loan-{app_id}")
    decisions = [e for e in loan if e.get("event_type") == "DecisionGenerated"]
    assert len(decisions) == 1
    dp = decisions[0].get("payload") or {}
    assert dp.get("recommendation") == "DECLINE"
    assert float(dp.get("confidence") or 0.0) == pytest.approx(0.82, rel=0, abs=1e-6)

    ver = await store.stream_version(f"loan-{app_id}")
    hr = HumanReviewCompleted(
        application_id=app_id,
        reviewer_id="LO-Sarah-Chen",
        override=True,
        original_recommendation="DECLINE",
        final_decision="APPROVE",
        override_reason="15-year customer, prior repayment history, collateral offered",
        reviewed_at=datetime.now(),
    ).to_store_dict()
    approved = ApplicationApproved(
        application_id=app_id,
        approved_amount_usd=Decimal("750000"),
        interest_rate_pct=10.25,
        term_months=36,
        conditions=[
            "Monthly revenue reporting for 12 months",
            "Personal guarantee from CEO",
        ],
        approved_by="LO-Sarah-Chen",
        effective_date=datetime.now().date().isoformat(),
        approved_at=datetime.now(),
    ).to_store_dict()
    await store.append(f"loan-{app_id}", [hr, approved], expected_version=ver)

    loan = await store.load_stream(f"loan-{app_id}")
    hr_done = [e for e in loan if e.get("event_type") == "HumanReviewCompleted"]
    approved_events = [e for e in loan if e.get("event_type") == "ApplicationApproved"]
    assert len(hr_done) == 1
    assert len(approved_events) == 1
    hp = hr_done[0].get("payload") or {}
    ap = approved_events[0].get("payload") or {}
    assert hp.get("override") is True
    assert hp.get("reviewer_id") == "LO-Sarah-Chen"
    assert Decimal(str(ap.get("approved_amount_usd"))) == Decimal("750000")
    assert len(ap.get("conditions") or []) == 2


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
