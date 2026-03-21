from datetime import datetime, timezone

import pytest

from src.aggregates.agent_session import AgentSessionAggregate
from src.aggregates.loan_application import LoanApplicationAggregate
from src.commands.handlers import (
    handle_credit_analysis_completed,
    handle_submit_application,
)
from src.event_store import InMemoryEventStore
from src.models.events import DomainError, StreamMetadata


@pytest.mark.asyncio
async def test_submit_handler_uses_loaded_version_and_threads_causal_metadata():
    store = InMemoryEventStore()

    await handle_submit_application(
        {
            "application_id": "APEX-001",
            "applicant_id": "COMP-001",
            "requested_amount_usd": 120000.0,
            "loan_purpose": "working_capital",
            "submission_channel": "portal",
            "submitted_at": datetime.now(timezone.utc).isoformat(),
        },
        store,
        correlation_id="corr-123",
        causation_id="cause-123",
    )

    events = await store.load_stream("loan-APEX-001")
    assert len(events) == 1
    assert events[0]["metadata"]["correlation_id"] == "corr-123"
    assert events[0]["metadata"]["causation_id"] == "cause-123"


@pytest.mark.asyncio
async def test_credit_analysis_handler_loads_both_aggregates_and_enforces_guards():
    store = InMemoryEventStore()

    await handle_submit_application(
        {
            "application_id": "APEX-002",
            "applicant_id": "COMP-002",
            "requested_amount_usd": 250000.0,
            "loan_purpose": "expansion",
            "submission_channel": "portal",
            "submitted_at": datetime.now(timezone.utc).isoformat(),
        },
        store,
    )

    with pytest.raises(DomainError):
        await handle_credit_analysis_completed(
            {
                "application_id": "APEX-002",
                "agent_id": "agent-credit",
                "session_id": "s-1",
                "model_version": "v1",
                "confidence_score": 0.91,
                "risk_tier": "LOW",
                "recommended_limit_usd": 180000.0,
                "analysis_duration_ms": 200,
                "input_data_hash": "abc123",
            },
            store,
        )

    await store.append(
        "agent-agent-credit-s-1",
        [
            {
                "event_type": "AgentContextLoaded",
                "event_version": 1,
                "payload": {
                    "agent_id": "agent-credit",
                    "session_id": "s-1",
                    "context_source": "replay",
                    "event_replay_from_position": 0,
                    "context_token_count": 1000,
                    "model_version": "v1",
                },
            }
        ],
        expected_version=-1,
    )

    await handle_credit_analysis_completed(
        {
            "application_id": "APEX-002",
            "agent_id": "agent-credit",
            "session_id": "s-1",
            "model_version": "v1",
            "confidence_score": 0.91,
            "risk_tier": "LOW",
            "recommended_limit_usd": 180000.0,
            "analysis_duration_ms": 200,
            "input_data_hash": "abc123",
        },
        store,
    )

    loan_events = await store.load_stream("loan-APEX-002")
    assert loan_events[-1]["event_type"] == "CreditAnalysisCompleted"
    assert loan_events[-1]["stream_position"] == 1


@pytest.mark.asyncio
async def test_aggregate_replay_and_stream_metadata_shape():
    store = InMemoryEventStore()
    await store.append(
        "loan-APEX-003",
        [
            {
                "event_type": "ApplicationSubmitted",
                "event_version": 1,
                "payload": {
                    "application_id": "APEX-003",
                    "applicant_id": "COMP-003",
                    "requested_amount_usd": 100000.0,
                    "loan_purpose": "bridge",
                    "submission_channel": "api",
                    "submitted_at": datetime.now(timezone.utc).isoformat(),
                },
            },
            {
                "event_type": "CreditAnalysisCompleted",
                "event_version": 2,
                "payload": {
                    "application_id": "APEX-003",
                    "agent_id": "agent-credit",
                    "session_id": "s-3",
                    "model_version": "v2",
                    "confidence_score": 0.93,
                    "risk_tier": "LOW",
                    "recommended_limit_usd": 95000.0,
                    "analysis_duration_ms": 150,
                    "input_data_hash": "hash",
                },
            },
        ],
        expected_version=-1,
    )

    app = await LoanApplicationAggregate.load(store, "APEX-003")
    assert app.state == "AnalysisComplete"
    assert app.current_version == 1

    metadata = await store.get_stream_metadata("loan-APEX-003")
    assert isinstance(metadata, StreamMetadata)
    assert metadata.stream_id == "loan-APEX-003"

    session = AgentSessionAggregate(agent_id="agent-credit", session_id="s-3")
    with pytest.raises(DomainError):
        session.assert_context_loaded()
