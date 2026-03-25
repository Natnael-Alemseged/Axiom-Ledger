"""
tests/test_integrity.py — Cryptographic Audit Chain Integrity Tests
====================================================================
Tests that the tamper detection mechanism correctly identifies when
stored event payloads are modified after an integrity check baseline.
"""
from __future__ import annotations

import pytest

from src.event_store import InMemoryEventStore
from src.integrity.audit_chain import run_integrity_check


@pytest.fixture
def store():
    return InMemoryEventStore()


# ─── Test: Clean chain returns chain_valid=True ─────────────────────────────

@pytest.mark.asyncio
async def test_integrity_check_clean_chain(store):
    """A freshly appended stream with no tampering must pass integrity check."""
    await store.connect()
    await store.append(
        stream_id="loan-clean-001",
        events=[
            {
                "event_type": "ApplicationSubmitted",
                "event_version": 1,
                "payload": {"application_id": "clean-001", "requested_amount_usd": 100000},
            },
            {
                "event_type": "CreditAnalysisCompleted",
                "event_version": 2,
                "payload": {
                    "application_id": "clean-001",
                    "confidence_score": 0.87,
                    "recommended_limit_usd": 95000,
                },
            },
        ],
        expected_version=-1,
    )

    result = await run_integrity_check(store, entity_type="loan", entity_id="clean-001")

    assert result.chain_valid is True
    assert result.tamper_detected is False
    assert result.events_verified == 2
    assert result.integrity_hash  # non-empty hash


# ─── Test: Tamper detection — modify stored payload ─────────────────────────

@pytest.mark.asyncio
async def test_tamper_detection_after_payload_mutation(store):
    """
    TAMPER DETECTION TEST.

    Steps:
    1. Append events and run integrity check → establish baseline hash.
    2. Directly mutate a stored event's payload (simulates DB tampering).
    3. Run integrity check again.
    4. Assert tamper_detected=True and chain_valid=False.
    """
    await store.connect()

    # entity_id determines the stream: run_integrity_check looks for "loan-{entity_id}"
    entity_id = "tamper-001"
    stream_id = f"loan-{entity_id}"

    await store.append(
        stream_id=stream_id,
        events=[
            {
                "event_type": "ApplicationSubmitted",
                "event_version": 1,
                "payload": {
                    "application_id": entity_id,
                    "requested_amount_usd": 50000,
                    "applicant_id": "corp-99",
                },
            },
            {
                "event_type": "CreditAnalysisCompleted",
                "event_version": 2,
                "payload": {
                    "application_id": entity_id,
                    "confidence_score": 0.91,
                    "recommended_limit_usd": 48000,
                },
            },
        ],
        expected_version=-1,
    )

    # Step 1: Baseline integrity check — must pass cleanly
    baseline = await run_integrity_check(store, entity_type="loan", entity_id=entity_id)
    assert baseline.chain_valid is True
    assert baseline.tamper_detected is False

    # Step 2: Directly mutate the stored payload (simulates an attacker editing the DB)
    # InMemoryEventStore stores events in store._streams[stream_id] as a list of dicts
    stored_events = store._streams[stream_id]
    # Tamper: change the requested_amount_usd retroactively
    stored_events[0]["payload"]["requested_amount_usd"] = 9_999_999

    # Step 3: Re-run integrity check — must detect the tampering
    result = await run_integrity_check(store, entity_type="loan", entity_id=entity_id)

    # Step 4: Tamper detected
    assert result.tamper_detected is True, (
        "Integrity check should have detected that the stored payload was mutated, "
        f"but tamper_detected={result.tamper_detected}"
    )
    assert result.chain_valid is False, (
        "chain_valid should be False when tamper is detected, "
        f"but chain_valid={result.chain_valid}"
    )


# ─── Test: Second clean check after first (incremental hashing) ─────────────

@pytest.mark.asyncio
async def test_incremental_integrity_check_no_tampering(store):
    """
    Running two consecutive integrity checks on the same clean stream
    must both return chain_valid=True and tamper_detected=False.
    The second check hashes new events only (incremental).
    """
    await store.connect()

    entity_id = "incr-001"
    stream_id = f"loan-{entity_id}"
    await store.append(
        stream_id=stream_id,
        events=[
            {
                "event_type": "ApplicationSubmitted",
                "event_version": 1,
                "payload": {"application_id": entity_id, "requested_amount_usd": 75000},
            },
        ],
        expected_version=-1,
    )

    first = await run_integrity_check(store, entity_type="loan", entity_id=entity_id)
    assert first.chain_valid is True
    assert first.tamper_detected is False

    # Append more events between checks
    await store.append(
        stream_id=stream_id,
        events=[
            {
                "event_type": "CreditAnalysisCompleted",
                "event_version": 2,
                "payload": {"application_id": entity_id, "confidence_score": 0.85, "recommended_limit_usd": 70000},
            },
        ],
        expected_version=0,
    )

    second = await run_integrity_check(store, entity_type="loan", entity_id=entity_id)
    assert second.chain_valid is True
    assert second.tamper_detected is False
    # Only the new event is freshly verified in the second run
    assert second.events_verified == 1
