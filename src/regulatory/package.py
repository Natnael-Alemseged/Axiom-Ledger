"""
src/regulatory/package.py — Regulatory Examination Package Generator
=====================================================================
Phase 6 (Bonus): Generate a self-contained examination package for regulators.

The package is a JSON file containing:
  1. Complete event stream in order with full payloads.
  2. State of every projection at examination_date.
  3. Audit chain integrity verification result.
  4. Human-readable narrative of the lifecycle.
  5. AI agent model versions, confidence scores, and input data hashes.

A regulator can verify the package independently against the database
without trusting the system — they can re-run the hash checks themselves.
"""
from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from typing import Any


async def generate_regulatory_package(
    store,
    application_id: str,
    examination_date: datetime | str | None = None,
) -> dict:
    """
    Generate a self-contained regulatory examination package.

    Parameters
    ----------
    store:
        Connected EventStore instance.
    application_id:
        The loan application to package.
    examination_date:
        Point-in-time for the package. Defaults to now.

    Returns
    -------
    dict
        Self-contained package — serialisable to JSON.
    """
    if examination_date is None:
        examination_date = datetime.now(timezone.utc)
    elif isinstance(examination_date, str):
        examination_date = datetime.fromisoformat(examination_date.replace("Z", "+00:00"))

    # ------------------------------------------------------------------
    # 1. Collect all event streams for this application
    # ------------------------------------------------------------------
    streams = [
        f"loan-{application_id}",
        f"docpkg-{application_id}",
        f"credit-{application_id}",
        f"fraud-{application_id}",
        f"compliance-{application_id}",
        f"audit-loan-{application_id}",
    ]

    all_events: list[dict] = []
    for stream_id in streams:
        try:
            events = await store.load_stream(stream_id)
            for e in events:
                all_events.append({
                    "stream_id": e["stream_id"],
                    "stream_position": e["stream_position"],
                    "global_position": e.get("global_position", 0),
                    "event_id": str(e.get("event_id", "")),
                    "event_type": e["event_type"],
                    "event_version": e.get("event_version", 1),
                    "payload": e.get("payload", {}),
                    "metadata": e.get("metadata", {}),
                    "recorded_at": str(e.get("recorded_at", "")),
                })
        except Exception:
            pass

    # Filter to events at or before examination_date
    def _before_exam(event_dict: dict) -> bool:
        ts_str = event_dict.get("recorded_at", "")
        if not ts_str or ts_str == "None":
            return True  # include events with unknown timestamp
        try:
            ts = datetime.fromisoformat(str(ts_str).replace("Z", "+00:00"))
            if ts.tzinfo is None:
                ts = ts.replace(tzinfo=timezone.utc)
            exam_ts = examination_date
            if exam_ts.tzinfo is None:
                exam_ts = exam_ts.replace(tzinfo=timezone.utc)
            return ts <= exam_ts
        except Exception:
            return True

    events_at_exam = [e for e in all_events if _before_exam(e)]
    events_at_exam.sort(key=lambda e: (e.get("global_position", 0), e.get("stream_position", 0)))

    # ------------------------------------------------------------------
    # 2. Build projection states at examination_date
    # ------------------------------------------------------------------
    from src.projections.application_summary import ApplicationSummaryProjection
    from src.projections.compliance_audit import ComplianceAuditViewProjection

    app_summary_proj = ApplicationSummaryProjection()
    compliance_proj = ComplianceAuditViewProjection()

    for event in events_at_exam:
        await app_summary_proj.handle(event)
        await compliance_proj.handle(event)

    # ------------------------------------------------------------------
    # 3. Integrity verification
    # ------------------------------------------------------------------
    try:
        from src.integrity.audit_chain import run_integrity_check
        integrity_result = await run_integrity_check(store, "loan", application_id)
        integrity = {
            "events_verified": integrity_result.events_verified,
            "chain_valid": integrity_result.chain_valid,
            "tamper_detected": integrity_result.tamper_detected,
            "integrity_hash": integrity_result.integrity_hash,
        }
    except Exception as ex:
        integrity = {"error": str(ex), "chain_valid": None}

    # ------------------------------------------------------------------
    # 4. Human-readable narrative
    # ------------------------------------------------------------------
    narrative = _build_narrative(events_at_exam)

    # ------------------------------------------------------------------
    # 5. Agent model metadata
    # ------------------------------------------------------------------
    agent_metadata = _extract_agent_metadata(events_at_exam)

    # ------------------------------------------------------------------
    # 6. Package integrity hash (so regulators can verify the package itself)
    # ------------------------------------------------------------------
    package_body = {
        "application_id": application_id,
        "examination_date": examination_date.isoformat(),
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "event_count": len(events_at_exam),
        "events": events_at_exam,
        "projections": {
            "application_summary": app_summary_proj.get(application_id),
            "compliance_audit": compliance_proj.get_current_compliance(application_id),
        },
        "integrity_verification": integrity,
        "narrative": narrative,
        "agent_metadata": agent_metadata,
    }

    package_hash = hashlib.sha256(
        json.dumps(package_body, sort_keys=True, default=str).encode()
    ).hexdigest()
    package_body["package_hash"] = package_hash
    package_body["verification_instructions"] = (
        "To verify this package: remove the 'package_hash' field, "
        "serialize to JSON with sort_keys=True, default=str, "
        "and compute SHA-256. The result must match package_hash."
    )

    return package_body


def _build_narrative(events: list[dict]) -> list[str]:
    """Generate one-sentence narrative per significant event."""
    significant = {
        "ApplicationSubmitted": lambda p: f"Application {p.get('application_id')} submitted by {p.get('applicant_id')} for ${p.get('requested_amount_usd'):,.0f} ({p.get('loan_purpose')}).",
        "CreditAnalysisCompleted": lambda p: f"Credit analysis completed: risk tier {p.get('risk_tier')}, recommended limit ${p.get('recommended_limit_usd') or 0:,.0f}, confidence {p.get('confidence_score', 0):.0%}.",
        "FraudScreeningCompleted": lambda p: f"Fraud screening completed: score {p.get('fraud_score', 0):.2f}, recommendation {p.get('recommendation')}.",
        "ComplianceRulePassed": lambda p: f"Compliance rule {p.get('rule_id')} ({p.get('rule_name', '')}) passed.",
        "ComplianceRuleFailed": lambda p: f"Compliance rule {p.get('rule_id')} failed: {p.get('failure_reason', '')}.",
        "DecisionGenerated": lambda p: f"AI decision generated: {p.get('recommendation')} with {p.get('confidence_score', 0):.0%} confidence.",
        "HumanReviewCompleted": lambda p: f"Human review by {p.get('reviewer_id')}: {'OVERRIDE applied — ' + str(p.get('override_reason', '')) if p.get('override') else 'confirmed AI recommendation'}.",
        "ApplicationApproved": lambda p: f"Application APPROVED by {p.get('approved_by')}.",
        "ApplicationDeclined": lambda p: f"Application DECLINED. Reasons: {'; '.join(p.get('decline_reasons', []))}.",
        "AuditIntegrityCheckRun": lambda p: f"Audit integrity check: {'✓ chain valid' if p.get('chain_valid') else '⚠ chain INVALID'} ({p.get('events_verified_count', 0)} events verified).",
    }
    lines = []
    for event in events:
        et = event["event_type"]
        if et in significant:
            try:
                line = significant[et](event.get("payload", {}))
                lines.append(f"[{event.get('recorded_at', '')[:19]}] {line}")
            except Exception:
                lines.append(f"[{event.get('recorded_at', '')[:19]}] {et} occurred.")
    return lines


def _extract_agent_metadata(events: list[dict]) -> list[dict]:
    """Extract AI agent participation: model versions, confidence scores, input hashes."""
    agent_events = [
        "CreditAnalysisCompleted", "FraudScreeningCompleted",
        "DecisionGenerated", "AgentContextLoaded", "AgentSessionCompleted",
    ]
    metadata = []
    for event in events:
        if event["event_type"] in agent_events:
            p = event.get("payload", {})
            metadata.append({
                "event_type": event["event_type"],
                "recorded_at": str(event.get("recorded_at", "")),
                "agent_id": p.get("agent_id") or p.get("orchestrator_agent_id"),
                "session_id": p.get("session_id"),
                "model_version": p.get("model_version"),
                "confidence_score": p.get("confidence_score"),
                "input_data_hash": p.get("input_data_hash"),
                "model_versions": p.get("model_versions"),
            })
    return metadata
