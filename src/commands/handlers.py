from __future__ import annotations

from datetime import datetime, timezone

from src.aggregates.agent_session import AgentSessionAggregate
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

