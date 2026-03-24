"""
src/mcp/server.py — MCP Server Entry Point
==========================================
The Ledger MCP server exposes The Ledger as enterprise infrastructure
for AI agents and downstream systems via the Model Context Protocol.

Architecture:
  - Tools (Commands): Write events to the store via command handlers
  - Resources (Queries): Read from projections — never from raw event streams
    (except justified exceptions documented in resources.py)

Running the server:
  python -m src.mcp.server
  or:
  DB_URL=postgresql://... python -m src.mcp.server

In-memory mode (no DB required, for testing):
  LEDGER_INMEMORY=1 python -m src.mcp.server
"""
from __future__ import annotations

import asyncio
import logging
import os
from typing import Any

from fastmcp import FastMCP

from src.mcp.tools import register_tools
from src.mcp.resources import register_resources

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Build the store and projections
# ---------------------------------------------------------------------------

def _build_store():
    """Return a connected event store. Supports in-memory and PostgreSQL."""
    if os.environ.get("LEDGER_INMEMORY", "").lower() in ("1", "true", "yes"):
        from src.event_store import InMemoryEventStore
        from src.upcasting.upcasters import registry as upcaster_registry
        store = InMemoryEventStore(upcaster_registry=upcaster_registry)
        # InMemoryEventStore doesn't need async connect — wrap for uniformity
        return store
    else:
        db_url = os.environ.get(
            "DB_URL",
            "postgresql://postgres:postgres@localhost:5432/ledger",
        )
        from src.event_store import EventStore
        # NOTE: EventStore.connect() is async — in production use lifespan
        # For simple usage, callers must await store.connect() first
        from src.upcasting.upcasters import registry as upcaster_registry
        return EventStore(db_url=db_url, upcaster_registry=upcaster_registry)


# Global singletons (initialised in lifespan)
_store = None
_projections: dict[str, Any] = {}
_daemon = None


def _store_factory():
    return _store


def _projections_factory():
    return _projections


# ---------------------------------------------------------------------------
# Lifespan: connect store, start daemon
# ---------------------------------------------------------------------------

from contextlib import asynccontextmanager


@asynccontextmanager
async def _lifespan(server: FastMCP):
    global _store, _projections, _daemon

    _store = _build_store()
    if hasattr(_store, "connect"):
        await _store.connect()

    # Initialise projections
    from src.projections.application_summary import ApplicationSummaryProjection
    from src.projections.agent_performance import AgentPerformanceLedgerProjection
    from src.projections.compliance_audit import ComplianceAuditViewProjection
    from src.projections.daemon import ProjectionDaemon

    app_summary = ApplicationSummaryProjection()
    agent_perf = AgentPerformanceLedgerProjection()
    compliance_audit = ComplianceAuditViewProjection()

    daemon = ProjectionDaemon(
        store=_store,
        projections=[app_summary, agent_perf, compliance_audit],
    )

    _projections = {
        "application_summary": app_summary,
        "agent_performance": agent_perf,
        "compliance_audit": compliance_audit,
        "daemon": daemon,
    }
    _daemon = daemon

    # Start daemon in background
    daemon_task = asyncio.create_task(daemon.run_forever(poll_interval_ms=100))
    logger.info("Ledger MCP server started — daemon running")

    try:
        yield
    finally:
        await daemon.stop()
        daemon_task.cancel()
        if hasattr(_store, "close"):
            await _store.close()
        logger.info("Ledger MCP server stopped")


# ---------------------------------------------------------------------------
# Create and configure the MCP server
# ---------------------------------------------------------------------------

mcp = FastMCP(
    name="The Ledger",
    instructions=(
        "The Ledger is an event-sourced audit infrastructure for commercial loan applications. "
        "Use TOOLS to write events (commands) and RESOURCES to query the current state. "
        "ALWAYS call start_agent_session before any agent decision tool. "
        "ALWAYS check ledger://ledger/health before starting a workflow."
    ),
    lifespan=_lifespan,
)

# Register tools and resources
register_tools(mcp, _store_factory)
register_resources(mcp, _store_factory, _projections_factory)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import sys
    logging.basicConfig(level=logging.INFO)
    mcp.run(transport="stdio")
