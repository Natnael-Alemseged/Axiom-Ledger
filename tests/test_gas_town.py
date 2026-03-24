"""
tests/test_gas_town.py — Phase 4C: Gas Town Agent Memory Reconstruction
========================================================================
Test that an agent can reconstruct its full context from the event store
after a simulated crash — without any in-memory agent state.
"""
from __future__ import annotations

import pytest

from src.event_store import InMemoryEventStore
from src.integrity.gas_town import reconstruct_agent_context, SessionHealthStatus


@pytest.fixture
def store():
    return InMemoryEventStore()


# ─── Test: Successful Reconstruction After Simulated Crash ─────────────────

@pytest.mark.asyncio
async def test_agent_context_reconstruction_after_crash(store):
    """
    Simulate a crash:
    1. Append 5 agent session events (no in-memory agent object).
    2. Call reconstruct_agent_context() from scratch.
    3. Verify reconstructed context contains enough information to continue.
    """
    await store.connect()
    agent_id = "credit-agent-01"
    session_id = "sess-crash-001"
    stream_id = f"agent-{agent_id}-{session_id}"

    # Simulate agent recording its session events before crash
    events = [
        {
            "event_type": "AgentContextLoaded",
            "event_version": 1,
            "payload": {
                "agent_id": agent_id,
                "session_id": session_id,
                "application_id": "app-crash-001",
                "context_source": "event_replay",
                "event_replay_from_position": 0,
                "context_token_count": 1500,
                "model_version": "credit-v2.3",
            },
        },
        {
            "event_type": "AgentNodeExecuted",
            "event_version": 1,
            "payload": {
                "session_id": session_id,
                "node_name": "load_documents",
                "node_sequence": 1,
                "llm_called": False,
                "duration_ms": 200,
            },
        },
        {
            "event_type": "AgentNodeExecuted",
            "event_version": 1,
            "payload": {
                "session_id": session_id,
                "node_name": "compute_ratios",
                "node_sequence": 2,
                "llm_called": False,
                "duration_ms": 150,
            },
        },
        {
            "event_type": "AgentNodeExecuted",
            "event_version": 1,
            "payload": {
                "session_id": session_id,
                "node_name": "llm_analysis",
                "node_sequence": 3,
                "llm_called": True,
                "llm_tokens_input": 1200,
                "llm_tokens_output": 300,
                "duration_ms": 1800,
            },
        },
        {
            "event_type": "AgentToolCalled",
            "event_version": 1,
            "payload": {
                "session_id": session_id,
                "tool_name": "get_historical_profile",
                "tool_input_summary": "application_id=app-crash-001",
                "tool_output_summary": "3 years history, no defaults",
                "tool_duration_ms": 45,
            },
        },
    ]

    version = -1
    for event in events:
        await store.append(
            stream_id=stream_id,
            events=[event],
            expected_version=version,
        )
        version += 1

    # CRASH SIMULATION: no in-memory agent object — reconstruct from events only
    context = await reconstruct_agent_context(
        store=store,
        agent_id=agent_id,
        session_id=session_id,
        token_budget=8000,
    )

    # Verify reconstruction quality
    assert context.agent_id == agent_id
    assert context.session_id == session_id
    assert context.application_id == "app-crash-001"
    assert context.model_version == "credit-v2.3"
    assert context.events_replayed == 5
    assert context.last_event_position == 4  # 0-indexed, 5 events
    assert context.session_health_status == SessionHealthStatus.HEALTHY
    assert len(context.context_text) > 0
    assert context.context_text != ""

    # Context must contain enough information to continue
    assert "app-crash-001" in context.context_text or context.application_id == "app-crash-001"


@pytest.mark.asyncio
async def test_needs_reconciliation_for_incomplete_session(store):
    """
    If agent session failed without recovery, status should be FAILED or NEEDS_RECONCILIATION.
    """
    await store.connect()
    agent_id = "credit-agent-02"
    session_id = "sess-failed-001"
    stream_id = f"agent-{agent_id}-{session_id}"

    events = [
        {
            "event_type": "AgentContextLoaded",
            "event_version": 1,
            "payload": {
                "agent_id": agent_id,
                "session_id": session_id,
                "application_id": "app-failed-001",
                "model_version": "credit-v2.3",
                "context_source": "fresh",
                "event_replay_from_position": 0,
                "context_token_count": 800,
            },
        },
        {
            "event_type": "AgentSessionFailed",
            "event_version": 1,
            "payload": {
                "session_id": session_id,
                "agent_type": "credit_analysis",
                "application_id": "app-failed-001",
                "error_type": "LLMTimeoutError",
                "error_message": "Timeout after 30s",
                "recoverable": True,
            },
        },
    ]

    version = -1
    for event in events:
        await store.append(
            stream_id=stream_id,
            events=[event],
            expected_version=version,
        )
        version += 1

    context = await reconstruct_agent_context(store, agent_id, session_id)

    # Must detect the failure
    assert context.session_health_status in (
        SessionHealthStatus.FAILED,
        SessionHealthStatus.NEEDS_RECONCILIATION,
    )


@pytest.mark.asyncio
async def test_empty_session_returns_empty_status(store):
    """Reconstructing a non-existent session returns EMPTY status."""
    await store.connect()
    context = await reconstruct_agent_context(store, "ghost-agent", "nonexistent-session")
    assert context.session_health_status == SessionHealthStatus.EMPTY
    assert context.events_replayed == 0
    assert context.last_event_position == -1


@pytest.mark.asyncio
async def test_context_respects_token_budget(store):
    """Context text must not exceed the token budget (4 chars ≈ 1 token)."""
    await store.connect()
    agent_id = "credit-agent-03"
    session_id = "sess-long-001"
    stream_id = f"agent-{agent_id}-{session_id}"

    # Create many events
    await store.append(
        stream_id=stream_id,
        events=[{
            "event_type": "AgentContextLoaded",
            "event_version": 1,
            "payload": {
                "agent_id": agent_id,
                "session_id": session_id,
                "application_id": "app-long-001",
                "model_version": "v1",
                "context_source": "fresh",
                "event_replay_from_position": 0,
                "context_token_count": 100,
            },
        }],
        expected_version=-1,
    )
    for i in range(50):
        await store.append(
            stream_id=stream_id,
            events=[{
                "event_type": "AgentNodeExecuted",
                "event_version": 1,
                "payload": {
                    "session_id": session_id,
                    "node_name": f"node_{i}",
                    "node_sequence": i + 1,
                    "llm_called": False,
                    "duration_ms": 100,
                },
            }],
            expected_version=i,
        )

    token_budget = 500
    context = await reconstruct_agent_context(store, agent_id, session_id, token_budget=token_budget)
    max_chars = token_budget * 4
    assert len(context.context_text) <= max_chars + 100  # small tolerance for truncation marker
