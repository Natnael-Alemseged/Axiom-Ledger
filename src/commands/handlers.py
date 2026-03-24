from __future__ import annotations

from datetime import datetime, timezone

from src.aggregates.agent_session import AgentSessionAggregate
from src.aggregates.compliance_record import ComplianceRecordAggregate
from src.aggregates.loan_application import LoanApplicationAggregate


async def handle_submit_application(
    cmd: dict,
    store,
    correlation_id: str | None = None,
    causation_id: str | None = None,
) -> None:
    application_id = cmd["application_id"]

    app = await LoanApplicationAggregate.load(store, application_id)
    app.assert_can_submit()

    event = {
        "event_type": "ApplicationSubmitted",
        "event_version": 1,
        "payload": {
            "application_id": application_id,
            "applicant_id": cmd["applicant_id"],
            "requested_amount_usd": cmd["requested_amount_usd"],
            "loan_purpose": cmd["loan_purpose"],
            "submission_channel": cmd["submission_channel"],
            "submitted_at": cmd.get("submitted_at") or datetime.now(timezone.utc).isoformat(),
        },
    }

    await store.append(
        stream_id=f"loan-{application_id}",
        events=[event],
        expected_version=app.current_version,
        correlation_id=correlation_id,
        causation_id=causation_id,
    )


async def handle_fraud_screening_completed(
    cmd: dict,
    store,
    correlation_id: str | None = None,
    causation_id: str | None = None,
) -> None:
    application_id = cmd["application_id"]
    agent_id = cmd["agent_id"]
    session_id = cmd["session_id"]

    agent = await AgentSessionAggregate.load(store, agent_id, session_id)
    agent.assert_context_loaded()

    app = await LoanApplicationAggregate.load(store, application_id)

    event = {
        "event_type": "FraudScreeningCompleted",
        "event_version": 1,
        "payload": {
            "application_id": application_id,
            "agent_id": agent_id,
            "fraud_score": cmd["fraud_score"],
            "anomaly_flags": cmd.get("anomaly_flags", []),
            "screening_model_version": cmd["screening_model_version"],
            "input_data_hash": cmd["input_data_hash"],
        },
    }

    await store.append(
        stream_id=f"loan-{application_id}",
        events=[event],
        expected_version=app.current_version,
        correlation_id=correlation_id,
        causation_id=causation_id,
    )


async def handle_compliance_check(
    cmd: dict,
    store,
    correlation_id: str | None = None,
    causation_id: str | None = None,
) -> None:
    application_id = cmd["application_id"]

    compliance = await ComplianceRecordAggregate.load(store, application_id)

    if cmd["passed"]:
        event = {
            "event_type": "ComplianceRulePassed",
            "event_version": 1,
            "payload": {
                "application_id": application_id,
                "rule_id": cmd["rule_id"],
                "rule_version": cmd["rule_version"],
                "evaluation_timestamp": cmd.get("evaluation_timestamp")
                    or datetime.now(timezone.utc).isoformat(),
                "evidence_hash": cmd["evidence_hash"],
            },
        }
    else:
        event = {
            "event_type": "ComplianceRuleFailed",
            "event_version": 1,
            "payload": {
                "application_id": application_id,
                "rule_id": cmd["rule_id"],
                "rule_version": cmd["rule_version"],
                "failure_reason": cmd["failure_reason"],
                "remediation_required": cmd.get("remediation_required", False),
            },
        }

    await store.append(
        stream_id=f"compliance-{application_id}",
        events=[event],
        expected_version=compliance.current_version,
        correlation_id=correlation_id,
        causation_id=causation_id,
    )


async def handle_generate_decision(
    cmd: dict,
    store,
    correlation_id: str | None = None,
    causation_id: str | None = None,
) -> None:
    application_id = cmd["application_id"]

    app = await LoanApplicationAggregate.load(store, application_id)

    confidence_score = cmd["confidence_score"]
    recommendation = cmd["recommendation"]
    if confidence_score < 0.6:
        recommendation = "REFER"

    event = {
        "event_type": "DecisionGenerated",
        "event_version": 2,
        "payload": {
            "application_id": application_id,
            "orchestrator_agent_id": cmd["orchestrator_agent_id"],
            "recommendation": recommendation,
            "confidence_score": confidence_score,
            "contributing_agent_sessions": cmd.get("contributing_agent_sessions", []),
            "decision_basis_summary": cmd["decision_basis_summary"],
            "model_versions": cmd.get("model_versions", {}),
        },
    }

    await store.append(
        stream_id=f"loan-{application_id}",
        events=[event],
        expected_version=app.current_version,
        correlation_id=correlation_id,
        causation_id=causation_id,
    )


async def handle_human_review_completed(
    cmd: dict,
    store,
    correlation_id: str | None = None,
    causation_id: str | None = None,
) -> None:
    application_id = cmd["application_id"]

    app = await LoanApplicationAggregate.load(store, application_id)

    final_decision = cmd["final_decision"]

    review_event = {
        "event_type": "HumanReviewCompleted",
        "event_version": 1,
        "payload": {
            "application_id": application_id,
            "reviewer_id": cmd["reviewer_id"],
            "override": cmd.get("override", False),
            "final_decision": final_decision,
            "override_reason": cmd.get("override_reason"),
        },
    }

    if final_decision == "APPROVE":
        outcome_event = {
            "event_type": "ApplicationApproved",
            "event_version": 1,
            "payload": {
                "application_id": application_id,
                "approved_amount_usd": cmd["approved_amount_usd"],
                "interest_rate": cmd["interest_rate"],
                "conditions": cmd.get("conditions", []),
                "approved_by": cmd["reviewer_id"],
                "effective_date": cmd.get("effective_date")
                    or datetime.now(timezone.utc).isoformat(),
            },
        }
    else:
        outcome_event = {
            "event_type": "ApplicationDeclined",
            "event_version": 1,
            "payload": {
                "application_id": application_id,
                "decline_reasons": cmd.get("decline_reasons", []),
                "declined_by": cmd["reviewer_id"],
                "adverse_action_notice_required": cmd.get("adverse_action_notice_required", True),
            },
        }

    await store.append(
        stream_id=f"loan-{application_id}",
        events=[review_event, outcome_event],
        expected_version=app.current_version,
        correlation_id=correlation_id,
        causation_id=causation_id,
    )


async def handle_start_agent_session(
    cmd: dict,
    store,
    correlation_id: str | None = None,
    causation_id: str | None = None,
) -> None:
    agent_id = cmd["agent_id"]
    session_id = cmd["session_id"]

    agent = await AgentSessionAggregate.load(store, agent_id, session_id)

    event = {
        "event_type": "AgentContextLoaded",
        "event_version": 1,
        "payload": {
            "agent_id": agent_id,
            "session_id": session_id,
            "context_source": cmd["context_source"],
            "event_replay_from_position": cmd.get("event_replay_from_position", 0),
            "context_token_count": cmd["context_token_count"],
            "model_version": cmd["model_version"],
        },
    }

    await store.append(
        stream_id=f"agent-{agent_id}-{session_id}",
        events=[event],
        expected_version=agent.current_version,
        correlation_id=correlation_id,
        causation_id=causation_id,
    )


async def handle_credit_analysis_completed(
    cmd: dict,
    store,
    correlation_id: str | None = None,
    causation_id: str | None = None,
) -> None:
    application_id = cmd["application_id"]
    agent_id = cmd["agent_id"]
    session_id = cmd["session_id"]

    app = await LoanApplicationAggregate.load(store, application_id)
    agent = await AgentSessionAggregate.load(store, agent_id, session_id)

    app.assert_awaiting_credit_analysis()
    agent.assert_context_loaded()
    agent.assert_model_version_current(cmd["model_version"])

    event = {
        "event_type": "CreditAnalysisCompleted",
        "event_version": 2,
        "payload": {
            "application_id": application_id,
            "agent_id": agent_id,
            "session_id": session_id,
            "model_version": cmd["model_version"],
            "confidence_score": cmd["confidence_score"],
            "risk_tier": cmd["risk_tier"],
            "recommended_limit_usd": cmd["recommended_limit_usd"],
            "analysis_duration_ms": cmd["analysis_duration_ms"],
            "input_data_hash": cmd["input_data_hash"],
        },
    }

    await store.append(
        stream_id=f"loan-{application_id}",
        events=[event],
        expected_version=app.current_version,
        correlation_id=correlation_id,
        causation_id=causation_id,
    )

