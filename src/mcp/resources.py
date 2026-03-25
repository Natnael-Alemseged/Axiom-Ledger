"""
src/mcp/resources.py — MCP Resources (Query Side)
==================================================
6 MCP resources implementing the CQRS query side.

Resources MUST read from projections — never replay aggregate streams
(except justified exceptions: AuditLedger stream and AgentSession stream
for full audit trail, where streaming the raw log is the correct answer).

SLO targets (defined in DESIGN.md):
  ledger://applications/{id}              p99 < 50ms
  ledger://applications/{id}/compliance   p99 < 200ms
  ledger://applications/{id}/audit-trail  p99 < 500ms
  ledger://agents/{id}/performance        p99 < 50ms
  ledger://agents/{id}/sessions/{sid}     p99 < 300ms
  ledger://ledger/health                  p99 < 10ms
"""
from __future__ import annotations

from datetime import datetime
from typing import Any

from fastmcp import FastMCP


def register_resources(mcp: FastMCP, store_factory, projections_factory) -> None:
    """
    Register all 6 query-side resources.
    projections_factory() returns dict with keys:
      'application_summary', 'agent_performance', 'compliance_audit', 'daemon'
    """

    # ------------------------------------------------------------------
    # RESOURCE 1: ledger://applications/{id}
    # ------------------------------------------------------------------
    @mcp.resource("ledger://applications/{application_id}")
    async def get_application(application_id: str) -> dict:
        """
        Current state summary for a loan application.
        Source: ApplicationSummary projection.
        SLO: p99 < 50ms.
        """
        projections = projections_factory()
        summary_proj = projections.get("application_summary")
        if summary_proj is None:
            return {"error": "ApplicationSummary projection not available"}
        result = summary_proj.get(application_id)
        if result is None:
            return {"error": f"Application {application_id!r} not found"}
        return result

    # ------------------------------------------------------------------
    # RESOURCE 2: ledger://applications/{id}/compliance
    # ------------------------------------------------------------------
    @mcp.resource("ledger://applications/{application_id}/compliance")
    async def get_application_compliance(
        application_id: str,
        as_of: str | None = None,
    ) -> dict:
        """
        Compliance audit view for an application.
        Source: ComplianceAuditView projection.
        Supports temporal query: ?as_of=ISO-timestamp (compliance state at that moment).
        SLO: p99 < 200ms.
        """
        projections = projections_factory()
        compliance_proj = projections.get("compliance_audit")
        if compliance_proj is None:
            return {"error": "ComplianceAuditView projection not available"}

        if as_of:
            try:
                ts = datetime.fromisoformat(as_of.replace("Z", "+00:00"))
                result = compliance_proj.get_compliance_at(application_id, ts)
            except ValueError:
                return {"error": f"Invalid as_of timestamp: {as_of!r}"}
        else:
            result = compliance_proj.get_current_compliance(application_id)

        if result is None:
            return {"error": f"No compliance record for application {application_id!r}"}
        return result

    # ------------------------------------------------------------------
    # RESOURCE 3: ledger://applications/{id}/audit-trail
    # ------------------------------------------------------------------
    @mcp.resource("ledger://applications/{application_id}/audit-trail")
    async def get_audit_trail(
        application_id: str,
        from_position: int = 0,
        to_position: int | None = None,
    ) -> dict:
        """
        Full audit trail for a loan application (all streams).
        Justified exception: loads directly from event streams (not a projection)
        because the audit trail is the raw event log by design — a projection
        would be a redundant copy of the same data.
        SLO: p99 < 500ms.
        """
        store = store_factory()
        streams = [
            f"loan-{application_id}",
            f"docpkg-{application_id}",
            f"credit-{application_id}",
            f"fraud-{application_id}",
            f"compliance-{application_id}",
            f"audit-loan-{application_id}",
        ]

        all_events = []
        for stream_id in streams:
            try:
                events = await store.load_stream(
                    stream_id,
                    from_position=from_position,
                    to_position=to_position,
                )
                for e in events:
                    all_events.append({
                        "stream_id": e["stream_id"],
                        "stream_position": e["stream_position"],
                        "global_position": e["global_position"],
                        "event_type": e["event_type"],
                        "event_version": e["event_version"],
                        "payload": e["payload"],
                        "metadata": e.get("metadata", {}),
                        "recorded_at": str(e.get("recorded_at", "")),
                    })
            except Exception:
                pass  # Stream may not exist for all applications

        all_events.sort(key=lambda e: (e.get("global_position", 0),))
        return {
            "application_id": application_id,
            "total_events": len(all_events),
            "events": all_events,
        }

    # ------------------------------------------------------------------
    # RESOURCE 4: ledger://agents/{id}/performance
    # ------------------------------------------------------------------
    @mcp.resource("ledger://agents/{agent_id}/performance")
    async def get_agent_performance(agent_id: str, model_version: str | None = None) -> dict:
        """
        Performance metrics for an AI agent.
        Source: AgentPerformanceLedger projection.
        SLO: p99 < 50ms.
        """
        projections = projections_factory()
        perf_proj = projections.get("agent_performance")
        if perf_proj is None:
            return {"error": "AgentPerformanceLedger projection not available"}
        results = perf_proj.get(agent_id, model_version)
        return {
            "agent_id": agent_id,
            "model_version_filter": model_version,
            "records": results,
        }

    # ------------------------------------------------------------------
    # RESOURCE 5: ledger://agents/{id}/sessions/{session_id}
    # ------------------------------------------------------------------
    @mcp.resource("ledger://agents/{agent_id}/sessions/{session_id}")
    async def get_agent_session(agent_id: str, session_id: str) -> dict:
        """
        Full replay of a specific agent session stream.
        Justified direct stream load: session replay is the diagnostic use case —
        reading a pre-built projection would not provide the raw reasoning trace.
        SLO: p99 < 300ms.
        """
        store = store_factory()
        stream_id = f"agent-{agent_id}-{session_id}"
        try:
            events = await store.load_stream(stream_id)
            return {
                "agent_id": agent_id,
                "session_id": session_id,
                "stream_id": stream_id,
                "total_events": len(events),
                "events": [
                    {
                        "stream_position": e["stream_position"],
                        "event_type": e["event_type"],
                        "event_version": e["event_version"],
                        "payload": e["payload"],
                        "recorded_at": str(e.get("recorded_at", "")),
                    }
                    for e in events
                ],
            }
        except Exception as ex:
            return {"error": f"Could not load session {session_id!r}: {ex}"}

    # ------------------------------------------------------------------
    # RESOURCE 6: ledger://ledger/health
    # ------------------------------------------------------------------
    @mcp.resource("ledger://ledger/health")
    async def get_ledger_health() -> dict:
        """
        Projection daemon health and lag metrics.
        Source: ProjectionDaemon.get_all_lags().
        SLO: p99 < 10ms — this is the watchdog endpoint.
        """
        projections = projections_factory()
        daemon = projections.get("daemon")
        if daemon is None:
            return {
                "status": "degraded",
                "message": "ProjectionDaemon not running",
                "lags": {},
            }
        lags = daemon.get_all_lags()
        max_lag = max(lags.values(), default=0.0)
        status = "healthy" if max_lag < 500 else "degraded"
        return {
            "status": status,
            "projection_lags_ms": lags,
            "max_lag_ms": max_lag,
        }
