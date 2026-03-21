import asyncio

import pytest

from src.event_store import InMemoryEventStore
from src.models.events import OptimisticConcurrencyError


def _event(event_type: str, seq: int) -> dict:
    return {
        "event_type": event_type,
        "event_version": 1,
        "payload": {"seq": seq},
    }


@pytest.mark.asyncio
async def test_double_decision_exactly_one_wins():
    store = InMemoryEventStore()
    stream_id = "loan-APEX-DOUBLE-001"

    await store.append(stream_id, [_event("ApplicationSubmitted", 0)], expected_version=-1)
    await store.append(stream_id, [_event("CreditAnalysisRequested", 1)], expected_version=0)
    await store.append(stream_id, [_event("FraudScreeningCompleted", 2)], expected_version=1)
    await store.append(stream_id, [_event("ComplianceRulePassed", 3)], expected_version=2)

    async def try_append(agent_name: str):
        return await store.append(
            stream_id,
            [
                {
                    "event_type": "CreditAnalysisCompleted",
                    "event_version": 2,
                    "payload": {"agent": agent_name, "recommended_limit_usd": 100000},
                }
            ],
            expected_version=3,
        )

    before_stream = await store.load_stream(stream_id)
    before_len = len(before_stream)

    results = await asyncio.gather(
        try_append("A"),
        try_append("B"),
        return_exceptions=True,
    )

    success_positions = [r[0] for r in results if isinstance(r, list)]
    failures = [r for r in results if isinstance(r, OptimisticConcurrencyError)]

    stream = await store.load_stream(stream_id)
    winner_events = [
        e for e in stream if e["event_type"] == "CreditAnalysisCompleted" and e["stream_position"] == 4
    ]

    # Required concurrency guarantees under contention:
    # 1) exactly one additional event was appended
    # 2) the winner occupies the expected stream position
    assert len(stream) == before_len + 1
    assert success_positions == [4]
    assert len(failures) == 1
    assert len(winner_events) == 1
