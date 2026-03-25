"""
tests/test_upcasting.py — Phase 4A: Upcaster Immutability Test
================================================================
MANDATORY TEST (per challenge spec):

1. Store a v1 event directly in the in-memory store.
2. Load the same event through EventStore.load_stream() — verify it is upcasted to v2.
3. Inspect the raw stored payload — verify it is UNCHANGED.

Any system where upcasting touches stored events has broken the core guarantee of event sourcing.
"""
from __future__ import annotations

import pytest
from datetime import datetime, timezone

from src.event_store import InMemoryEventStore
from src.upcasting.registry import UpcasterRegistry as StandaloneRegistry
from src.upcasting.upcasters import registry as default_registry


# ─── Fixtures ──────────────────────────────────────────────────────────────

@pytest.fixture
def store_with_upcasters():
    """InMemoryEventStore configured with the default upcaster registry."""
    return InMemoryEventStore(upcaster_registry=default_registry)


@pytest.fixture
def store_raw():
    """InMemoryEventStore with NO upcasters — for reading raw stored data."""
    return InMemoryEventStore(upcaster_registry=None)


# ─── Test: CreditAnalysisCompleted v1 → v2 Immutability ────────────────────

@pytest.mark.asyncio
async def test_credit_analysis_upcasting_immutability():
    """
    THE IMMUTABILITY TEST.

    Steps:
    1. Append a v1 CreditAnalysisCompleted event to stream.
    2. Load via upcasting store → confirm event_version == 2.
    3. Read raw stored event → confirm event_version == 1 (UNCHANGED).
    """
    # Step 1: Append v1 event
    store_up = InMemoryEventStore(upcaster_registry=default_registry)
    await store_up.connect()

    v1_event = {
        "event_type": "CreditAnalysisCompleted",
        "event_version": 1,
        "payload": {
            "application_id": "app-001",
            "agent_id": "agent-credit-01",
            "session_id": "sess-001",
            "confidence_score": 0.82,
            "risk_tier": "LOW",
            "recommended_limit_usd": 500000,
            "analysis_duration_ms": 1200,
            "input_data_hash": "abc123",
        },
    }
    await store_up.append(
        stream_id="loan-app-001",
        events=[v1_event],
        expected_version=-1,
    )

    # Step 2: Load via upcasting store — must be v2
    upcasted_events = await store_up.load_stream("loan-app-001")
    assert len(upcasted_events) == 1
    upcasted = upcasted_events[0]
    assert upcasted["event_version"] == 2, (
        f"Expected event_version=2 after upcasting, got {upcasted['event_version']}"
    )
    assert "regulatory_basis" in upcasted["payload"], (
        "v2 payload must have regulatory_basis field"
    )

    # Step 3: Read RAW stored data — must still be v1
    # Access the internal _streams directly (simulates direct DB read without upcasters)
    raw_stored = store_up._streams["loan-app-001"][0]
    assert raw_stored["event_version"] == 1, (
        f"RAW STORED EVENT WAS MUTATED! Expected version=1 but got {raw_stored['event_version']}. "
        "Upcasting must NEVER modify stored events."
    )
    assert "regulatory_basis" not in raw_stored["payload"], (
        "RAW STORED PAYLOAD WAS MUTATED! regulatory_basis should not exist in the raw stored event."
    )


@pytest.mark.asyncio
async def test_decision_generated_upcasting_immutability():
    """
    Immutability test for DecisionGenerated v1 → v2.
    """
    store_up = InMemoryEventStore(upcaster_registry=default_registry)
    await store_up.connect()

    v1_event = {
        "event_type": "DecisionGenerated",
        "event_version": 1,
        "payload": {
            "application_id": "app-002",
            "orchestrator_agent_id": "orch-01",
            "recommendation": "APPROVE",
            "confidence_score": 0.88,
            "contributing_agent_sessions": ["sess-001", "sess-002"],
            "decision_basis_summary": "Strong financials, low risk.",
        },
    }
    await store_up.append(
        stream_id="loan-app-002",
        events=[v1_event],
        expected_version=-1,
    )

    # Upcasted view: must be v2
    upcasted_events = await store_up.load_stream("loan-app-002")
    upcasted = upcasted_events[0]
    assert upcasted["event_version"] == 2
    assert "model_versions" in upcasted["payload"]

    # Raw stored: must still be v1
    raw = store_up._streams["loan-app-002"][0]
    assert raw["event_version"] == 1, (
        "DecisionGenerated raw stored event was mutated by upcasting!"
    )
    assert "model_versions" not in raw["payload"], (
        "model_versions was written into the raw stored payload — immutability violated."
    )


def test_credit_upcast_inference_uses_recorded_at():
    """model_version and regulatory_basis use event.recorded_at when payload omits them."""
    event = {
        "event_type": "CreditAnalysisCompleted",
        "event_version": 1,
        "recorded_at": datetime(2020, 3, 1, 12, 0, 0, tzinfo=timezone.utc),
        "payload": {
            "application_id": "app-inf",
            "agent_id": "ag-1",
            "session_id": "s-1",
            "risk_tier": "LOW",
            "recommended_limit_usd": 100000,
            "analysis_duration_ms": 100,
            "input_data_hash": "x",
        },
    }
    out = default_registry.upcast(dict(event))
    assert out["event_version"] == 2
    assert "credit-legacy-y2020" in out["payload"]["model_version"]
    assert out["payload"]["regulatory_basis"]
    assert out["payload"]["regulatory_basis"][0].get("package") == "FIN_BASE"


def test_decision_upcast_reconstructs_model_versions_from_sessions():
    event = {
        "event_type": "DecisionGenerated",
        "event_version": 1,
        "recorded_at": datetime(2025, 7, 1, tzinfo=timezone.utc),
        "payload": {
            "application_id": "app-d",
            "orchestrator_agent_id": "orch",
            "recommendation": "APPROVE",
            "confidence_score": 0.9,
            "contributing_agent_sessions": ["sess-001", "sess-002"],
            "decision_basis_summary": "ok",
        },
    }
    out = default_registry.upcast(dict(event))
    assert out["event_version"] == 2
    mv = out["payload"]["model_versions"]
    assert set(mv.keys()) == {"sess-001", "sess-002"}
    assert all(v.startswith("inferred-from-session@y2025:") for v in mv.values())


# ─── Test: UpcasterRegistry.upcast() returns new dict ─────────────────────

def test_upcaster_registry_returns_new_dict():
    """upcast() must return a NEW dict, never mutate the input."""
    reg = StandaloneRegistry()

    @reg.register("TestEvent", from_version=1)
    def upcast_v1(payload: dict) -> dict:
        return {**payload, "new_field": "added"}

    original = {
        "event_type": "TestEvent",
        "event_version": 1,
        "payload": {"original_field": "original"},
    }
    original_payload_id = id(original["payload"])

    result = reg.upcast(dict(original))

    # Result must be different object
    assert result is not original
    assert result["event_version"] == 2
    assert "new_field" in result["payload"]

    # Original must be unchanged
    assert original["event_version"] == 1
    assert "new_field" not in original["payload"]


def test_upcaster_chain_multiple_versions():
    """Registry must apply upcasters in version-order chains (v1→v2→v3)."""
    reg = StandaloneRegistry()

    @reg.register("ChainEvent", from_version=1)
    def v1_to_v2(payload: dict) -> dict:
        return {**payload, "step": "v2"}

    @reg.register("ChainEvent", from_version=2)
    def v2_to_v3(payload: dict) -> dict:
        return {**payload, "step": "v3"}

    event = {
        "event_type": "ChainEvent",
        "event_version": 1,
        "payload": {"step": "v1"},
    }
    result = reg.upcast(dict(event))
    assert result["event_version"] == 3
    assert result["payload"]["step"] == "v3"


def test_upcaster_no_op_for_current_version():
    """If event is already at current version, upcast() returns it unchanged."""
    reg = StandaloneRegistry()

    @reg.register("MyEvent", from_version=1)
    def v1_to_v2(payload: dict) -> dict:
        return {**payload, "added": True}

    event = {
        "event_type": "MyEvent",
        "event_version": 2,
        "payload": {"field": "value"},
    }
    result = reg.upcast(dict(event))
    assert result["event_version"] == 2
    assert "added" not in result["payload"]
