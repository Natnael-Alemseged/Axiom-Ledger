from __future__ import annotations
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any


class SessionHealthStatus(str, Enum):
    HEALTHY = "HEALTHY"
    NEEDS_RECONCILIATION = "NEEDS_RECONCILIATION"
    FAILED = "FAILED"
    EMPTY = "EMPTY"


@dataclass
class AgentContext:
    agent_id: str
    session_id: str
    context_text: str
    last_event_position: int
    pending_work: list[str]
    session_health_status: SessionHealthStatus
    last_completed_action: str | None = None
    application_id: str | None = None
    model_version: str | None = None
    events_replayed: int = 0
    reconstructed_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))


# Decision event types that indicate completed work
_DECISION_EVENT_TYPES = {
    "CreditAnalysisCompleted",
    "FraudScreeningCompleted",
    "ComplianceCheckCompleted",
    "DecisionGenerated",
    "AgentSessionCompleted",
}

# Partial/error events that require reconciliation
_PARTIAL_EVENT_TYPES = {
    "AgentSessionFailed",
    "AgentInputValidationFailed",
    "CreditAnalysisDeferred",
}


async def reconstruct_agent_context(
    store,
    agent_id: str,
    session_id: str,
    token_budget: int = 8000,
) -> AgentContext:
    """
    Reconstruct agent context from event stream after a crash.

    1. Load full AgentSession stream for agent_id + session_id.
    2. Identify: last completed action, pending work items, current application state.
    3. Summarise old events into prose (token-efficient).
    4. Preserve verbatim: last 3 events, any PENDING or ERROR state events.
    5. Return AgentContext with context_text, last_event_position, pending_work[], session_health_status.

    CRITICAL: if the agent's last event was a partial decision (no corresponding completion),
    flag as NEEDS_RECONCILIATION.
    """
    stream_id = f"agent-{agent_id}-{session_id}"

    try:
        events = await store.load_stream(stream_id)
    except Exception:
        events = []

    if not events:
        return AgentContext(
            agent_id=agent_id,
            session_id=session_id,
            context_text=f"No events found for session {session_id}. Fresh start.",
            last_event_position=-1,
            pending_work=[],
            session_health_status=SessionHealthStatus.EMPTY,
        )

    last_position = int(events[-1]["stream_position"])
    application_id = None
    model_version = None
    last_completed_action = None
    pending_work: list[str] = []
    health_status = SessionHealthStatus.HEALTHY

    # Track tool calls awaiting results (tool called but no completion seen yet)
    _pending_tool_calls: list[str] = []

    # Extract key state
    for event in events:
        et = event["event_type"]
        p = event.get("payload", {})

        if et == "AgentContextLoaded" or et == "AgentSessionStarted":
            application_id = p.get("application_id")
            model_version = p.get("model_version")
        elif et in _DECISION_EVENT_TYPES:
            last_completed_action = et
        elif et in _PARTIAL_EVENT_TYPES:
            health_status = SessionHealthStatus.NEEDS_RECONCILIATION
            pending_work.append(f"Resolve {et}: {p.get('error_type', 'unknown error')}")
        elif et == "AgentToolCalled":
            tool_name = p.get("tool_name", "unknown_tool")
            _pending_tool_calls.append(tool_name)
        elif et == "AgentToolCompleted":
            tool_name = p.get("tool_name", "")
            if tool_name in _pending_tool_calls:
                _pending_tool_calls.remove(tool_name)

    # Tool calls with no corresponding completion = pending work items
    for tool_name in _pending_tool_calls:
        pending_work.append(f"Awaiting result for tool call: {tool_name}")

    # Check for partial decisions (started but no completion)
    event_types_seen = {e["event_type"] for e in events}
    if "CreditAnalysisRequested" in event_types_seen and "CreditAnalysisCompleted" not in event_types_seen:
        health_status = SessionHealthStatus.NEEDS_RECONCILIATION
        pending_work.append("CreditAnalysis was requested but never completed — requires reconciliation")

    if "AgentSessionFailed" in event_types_seen and "AgentSessionRecovered" not in event_types_seen:
        health_status = SessionHealthStatus.FAILED

    import json

    # Build context text with token budget awareness
    last_3 = events[-3:]
    older_events = events[:-3] if len(events) > 3 else []

    # Event types that must be preserved verbatim even when older than last 3
    _VERBATIM_PRESERVE_TYPES = _PARTIAL_EVENT_TYPES | {"AgentToolCalled"}

    # Summarise older events into prose; preserve PENDING/ERROR events verbatim
    summary_lines = []
    verbatim_preserved_lines: list[str] = []
    if older_events:
        summary_lines.append(f"Session summary ({len(older_events)} prior events):")
        for e in older_events:
            et = e["event_type"]
            p = e.get("payload", {})
            pos = e.get("stream_position", "?")
            if et in _VERBATIM_PRESERVE_TYPES:
                # Preserve PENDING/ERROR events verbatim regardless of age
                verbatim_preserved_lines.append(
                    f"  [{pos}] {et} [PRESERVED]: {json.dumps(p, default=str)[:200]}"
                )
            else:
                line = f"  [{pos}] {et}"
                if "application_id" in p:
                    line += f" for app {p['application_id']}"
                summary_lines.append(line)

    # Verbatim last 3 events
    verbatim_lines = ["\nRecent events (verbatim):"]
    if verbatim_preserved_lines:
        verbatim_lines.append("\nPreserved PENDING/ERROR events (verbatim):")
        verbatim_lines.extend(verbatim_preserved_lines)
    for e in last_3:
        verbatim_lines.append(f"  [{e.get('stream_position')}] {e['event_type']}: {json.dumps(e.get('payload', {}), default=str)[:200]}")

    if pending_work:
        verbatim_lines.append(f"\nPENDING WORK: {pending_work}")

    if health_status == SessionHealthStatus.NEEDS_RECONCILIATION:
        verbatim_lines.append("\n⚠️  NEEDS_RECONCILIATION: Partial state detected. Resolve before proceeding.")

    context_text = "\n".join(summary_lines + verbatim_lines)

    # Truncate to approximate token budget (rough: 4 chars ≈ 1 token)
    max_chars = token_budget * 4
    if len(context_text) > max_chars:
        context_text = context_text[:max_chars] + "\n[truncated to token budget]"

    return AgentContext(
        agent_id=agent_id,
        session_id=session_id,
        context_text=context_text,
        last_event_position=last_position,
        pending_work=pending_work,
        session_health_status=health_status,
        last_completed_action=last_completed_action,
        application_id=application_id,
        model_version=model_version,
        events_replayed=len(events),
    )
