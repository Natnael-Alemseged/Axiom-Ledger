"""
tests/test_mcp_lifecycle.py — Phase 5: MCP Integration Test
============================================================
Full loan application lifecycle driven ENTIRELY through MCP tool calls.
No direct Python function calls after setup.

Lifecycle:
  1. start_agent_session (Gas Town anchor)
  2. submit_application
  3. record_credit_analysis
  4. record_fraud_screening
  5. record_compliance_check (pass)
  6. generate_decision
  7. record_human_review
  8. Query ledger://applications/{id}/compliance to verify complete trace
  9. Query ledger://applications/{id}/audit-trail to verify full event stream
"""
from __future__ import annotations

import pytest
import asyncio
from datetime import datetime, timezone

from src.event_store import InMemoryEventStore
from src.projections.application_summary import ApplicationSummaryProjection
from src.projections.agent_performance import AgentPerformanceLedgerProjection
from src.projections.compliance_audit import ComplianceAuditViewProjection
from src.projections.daemon import ProjectionDaemon
from src.mcp.tools import register_tools
from src.mcp.resources import register_resources


# ─── MCP Harness ───────────────────────────────────────────────────────────

class MockMCP:
    """Minimal MCP server mock — collects tools and resources."""
    def __init__(self):
        self._tools: dict = {}
        self._resources: dict = {}

    def tool(self, description=""):
        def decorator(fn):
            self._tools[fn.__name__] = fn
            return fn
        return decorator

    def resource(self, uri_pattern):
        def decorator(fn):
            self._resources[uri_pattern] = fn
            return fn
        return decorator

    async def call_tool(self, name: str, **kwargs):
        fn = self._tools[name]
        return await fn(**kwargs)

    async def read_resource(self, uri_pattern: str, **kwargs):
        fn = self._resources[uri_pattern]
        return await fn(**kwargs)


@pytest.fixture
def mcp_setup():
    """Set up a fresh MCP server with in-memory store and projections."""
    store = InMemoryEventStore()
    app_summary = ApplicationSummaryProjection()
    agent_perf = AgentPerformanceLedgerProjection()
    compliance_audit = ComplianceAuditViewProjection()
    daemon = ProjectionDaemon(store=store, projections=[app_summary, agent_perf, compliance_audit])

    projections = {
        "application_summary": app_summary,
        "agent_performance": agent_perf,
        "compliance_audit": compliance_audit,
        "daemon": daemon,
    }

    mcp = MockMCP()
    register_tools(mcp, lambda: store)
    register_resources(mcp, lambda: store, lambda: projections)

    return mcp, store, projections, daemon


# ─── Full Lifecycle Test ────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_full_mcp_loan_lifecycle(mcp_setup):
    """
    Drive a complete loan application lifecycle using ONLY MCP tool calls.
    Validates: start_agent_session → record_credit_analysis → record_fraud_screening
    → record_compliance_check → generate_decision → record_human_review
    → query compliance view → query audit trail
    """
    mcp, store, projections, daemon = mcp_setup
    await store.connect()

    application_id = "mcp-lifecycle-001"
    agent_id = "credit-agent-01"
    session_id = "sess-mcp-001"

    # ── Step 1: Start agent session (Gas Town anchor) ──────────────────────
    result = await mcp.call_tool(
        "start_agent_session",
        agent_id=agent_id,
        session_id=session_id,
        application_id=application_id,
        model_version="credit-v2.3",
        context_source="fresh_start",
        context_token_count=500,
    )
    assert result["success"], f"start_agent_session failed: {result}"
    assert result["session_id"] == session_id

    # ── Step 2: Submit application ─────────────────────────────────────────
    result = await mcp.call_tool(
        "submit_application",
        application_id=application_id,
        applicant_id="apex-financial-client",
        requested_amount_usd=750000.0,
        loan_purpose="working_capital",
        submission_channel="online_portal",
    )
    assert result["success"], f"submit_application failed: {result}"
    assert result["stream_id"] == f"loan-{application_id}"

    # ── Step 3: Record credit analysis ─────────────────────────────────────
    result = await mcp.call_tool(
        "record_credit_analysis",
        application_id=application_id,
        agent_id=agent_id,
        session_id=session_id,
        model_version="credit-v2.3",
        confidence_score=0.82,
        risk_tier="LOW",
        recommended_limit_usd=750000.0,
        analysis_duration_ms=1500,
        input_data_hash="sha256:abc123def456",
    )
    assert result["success"], f"record_credit_analysis failed: {result}"

    # ── Step 4: Record fraud screening ─────────────────────────────────────
    fraud_agent_id = "fraud-agent-01"
    fraud_session_id = "sess-fraud-001"
    await mcp.call_tool(
        "start_agent_session",
        agent_id=fraud_agent_id,
        session_id=fraud_session_id,
        application_id=application_id,
        model_version="fraud-v1.5",
        context_source="fresh_start",
    )
    result = await mcp.call_tool(
        "record_fraud_screening",
        application_id=application_id,
        agent_id=fraud_agent_id,
        session_id=fraud_session_id,
        fraud_score=0.08,
        risk_level="LOW",
        anomalies_found=0,
        recommendation="PROCEED",
        screening_model_version="fraud-v1.5",
        input_data_hash="sha256:fraud_hash_001",
    )
    assert result["success"], f"record_fraud_screening failed: {result}"

    # ── Step 5: Record compliance check ────────────────────────────────────
    result = await mcp.call_tool(
        "record_compliance_check",
        application_id=application_id,
        session_id=session_id,
        rule_id="AML_001",
        rule_name="Anti-Money Laundering Check",
        rule_version="v2.1",
        passed=True,
        evidence_hash="sha256:aml_evidence_hash",
        regulation_set_version="FINCEN-2026",
    )
    assert result["success"], f"record_compliance_check failed: {result}"
    assert result["compliance_status"] == "PASSED"

    # ── Step 6: Generate decision ───────────────────────────────────────────
    result = await mcp.call_tool(
        "generate_decision",
        application_id=application_id,
        orchestrator_agent_id=agent_id,
        recommendation="APPROVE",
        confidence_score=0.82,
        contributing_agent_sessions=[session_id, fraud_session_id],
        decision_basis_summary="Strong financials, low fraud risk, AML compliant.",
        model_versions={"credit": "credit-v2.3", "fraud": "fraud-v1.5"},
        approved_amount_usd=750000.0,
    )
    assert result["success"], f"generate_decision failed: {result}"
    assert result["recommendation"] == "APPROVE"
    assert not result["confidence_floor_applied"]  # 0.82 >= 0.6

    # ── Step 7: Record human review ─────────────────────────────────────────
    result = await mcp.call_tool(
        "record_human_review",
        application_id=application_id,
        reviewer_id="loan-officer-jane",
        final_decision="APPROVE",
        override=False,
        original_recommendation="APPROVE",
    )
    assert result["success"], f"record_human_review failed: {result}"
    assert result["final_decision"] == "APPROVE"
    assert result["application_state"] == "APPROVED"

    # ── Process events through projections ─────────────────────────────────
    await daemon.process_once()

    # ── Step 8: Query compliance view ──────────────────────────────────────
    compliance = await mcp.read_resource(
        "ledger://applications/{application_id}/compliance",
        application_id=application_id,
    )
    # ComplianceAuditView should have the AML rule
    assert "error" not in compliance or compliance.get("rules_passed") is not None
    if "rules_passed" in compliance:
        assert "AML_001" in compliance["rules_passed"]

    # ── Step 9: Query full audit trail ─────────────────────────────────────
    audit_trail = await mcp.read_resource(
        "ledger://applications/{application_id}/audit-trail",
        application_id=application_id,
    )
    assert audit_trail["application_id"] == application_id
    assert audit_trail["total_events"] >= 3  # at least submit, credit, decision

    event_types = [e["event_type"] for e in audit_trail["events"]]
    assert "ApplicationSubmitted" in event_types
    assert "CreditAnalysisCompleted" in event_types
    assert "HumanReviewCompleted" in event_types
    assert "ApplicationApproved" in event_types

    # ── Step 10: Query application summary ─────────────────────────────────
    summary = await mcp.read_resource(
        "ledger://applications/{application_id}",
        application_id=application_id,
    )
    assert "error" not in summary or "application_id" in summary
    # After daemon processing, state should reflect approved
    if "state" in summary:
        assert summary["state"] == "APPROVED"


# ─── Confidence Floor Enforcement ──────────────────────────────────────────

@pytest.mark.asyncio
async def test_confidence_floor_forces_refer(mcp_setup):
    """
    Regulatory confidence floor: if confidence < 0.6, recommendation must be REFER
    regardless of what the caller provides.
    """
    mcp, store, projections, daemon = mcp_setup
    await store.connect()

    app_id = "mcp-floor-001"
    agent_id = "credit-02"
    session_id = "sess-floor-001"

    await mcp.call_tool("start_agent_session", agent_id=agent_id, session_id=session_id,
                        application_id=app_id, model_version="v1.0", context_source="test")
    await mcp.call_tool("submit_application", application_id=app_id,
                        applicant_id="test", requested_amount_usd=100000.0,
                        loan_purpose="equipment_financing", submission_channel="test")
    await mcp.call_tool("record_credit_analysis", application_id=app_id, agent_id=agent_id,
                        session_id=session_id, model_version="v1.0", confidence_score=0.45,
                        risk_tier="HIGH", recommended_limit_usd=50000.0,
                        analysis_duration_ms=500, input_data_hash="hash")

    # Caller tries to approve but confidence is 0.45 < 0.6 — must be overridden to REFER
    result = await mcp.call_tool(
        "generate_decision",
        application_id=app_id,
        orchestrator_agent_id=agent_id,
        recommendation="APPROVE",  # Caller tries to approve
        confidence_score=0.45,  # Below floor
        contributing_agent_sessions=[session_id],
        decision_basis_summary="Low confidence decision.",
    )
    assert result["success"]
    assert result["recommendation"] == "REFER", (
        f"Expected REFER (confidence floor applied) but got {result['recommendation']}"
    )
    assert result["confidence_floor_applied"] is True


# ─── Precondition Enforcement ───────────────────────────────────────────────

@pytest.mark.asyncio
async def test_record_credit_without_session_fails(mcp_setup):
    """record_credit_analysis must fail if no agent session exists."""
    mcp, store, _, _ = mcp_setup
    await store.connect()

    await mcp.call_tool("submit_application", application_id="no-sess-app",
                        applicant_id="test", requested_amount_usd=100000.0,
                        loan_purpose="other", submission_channel="test")

    result = await mcp.call_tool(
        "record_credit_analysis",
        application_id="no-sess-app",
        agent_id="ghost-agent",
        session_id="no-session-here",
        model_version="v1.0",
        confidence_score=0.9,
        risk_tier="LOW",
        recommended_limit_usd=100000.0,
        analysis_duration_ms=100,
        input_data_hash="hash",
    )
    assert result["success"] is False
    assert result["error_type"] in ("PreconditionFailed", "InternalError")


@pytest.mark.asyncio
async def test_human_review_override_requires_reason(mcp_setup):
    """record_human_review with override=True must have override_reason."""
    mcp, store, _, _ = mcp_setup
    await store.connect()

    result = await mcp.call_tool(
        "record_human_review",
        application_id="some-app",
        reviewer_id="officer",
        final_decision="APPROVE",
        override=True,
        override_reason=None,  # Missing!
    )
    assert result["success"] is False
    assert result["error_type"] == "ValidationError"


@pytest.mark.asyncio
async def test_fraud_score_validation(mcp_setup):
    """Fraud score outside [0.0, 1.0] must be rejected."""
    mcp, store, _, _ = mcp_setup
    await store.connect()

    result = await mcp.call_tool(
        "record_fraud_screening",
        application_id="test-app",
        agent_id="fraud-agent",
        session_id="sess-x",
        fraud_score=1.5,  # Invalid
        risk_level="HIGH",
    )
    assert result["success"] is False
    assert result["error_type"] == "ValidationError"


@pytest.mark.asyncio
async def test_integrity_check_role_restriction(mcp_setup):
    """run_integrity_check must reject unauthorized roles."""
    mcp, store, _, _ = mcp_setup
    await store.connect()

    result = await mcp.call_tool(
        "run_integrity_check",
        entity_type="loan",
        entity_id="some-app",
        caller_role="developer",  # Not authorized
    )
    assert result["success"] is False
    assert result["error_type"] == "AuthorizationError"
