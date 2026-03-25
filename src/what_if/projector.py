"""
src/what_if/projector.py — Counterfactual Event Projection
===========================================================
Phase 6 (Bonus): What-if projection for regulatory scenario analysis.

Enables: "What would the final decision have been if we had used a different risk model?"

Algorithm:
  1. Load all events for the application stream up to the branch point.
  2. At the branch point, inject counterfactual_events instead of real events.
  3. Continue replaying real events that are causally INDEPENDENT of the branch.
  4. Skip real events that are causally DEPENDENT on the branched events.
  5. Apply all events (pre-branch real + counterfactual + post-branch independent)
     to each projection.
  6. Return {real_outcome, counterfactual_outcome, divergence_events[]}

NEVER writes counterfactual events to the real store.
Causal dependency: an event is dependent if its causation_id traces
back to an event at or after the branch point.
"""
from __future__ import annotations

import copy
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any


@dataclass
class WhatIfResult:
    application_id: str
    branch_at_event_type: str
    real_outcome: dict
    counterfactual_outcome: dict
    divergence_events: list[dict]
    counterfactual_events_injected: int
    real_events_replayed: int
    causal_dependents_skipped: int
    ran_at: datetime = field(default_factory=datetime.utcnow)


async def run_what_if(
    store,
    application_id: str,
    branch_at_event_type: str,
    counterfactual_events: list[dict],
    projections: list,
) -> WhatIfResult:
    """
    Run a counterfactual scenario.

    Parameters
    ----------
    store:
        The (real) event store — used READ-ONLY. NEVER written to.
    application_id:
        The loan application to run the scenario against.
    branch_at_event_type:
        The event type at which the counterfactual diverges
        (e.g. "CreditAnalysisCompleted").
    counterfactual_events:
        Replacement events to inject at the branch point instead of real ones.
    projections:
        Projection instances to evaluate under both scenarios.
        Must implement async handle(event) -> None and get / get_current_compliance etc.
    """
    primary_stream = f"loan-{application_id}"

    # 1. Load ALL events from the real store
    real_events = await store.load_stream(primary_stream)

    # Also load all related streams for audit
    for stream_prefix in ("credit", "fraud", "compliance"):
        try:
            extra = await store.load_stream(f"{stream_prefix}-{application_id}")
            real_events.extend(extra)
        except Exception:
            pass

    # Sort by global_position for correct causal ordering
    real_events.sort(key=lambda e: (e.get("global_position", 0), e.get("stream_position", 0)))

    # 2. Find branch point index
    branch_index = None
    for i, event in enumerate(real_events):
        if event["event_type"] == branch_at_event_type:
            branch_index = i
            break

    if branch_index is None:
        # No branch point found — run counterfactual on empty history
        branch_index = len(real_events)

    pre_branch = real_events[:branch_index]
    post_branch = real_events[branch_index + 1:]  # skip the branched event itself

    # 3. Identify causal dependents after branch point
    # An event is causally dependent if its causation_id traces to the branched event
    branched_event = real_events[branch_index] if branch_index < len(real_events) else None
    branched_ids: set[str] = set()
    if branched_event:
        branched_ids.add(str(branched_event.get("event_id", "")))

    causal_dependents_skipped = 0
    independent_post_branch: list[dict] = []
    for event in post_branch:
        causation_id = str(event.get("metadata", {}).get("causation_id") or "")
        if causation_id and causation_id in branched_ids:
            causal_dependents_skipped += 1
            branched_ids.add(str(event.get("event_id", "")))
        else:
            independent_post_branch.append(event)

    # 4. Build real scenario event list
    real_scenario = pre_branch + (
        [branched_event] if branched_event else []
    ) + independent_post_branch

    # 5. Build counterfactual scenario event list
    counterfactual_scenario = pre_branch + counterfactual_events + independent_post_branch

    # 6. Apply real scenario to fresh copies of projections
    real_outcome = await _apply_scenario(real_scenario, projections, application_id)

    # 7. Apply counterfactual scenario to fresh copies of projections
    counterfactual_outcome = await _apply_scenario(
        counterfactual_scenario, projections, application_id
    )

    # 8. Find divergence events (events in counterfactual but not real, or with different content)
    real_types = [e["event_type"] for e in real_scenario]
    cf_types = [e["event_type"] for e in counterfactual_scenario]
    divergence_events = [
        e for e in counterfactual_events
        if e["event_type"] not in real_types
    ]

    return WhatIfResult(
        application_id=application_id,
        branch_at_event_type=branch_at_event_type,
        real_outcome=real_outcome,
        counterfactual_outcome=counterfactual_outcome,
        divergence_events=divergence_events,
        counterfactual_events_injected=len(counterfactual_events),
        real_events_replayed=len(real_scenario),
        causal_dependents_skipped=causal_dependents_skipped,
    )


async def _apply_scenario(events: list[dict], projections: list, application_id: str) -> dict:
    """
    Apply a sequence of events to fresh copies of the projections.
    Returns a summary outcome dict.
    """
    import importlib
    from src.projections.application_summary import ApplicationSummaryProjection
    from src.projections.compliance_audit import ComplianceAuditViewProjection

    # Use fresh projection instances to avoid contaminating the real state
    app_summary = ApplicationSummaryProjection()
    compliance_audit = ComplianceAuditViewProjection()

    for event in events:
        await app_summary.handle(event)
        await compliance_audit.handle(event)

    return {
        "application_summary": app_summary.get(application_id),
        "compliance_state": compliance_audit.get_current_compliance(application_id),
        "events_applied": len(events),
    }
