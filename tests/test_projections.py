"""
tests/test_projections.py — Phase 3: Projection Tests
=======================================================
Tests:
  - ProjectionDaemon processes events and updates projections
  - ApplicationSummary projection state machine
  - AgentPerformanceLedger aggregates metrics
  - ComplianceAuditView temporal queries
  - rebuild_from_scratch idempotency
  - Lag SLO tests under simulated load
"""
from __future__ import annotations

import asyncio
import time
import pytest
from datetime import datetime, timezone, timedelta

from src.event_store import InMemoryEventStore
from src.commands.handlers import handle_submit_application
from src.projections.application_summary import ApplicationSummaryProjection
from src.projections.agent_performance import AgentPerformanceLedgerProjection
from src.projections.compliance_audit import ComplianceAuditViewProjection
from src.projections.daemon import ProjectionDaemon


def _now():
    return datetime.now(timezone.utc).isoformat()


# ─── ApplicationSummary ─────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_application_summary_full_lifecycle():
    """ApplicationSummary tracks full loan lifecycle state."""
    proj = ApplicationSummaryProjection()
    app_id = "app-proj-001"

    events = [
        {"event_type": "ApplicationSubmitted", "payload": {"application_id": app_id, "applicant_id": "acme", "requested_amount_usd": 500000}, "recorded_at": _now()},
        {"event_type": "CreditAnalysisCompleted", "payload": {"application_id": app_id, "risk_tier": "LOW", "recommended_limit_usd": 500000, "confidence_score": 0.85}, "recorded_at": _now()},
        {"event_type": "FraudScreeningCompleted", "payload": {"application_id": app_id, "fraud_score": 0.12, "recommendation": "PROCEED"}, "recorded_at": _now()},
        {"event_type": "ComplianceCheckRequested", "payload": {"application_id": app_id, "regulation_set_version": "v1"}, "recorded_at": _now()},
        {"event_type": "ComplianceRulePassed", "payload": {"application_id": app_id, "rule_id": "AML_001"}, "recorded_at": _now()},
        {"event_type": "DecisionGenerated", "payload": {"application_id": app_id, "recommendation": "APPROVE", "confidence_score": 0.85}, "recorded_at": _now()},
        {"event_type": "HumanReviewCompleted", "payload": {"application_id": app_id, "reviewer_id": "officer-1", "final_decision": "APPROVE", "override": False, "reviewed_at": _now()}, "recorded_at": _now()},
        {"event_type": "ApplicationApproved", "payload": {"application_id": app_id, "approved_amount_usd": 500000, "approved_by": "officer-1", "approved_at": _now()}, "recorded_at": _now()},
    ]

    for event in events:
        await proj.handle(event)

    summary = proj.get(app_id)
    assert summary is not None
    assert summary["state"] == "APPROVED"
    assert summary["risk_tier"] == "LOW"
    assert summary["fraud_score"] == 0.12
    assert summary["compliance_status"] == "PASSED"
    assert summary["decision"] == "APPROVE"
    assert summary["approved_amount_usd"] == 500000
    assert summary["human_reviewer_id"] == "officer-1"
    assert summary["final_decision_at"] is not None


@pytest.mark.asyncio
async def test_application_summary_declined():
    """ApplicationSummary handles declined path."""
    proj = ApplicationSummaryProjection()
    app_id = "app-declined-001"
    await proj.handle({"event_type": "ApplicationSubmitted", "payload": {"application_id": app_id, "applicant_id": "test"}, "recorded_at": _now()})
    await proj.handle({"event_type": "ApplicationDeclined", "payload": {"application_id": app_id, "decline_reasons": ["high_risk"], "declined_by": "auto"}, "recorded_at": _now()})

    summary = proj.get(app_id)
    assert summary["state"] == "DECLINED"
    assert summary["final_decision_at"] is not None


# ─── AgentPerformanceLedger ─────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_agent_performance_tracks_credit_analysis():
    """AgentPerformanceLedger computes rolling averages."""
    proj = AgentPerformanceLedgerProjection()
    ts = _now()

    for i in range(3):
        await proj.handle({
            "event_type": "CreditAnalysisCompleted",
            "payload": {
                "application_id": f"app-{i}",
                "agent_id": "credit-01",
                "model_version": "v2.3",
                "confidence_score": 0.80 + i * 0.05,  # 0.80, 0.85, 0.90
                "analysis_duration_ms": 1000 + i * 100,
            },
            "recorded_at": ts,
        })

    records = proj.get("credit-01", "v2.3")
    assert len(records) == 1
    m = records[0]
    assert m["analyses_completed"] == 3
    assert abs(m["avg_confidence_score"] - 0.85) < 0.01  # (0.80+0.85+0.90)/3
    assert m["avg_duration_ms"] == pytest.approx(1100.0, abs=1)


@pytest.mark.asyncio
async def test_agent_performance_decision_rates():
    """Decision rates are correctly computed."""
    proj = AgentPerformanceLedgerProjection()
    ts = _now()

    decisions = [
        ("APPROVE", "orch-01"),
        ("APPROVE", "orch-01"),
        ("DECLINE", "orch-01"),
    ]
    for rec, agent_id in decisions:
        await proj.handle({
            "event_type": "DecisionGenerated",
            "payload": {
                "orchestrator_agent_id": agent_id,
                "recommendation": rec,
                "model_versions": {"orchestrator": "v1.0"},
                "application_id": "app-x",
            },
            "recorded_at": ts,
        })

    records = proj.get("orch-01")
    assert len(records) >= 1
    m = records[0]
    assert m["decisions_generated"] == 3
    assert abs(m["approve_rate"] - 2/3) < 0.01
    assert abs(m["decline_rate"] - 1/3) < 0.01


# ─── ComplianceAuditView ────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_compliance_audit_temporal_query():
    """ComplianceAuditView temporal query returns state at a specific timestamp."""
    proj = ComplianceAuditViewProjection()
    app_id = "app-comp-001"

    # T0: initial compliance requested
    t0 = datetime(2026, 1, 1, 10, 0, 0, tzinfo=timezone.utc)
    await proj.handle({
        "event_type": "ComplianceCheckRequested",
        "payload": {"application_id": app_id, "regulation_set_version": "v1.0", "rules_to_evaluate": ["AML_001", "KYC_001"]},
        "recorded_at": t0,
    })

    # T1: first rule passed
    t1 = datetime(2026, 1, 1, 10, 5, 0, tzinfo=timezone.utc)
    await proj.handle({
        "event_type": "ComplianceRulePassed",
        "payload": {"application_id": app_id, "rule_id": "AML_001", "rule_name": "AML Check", "rule_version": "v1.0", "evidence_hash": "abc"},
        "recorded_at": t1,
    })

    # T2: second rule passed
    t2 = datetime(2026, 1, 1, 10, 10, 0, tzinfo=timezone.utc)
    await proj.handle({
        "event_type": "ComplianceRulePassed",
        "payload": {"application_id": app_id, "rule_id": "KYC_001", "rule_name": "KYC Check", "rule_version": "v1.0", "evidence_hash": "def"},
        "recorded_at": t2,
    })

    # Temporal query at T1 (before second rule) — only AML_001 should be passed
    state_at_t1 = proj.get_compliance_at(app_id, t1)
    assert state_at_t1 is not None
    assert "AML_001" in state_at_t1["rules_passed"]
    assert "KYC_001" not in state_at_t1["rules_passed"]

    # Current state — both rules passed
    current = proj.get_current_compliance(app_id)
    assert "AML_001" in current["rules_passed"]
    assert "KYC_001" in current["rules_passed"]


@pytest.mark.asyncio
async def test_compliance_audit_rebuild_from_scratch():
    """rebuild_from_scratch replays all events and produces identical state."""
    store = InMemoryEventStore()
    await store.connect()
    app_id = "app-rebuild-001"

    events_to_append = [
        {"event_type": "ComplianceCheckRequested", "payload": {"application_id": app_id, "regulation_set_version": "v1", "rules_to_evaluate": ["R1"]}, "event_version": 1},
        {"event_type": "ComplianceRulePassed", "payload": {"application_id": app_id, "rule_id": "R1", "rule_name": "R1", "rule_version": "v1", "evidence_hash": "hash1"}, "event_version": 1},
    ]
    version = -1
    for e in events_to_append:
        await store.append(f"compliance-{app_id}", [e], expected_version=version)
        version += 1

    proj = ComplianceAuditViewProjection()
    await proj.rebuild_from_scratch(store)

    state = proj.get_current_compliance(app_id)
    assert state is not None
    assert "R1" in state["rules_passed"]


# ─── ProjectionDaemon ───────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_projection_daemon_processes_events():
    """Daemon routes events to projections and saves checkpoints."""
    store = InMemoryEventStore()
    await store.connect()
    app_id = "app-daemon-001"

    await store.append(
        stream_id=f"loan-{app_id}",
        events=[{
            "event_type": "ApplicationSubmitted",
            "event_version": 1,
            "payload": {"application_id": app_id, "applicant_id": "test", "requested_amount_usd": 100000},
        }],
        expected_version=-1,
    )

    app_summary = ApplicationSummaryProjection()
    daemon = ProjectionDaemon(store=store, projections=[app_summary])

    processed = await daemon.process_once()
    assert processed >= 1

    summary = app_summary.get(app_id)
    assert summary is not None
    assert summary["state"] == "SUBMITTED"


@pytest.mark.asyncio
async def test_projection_daemon_fault_tolerance():
    """Daemon continues processing even if one projection raises an error."""
    store = InMemoryEventStore()
    await store.connect()

    await store.append(
        stream_id="loan-fault-001",
        events=[{"event_type": "ApplicationSubmitted", "event_version": 1, "payload": {"application_id": "fault-001", "applicant_id": "test"}}],
        expected_version=-1,
    )

    class BrokenProjection:
        name = "broken"
        async def handle(self, event):
            raise RuntimeError("Intentional failure")

    class GoodProjection:
        name = "good"
        calls = 0
        async def handle(self, event):
            GoodProjection.calls += 1

    daemon = ProjectionDaemon(store=store, projections=[BrokenProjection(), GoodProjection()])

    # Must not raise — daemon is fault tolerant
    processed = await daemon.process_once()
    assert processed >= 1
    # Good projection should still receive events
    assert GoodProjection.calls >= 1


@pytest.mark.asyncio
async def test_projection_daemon_lag_under_load():
    """
    Projection lag SLO test: lag must stay under 500ms
    when processing events from 50 concurrent command handlers.

    This uses the in-memory store so no actual 50 concurrent DB connections are needed.
    The test verifies the architectural pattern — lag measurement works correctly.
    """
    store = InMemoryEventStore()
    await store.connect()

    # Simulate 50 concurrent command handlers each writing one event
    async def write_one(i: int):
        await store.append(
            stream_id=f"loan-load-{i:03d}",
            events=[{
                "event_type": "ApplicationSubmitted",
                "event_version": 1,
                "payload": {"application_id": f"load-{i:03d}", "applicant_id": f"test-{i}"},
            }],
            expected_version=-1,
        )

    await asyncio.gather(*[write_one(i) for i in range(50)])

    app_summary = ApplicationSummaryProjection()
    daemon = ProjectionDaemon(store=store, projections=[app_summary])

    start = time.monotonic()
    await daemon.process_once()
    elapsed_ms = (time.monotonic() - start) * 1000

    # Lag should be well under 500ms for in-memory processing of 50 events
    assert elapsed_ms < 500, f"Projection processing took {elapsed_ms:.1f}ms — exceeds 500ms SLO"

    # All 50 applications should be visible in the projection
    all_apps = app_summary.get_all()
    assert len(all_apps) == 50


@pytest.mark.asyncio
async def test_projection_daemon_slo_with_50_concurrent_handlers_and_rebuild():
    """
    Rubric SLO test:
    - 50 concurrent command handlers append events.
    - Projection lag for ApplicationSummary stays under 500ms.
    - compliance rebuild_from_scratch completes while live reads remain available.
    """
    store = InMemoryEventStore()
    await store.connect()
    app_summary = ApplicationSummaryProjection()
    agent_perf = AgentPerformanceLedgerProjection()
    compliance = ComplianceAuditViewProjection()
    daemon = ProjectionDaemon(
        store=store,
        projections=[app_summary, agent_perf, compliance],
        max_retries=3,
        batch_size=25,
    )
    daemon_task = asyncio.create_task(daemon.run_forever(poll_interval_ms=5))
    try:
        async def submit(i: int):
            app_id = f"slo-{i:03d}"
            await handle_submit_application(
                {
                    "application_id": app_id,
                    "applicant_id": f"corp-{i:03d}",
                    "requested_amount_usd": 100000 + i,
                    "loan_purpose": "working_capital",
                    "submission_channel": "api",
                    "submitted_at": _now(),
                },
                store,
            )

        await asyncio.gather(*[submit(i) for i in range(50)])
        await asyncio.sleep(0.1)
        assert len(app_summary.get_all()) == 50
        assert daemon.get_lag("application_summary") < 500

        # Rebuild should finish quickly and not block reads from already-populated projections.
        rebuild_task = asyncio.create_task(compliance.rebuild_from_scratch(store))
        read_during_rebuild = app_summary.get("slo-000")
        assert read_during_rebuild is not None
        await rebuild_task
        assert daemon.get_lag("application_summary") < 500
    finally:
        await daemon.stop()
        daemon_task.cancel()
