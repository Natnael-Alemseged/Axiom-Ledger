from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any


@dataclass
class IntegrityCheckResult:
    entity_type: str
    entity_id: str
    events_verified: int
    chain_valid: bool
    tamper_detected: bool
    integrity_hash: str
    previous_hash: str | None
    checked_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    error_message: str | None = None


def _hash_event(event: dict) -> str:
    """Deterministic SHA-256 hash of a stored event's identity fields + payload."""
    canonical = json.dumps(
        {
            "event_id": str(event.get("event_id", "")),
            "stream_id": event.get("stream_id", ""),
            "stream_position": event.get("stream_position", 0),
            "event_type": event.get("event_type", ""),
            "payload": event.get("payload", {}),
        },
        sort_keys=True,
        default=str,
    )
    return hashlib.sha256(canonical.encode()).hexdigest()


def _compute_chain_hash(previous_hash: str | None, event_hashes: list[str]) -> str:
    """Compute integrity hash: SHA-256(previous_hash | event_hash_1 | ... | event_hash_n)."""
    data = (previous_hash or "GENESIS") + "|".join(event_hashes)
    return hashlib.sha256(data.encode()).hexdigest()


async def run_integrity_check(
    store,
    entity_type: str,
    entity_id: str,
) -> IntegrityCheckResult:
    """
    Cryptographic audit chain integrity check.

    Algorithm:
    1. Load all events from the primary stream.
    2. Find the last AuditIntegrityCheckRun event (if any).
    3. TAMPER DETECTION: Re-hash the events that were verified in the last check
       and compare the computed hash to the stored integrity_hash.
       A mismatch means stored events were altered after being verified.
    4. Hash the new events (since the last check) against the previous hash.
    5. Append a new AuditIntegrityCheckRun event to the audit stream.
    6. Return IntegrityCheckResult with chain_valid and tamper_detected flags.
    """
    primary_stream = f"loan-{entity_id}" if entity_type == "loan" else f"{entity_type}-{entity_id}"
    audit_stream = f"audit-{entity_type}-{entity_id}"

    # Load primary stream events
    primary_events = await store.load_stream(primary_stream)

    # Load audit stream to find the last integrity check record
    try:
        audit_events = await store.load_stream(audit_stream)
    except Exception:
        audit_events = []

    last_check = None
    for ae in reversed(audit_events):
        if ae["event_type"] == "AuditIntegrityCheckRun":
            last_check = ae
            break

    # ── Tamper detection ────────────────────────────────────────────────────
    chain_valid = True
    tamper_detected = False
    previous_hash: str | None = None
    events_to_verify: list[dict] = primary_events  # default: verify all events

    if last_check:
        stored_integrity_hash = last_check["payload"].get("integrity_hash")
        stored_previous_hash = last_check["payload"].get("previous_hash")
        # Cumulative count of events covered by the last check run
        last_verified_count: int = int(last_check["payload"].get("last_verified_count", 0))

        # Re-hash exactly the events that were present when the last check ran.
        # If anything in that segment was altered, the hash will differ.
        events_in_last_segment = primary_events[:last_verified_count]
        if events_in_last_segment:
            rehashed = [_hash_event(e) for e in events_in_last_segment]
            recomputed = _compute_chain_hash(stored_previous_hash, rehashed)
            if recomputed != stored_integrity_hash:
                # Hash mismatch: events before the last checkpoint were tampered with
                chain_valid = False
                tamper_detected = True

        # Carry the verified hash forward as the starting point for new events
        previous_hash = stored_integrity_hash
        events_to_verify = primary_events[last_verified_count:]

    # ── Hash new events ─────────────────────────────────────────────────────
    event_hashes = [_hash_event(e) for e in events_to_verify]
    new_hash = _compute_chain_hash(previous_hash, event_hashes)

    # Cumulative events now covered by this check (used for next re-verify)
    cumulative_count = (
        int(last_check["payload"].get("last_verified_count", 0)) if last_check else 0
    ) + len(events_to_verify)

    # ── Append AuditIntegrityCheckRun event ─────────────────────────────────
    try:
        audit_version = await store.stream_version(audit_stream)
        audit_event = {
            "event_type": "AuditIntegrityCheckRun",
            "event_version": 1,
            "payload": {
                "entity_type": entity_type,
                "entity_id": entity_id,
                "check_timestamp": datetime.now(timezone.utc).isoformat(),
                "events_verified_count": len(events_to_verify),
                "last_verified_count": cumulative_count,
                "integrity_hash": new_hash,
                "previous_hash": previous_hash,
                "chain_valid": chain_valid,
                "tamper_detected": tamper_detected,
            },
        }
        await store.append(
            stream_id=audit_stream,
            events=[audit_event],
            expected_version=audit_version,
        )
    except Exception as e:
        return IntegrityCheckResult(
            entity_type=entity_type,
            entity_id=entity_id,
            events_verified=len(events_to_verify),
            chain_valid=False,
            tamper_detected=tamper_detected,
            integrity_hash=new_hash,
            previous_hash=previous_hash,
            error_message=str(e),
        )

    return IntegrityCheckResult(
        entity_type=entity_type,
        entity_id=entity_id,
        events_verified=len(events_to_verify),
        chain_valid=chain_valid,
        tamper_detected=tamper_detected,
        integrity_hash=new_hash,
        previous_hash=previous_hash,
    )
