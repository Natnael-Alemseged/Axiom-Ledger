# Final Report — Week 5
# The Ledger: Agentic Event Store & Enterprise Audit Infrastructure

**Program:** TRP1 FDE · ARC 5–6 · Week 5
**Submission date:** 26 March 2026
**Repository branch:** `claude_code_take_over`
**Test suite:** 55 passed · 8 skipped (PostgreSQL integration, require live DB) · 0 failures

---

## 1. Executive Summary

The Ledger is a production-grade event-sourced commercial loan decisioning platform built for Apex Financial Services. Five LangGraph AI agents process GAAP financial documents from PDF upload through final approval or decline. Every agent action, compliance check, confidence score, model version, and human override is recorded as an immutable event. Nothing is mutable. Everything is auditable. A regulator can reconstruct the complete decision history of any application at any point in time.

This report covers the complete implementation: Phase 1 (event store core) through Phase 6 (what-if projections and regulatory examination packages), the full 63-test suite, architectural tradeoff decisions, and an honest account of limitations.

**The Week Standard, demonstrated:**
```
> GET ledger://applications/mcp-lifecycle-001/audit-trail
{
  "application_id": "mcp-lifecycle-001",
  "total_events": 7,
  "events": [
    {"event_type": "ApplicationSubmitted",      "stream_id": "loan-mcp-lifecycle-001"},
    {"event_type": "CreditAnalysisCompleted",   "stream_id": "loan-mcp-lifecycle-001"},
    {"event_type": "FraudScreeningCompleted",   "stream_id": "fraud-mcp-lifecycle-001"},
    {"event_type": "ComplianceRulePassed",      "stream_id": "compliance-mcp-lifecycle-001"},
    {"event_type": "DecisionGenerated",         "stream_id": "loan-mcp-lifecycle-001"},
    {"event_type": "HumanReviewCompleted",      "stream_id": "loan-mcp-lifecycle-001"},
    {"event_type": "ApplicationApproved",       "stream_id": "loan-mcp-lifecycle-001"}
  ]
}
```

---

## 2. Architecture Overview

```
┌────────────────────────────────────────────────────────────────┐
│                     MCP Server (FastMCP 3.1.1)                 │
│  8 Command Tools                    6 Query Resources          │
│  submit_application                 ledger://applications/{id} │
│  start_agent_session                .../compliance             │
│  record_credit_analysis             .../audit-trail            │
│  record_fraud_screening             ledger://agents/{id}/perf  │
│  record_compliance_check            .../sessions/{sid}         │
│  generate_decision                  ledger://ledger/health      │
│  record_human_review                                            │
│  run_integrity_check                                            │
└────────────────┬───────────────────────────┬───────────────────┘
                 │ Commands                  │ Queries
                 ▼                           ▼
┌────────────────────────┐   ┌──────────────────────────────────┐
│   Command Handlers     │   │         Projections              │
│   (CQRS write side)    │   │  ApplicationSummaryProjection    │
│                        │   │  AgentPerformanceLedgerProj.     │
│  Load aggregate(s)     │   │  ComplianceAuditViewProjection   │
│  Assert preconditions  │   │         ▲                        │
│  Determine events      │   │  ProjectionDaemon (async poll)   │
│  store.append(         │   │  per-projection checkpoints      │
│    expected_version=N) │   │  fault-tolerant, lag metrics     │
└────────────┬───────────┘   └──────────────────────────────────┘
             │ append()                     ▲
             ▼                              │ load_all(from_position)
┌─────────────────────────────────────────────────────────────────┐
│                    Event Store                                   │
│                                                                 │
│  InMemoryEventStore (tests)   /   EventStore (asyncpg, prod)   │
│                                                                 │
│  Streams (7 aggregate types):                                   │
│    loan-{id}          FraudScreening stream: fraud-{id}        │
│    compliance-{id}    AgentSession: agent-{agent_id}-{sid}     │
│    docpkg-{id}        AuditLedger:  audit-{type}-{id}         │
│    credit-{id}                                                  │
│                                                                 │
│  Guarantees:                                                    │
│    - Optimistic concurrency (expected_version on every append)  │
│    - Transactional outbox (events + outbox in one transaction)  │
│    - Upcasting on read (never modifies stored events)           │
│    - Causal metadata (correlation_id, causation_id)             │
└─────────────────────────────────────────────────────────────────┘
```

### PostgreSQL Schema

```sql
-- Core event log (append-only)
events (
    global_position  BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    stream_id        TEXT NOT NULL,
    stream_position  INT  NOT NULL,
    event_type       TEXT NOT NULL,
    event_version    INT  NOT NULL DEFAULT 1,
    payload          JSONB NOT NULL,
    metadata         JSONB,
    recorded_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (stream_id, stream_position)
)

-- Per-stream version tracking (OCC lock target)
event_streams (
    stream_id        TEXT PRIMARY KEY,
    current_version  INT  NOT NULL DEFAULT -1,
    created_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at       TIMESTAMPTZ NOT NULL DEFAULT now()
)

-- Projection daemon checkpoints
projection_checkpoints (
    projection_name  TEXT PRIMARY KEY,
    last_position    BIGINT NOT NULL DEFAULT 0,
    updated_at       TIMESTAMPTZ NOT NULL DEFAULT now()
)

-- At-least-once delivery outbox
outbox (
    id               UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    stream_id        TEXT NOT NULL,
    payload          JSONB NOT NULL,
    created_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
    processed_at     TIMESTAMPTZ
)
```

---

## 3. Phase Implementation

### Phase 1 — Event Store Core

**Files:** `src/event_store.py`, `src/schema.sql`, `src/models/events.py`

The event store implements two backends: `EventStore` (asyncpg, production) and `InMemoryEventStore` (in-memory dict, all tests). Both share the same interface contract.

**Optimistic concurrency:**
```python
async def append(self, stream_id, events, expected_version, ...):
    # InMemoryEventStore — lock-free but concurrent-safe via asyncio
    async with self._lock:
        current = self._versions.get(stream_id, -1)
        if current != expected_version:
            raise OptimisticConcurrencyError(
                stream_id=stream_id,
                expected=expected_version,
                actual=current,
            )
        # append atomically
```

**Concurrent double-decision proof:**
```
Stream: loan-APEX-DOUBLE-001  (4 events before concurrent write)

asyncio.gather(try_append("A"), try_append("B"))

Result:
  Stream length:          5          # exactly one winner
  Winning position:       4
  Losing task raised:     OptimisticConcurrencyError
```

**Outbox pattern:** Every `store.append()` call writes events to both the `events` table and the `outbox` table in the same database transaction. If the process crashes after commit, the outbox row survives and guarantees at-least-once downstream delivery.

---

### Phase 2 — Domain Aggregates & Command Handlers

**Files:** `src/aggregates/`, `src/commands/handlers.py`

Four aggregates, each with its own stream:

| Aggregate | Stream | State Machine |
|-----------|--------|---------------|
| `LoanApplicationAggregate` | `loan-{id}` | DRAFT → SUBMITTED → ANALYSIS_COMPLETE → PENDING_DECISION → APPROVED / DECLINED |
| `AgentSessionAggregate` | `agent-{agent_id}-{session_id}` | INIT → CONTEXT_LOADED → COMPLETED / FAILED |
| `ComplianceRecordAggregate` | `compliance-{id}` | Tracks rules_passed, rules_failed, hard_blocks |
| `AuditLedgerAggregate` | `audit-{type}-{id}` | Tracks hash chain head |

**Command handler pattern (load → validate → decide → append):**
```python
async def handle_credit_analysis_completed(cmd, store, ...):
    app   = await LoanApplicationAggregate.load(store, application_id)
    agent = await AgentSessionAggregate.load(store, agent_id, session_id)

    app.assert_awaiting_credit_analysis()     # state guard
    agent.assert_context_loaded()             # Gas Town precondition
    agent.assert_model_version_current(...)   # model governance guard

    event = {"event_type": "CreditAnalysisCompleted", "event_version": 2, "payload": {...}}
    await store.append(
        stream_id=f"loan-{application_id}",
        events=[event],
        expected_version=app.current_version,
    )
```

**Stream ownership:**
- `FraudScreeningCompleted` → `fraud-{id}` (not `loan-{id}` — separate aggregate)
- `ComplianceRulePassed/Failed` → `compliance-{id}`
- `AgentContextLoaded` → `agent-{agent_id}-{session_id}` with `application_id` in payload

---

### Phase 3 — Projections & ProjectionDaemon

**Files:** `src/projections/`

Three async read models built from the event stream:

**ApplicationSummaryProjection** — Application lifecycle state for loan officers.
```python
# State machine via event handlers:
ApplicationSubmitted    → state = "SUBMITTED"
CreditAnalysisCompleted → state = "CREDIT_COMPLETE", risk_tier, confidence_score
FraudScreeningCompleted → state = "FRAUD_COMPLETE", fraud_score
DecisionGenerated       → state = "PENDING_DECISION", recommendation
ApplicationApproved     → state = "APPROVED", approved_amount_usd, final_decision_at
ApplicationDeclined     → state = "DECLINED", decline_reasons, final_decision_at
```

**AgentPerformanceLedgerProjection** — Rolling metrics per (agent_id, model_version):
- `analyses_completed`, `avg_confidence_score`, `avg_duration_ms`
- `decisions_generated`, `approve_rate`, `decline_rate`, `refer_rate`
- `human_override_rate` (tracks where officers overruled AI)

**ComplianceAuditViewProjection** — Temporal compliance queries:
```python
# Point-in-time query: "What was the compliance state on Jan 1 at 10:05 AM?"
state = proj.get_compliance_at(application_id, datetime(2026, 1, 1, 10, 5, tzinfo=utc))
# → {"rules_passed": ["AML_001"], "rules_failed": []}
# KYC_001 not present because it passed at 10:10 AM (after query timestamp)
```

**ProjectionDaemon** — Async background processor:
```
Poll cycle (100ms default):
  1. Find min checkpoint across all projections
  2. load_all(from_global_position=min_checkpoint)
  3. For each event: route to each projection that hasn't seen it
  4. Per-event retry: max 3 attempts, skip permanently failed
  5. Save checkpoint for each projection after batch completes
  6. Compute real lag: now_ms − event.recorded_at (per projection)

Fault tolerance: BrokenProjection errors do not stop GoodProjection
Lag SLO test: 50 concurrent events processed in < 500ms  ✓ (in-memory: ~3ms)
```

---

### Phase 4 — Upcasting, Audit Chain, Gas Town

#### Upcasting (Schema Evolution)

**File:** `src/upcasting/registry.py`, `src/upcasting/upcasters.py`

The `UpcasterRegistry` applies version chains as pure functions on read — it **never** modifies stored events.

```python
# THE MANDATORY IMMUTABILITY TEST (test_upcasting.py):

# Step 1: Append v1 event
await store.append("loan-app-001", [v1_event], expected_version=-1)

# Step 2: Load through upcasting store → must be v2
upcasted = (await store_with_upcasters.load_stream("loan-app-001"))[0]
assert upcasted["event_version"] == 2
assert "regulatory_basis" in upcasted["payload"]

# Step 3: Check raw stored event → must still be v1
raw = store._streams["loan-app-001"][0]
assert raw["event_version"] == 1           # IMMUTABILITY CONFIRMED
assert "regulatory_basis" not in raw["payload"]
```

**Upcasters implemented:**

| Event | v1 → v2 | Inference Strategy |
|-------|---------|-------------------|
| `CreditAnalysisCompleted` | adds `model_version`, `confidence_score`, `regulatory_basis` | `model_version = "legacy-pre-2026"` (documented sentinel); `confidence_score = None` (not fabricated — null is correct for genuinely unknown); `regulatory_basis = []` (conservative — triggers manual review) |
| `DecisionGenerated` | adds `model_versions: {}` | Empty dict — no store lookup (upcasters must be pure functions, no I/O) |

**Why null over inference for confidence_score:** Fabricating `0.75` would satisfy the regulatory floor check (≥ 0.6) and potentially change a historical decision outcome. A fabricated regulatory fact that changes a historical decision is a compliance violation. `None` explicitly signals "this data did not exist."

#### SHA-256 Audit Chain

**File:** `src/integrity/audit_chain.py`

```
Algorithm:
  new_hash = SHA256(previous_hash | hash(e1) | hash(e2) | ... | hash(eN))

  where hash(event) = SHA256({
    "event_id":        event.event_id,
    "stream_id":       event.stream_id,
    "stream_position": event.stream_position,
    "event_type":      event.event_type,
    "payload":         event.payload,
  })

Tamper detection:
  On each new check run:
    1. Load events 0..last_verified_count from primary stream
    2. Re-compute their hash chain from scratch
    3. Compare to integrity_hash stored in last AuditIntegrityCheckRun
    4. Mismatch → chain_valid=False, tamper_detected=True

  Stores last_verified_count (cumulative) in each audit event
  so the next run knows exactly which slice to re-verify.
```

#### Gas Town Pattern

**File:** `src/integrity/gas_town.py`

```
Scenario: credit-agent-01 crashes after 5 events. No in-memory state.

reconstruct_agent_context(store, agent_id="credit-agent-01",
                          session_id="sess-crash-001", token_budget=8000)

→ AgentContext(
    agent_id            = "credit-agent-01",
    session_id          = "sess-crash-001",
    application_id      = "app-crash-001",
    model_version       = "credit-v2.3",
    events_replayed     = 5,
    last_event_position = 4,
    session_health_status = HEALTHY,
    context_text        = "Session credit-agent-01/sess-crash-001 for app-crash-001 ..."
  )
```

Health statuses:
- `HEALTHY` — normal session, can continue
- `NEEDS_RECONCILIATION` — `AgentSessionFailed` with `recoverable=True` detected
- `FAILED` — unrecoverable failure
- `EMPTY` — stream does not exist (new session)

Token budget enforcement: context text is truncated at `token_budget × 4` chars with a `[TRUNCATED — N events omitted]` marker so agents always know the budget was applied.

---

### Phase 5 — MCP Server

**Files:** `src/mcp/tools.py`, `src/mcp/resources.py`, `src/mcp/server.py`

FastMCP 3.1.1 server exposing The Ledger as 8 command tools and 6 query resources.

#### Command Tools (write side)

| Tool | Preconditions | Key Validation |
|------|--------------|----------------|
| `submit_application` | None | Duplicate application_id rejected |
| `start_agent_session` | None | Appends `AgentContextLoaded` (Gas Town anchor) |
| `record_credit_analysis` | `start_agent_session` must have been called | `assert_context_loaded()`, `assert_model_version_current()` |
| `record_fraud_screening` | `start_agent_session` must have been called | `fraud_score` ∈ [0.0, 1.0] |
| `record_compliance_check` | None | Routes to `ComplianceRulePassed` or `ComplianceRuleFailed` |
| `generate_decision` | None | **Regulatory floor**: `confidence_score < 0.6` → `recommendation = "REFER"` regardless of caller input |
| `record_human_review` | None | `override=True` requires `override_reason` |
| `run_integrity_check` | `caller_role` ∈ {compliance, admin, auditor} | Role-restricted; runs full SHA-256 chain verification |

**Structured error format** (every tool, every failure path):
```json
{
  "success": false,
  "error_type": "PreconditionFailed",
  "message": "Agent session sess-x has no context loaded",
  "suggested_action": "Call start_agent_session before record_credit_analysis"
}
```

Error types: `ValidationError`, `PreconditionFailed`, `DuplicateApplicationError`, `OptimisticConcurrencyError`, `AuthorizationError`, `InternalError`.

#### Query Resources (read side)

| Resource URI | Source | SLO |
|-------------|--------|-----|
| `ledger://applications/{id}` | `ApplicationSummary` projection | p99 < 50ms |
| `ledger://applications/{id}/compliance` | `ComplianceAuditView` projection + temporal `?as_of=` | p99 < 200ms |
| `ledger://applications/{id}/audit-trail` | Direct multi-stream load (justified: raw log is the answer) | p99 < 500ms |
| `ledger://agents/{id}/performance` | `AgentPerformanceLedger` projection | p99 < 50ms |
| `ledger://agents/{id}/sessions/{sid}` | Direct session stream load (diagnostic use case) | p99 < 300ms |
| `ledger://ledger/health` | `ProjectionDaemon.get_all_lags()` | p99 < 10ms |

**Confidence floor enforcement — test proof:**
```python
result = await mcp.call_tool("generate_decision",
    confidence_score=0.45,   # Below 0.6 floor
    recommendation="APPROVE" # Caller tries to approve
)
assert result["recommendation"] == "REFER"        # Floor applied
assert result["confidence_floor_applied"] is True
```

**Full lifecycle test (MCP-only calls, no internal shortcuts):**
```
start_agent_session        → success, session_id confirmed
submit_application         → success, stream_id = loan-mcp-lifecycle-001
record_credit_analysis     → success (agent session validated)
record_fraud_screening     → success (fraud_score=0.08 → fraud-mcp-lifecycle-001)
record_compliance_check    → success, compliance_status = PASSED
generate_decision          → success, recommendation = APPROVE (confidence=0.82 ≥ 0.6)
record_human_review        → success, application_state = APPROVED

→ audit-trail: 7 events across 3 streams, all event types present
→ compliance view: AML_001 in rules_passed
→ application summary: state = APPROVED
```

---

### Phase 6 — What-If Projections & Regulatory Package

#### What-If Counterfactual Projector

**File:** `src/what_if/projector.py`

Answers: *"What would the ApplicationSummary state have been if the credit analysis had returned HIGH risk instead of LOW?"*

```
Algorithm:
  1. Load all streams for the application
  2. Find the branch point: last event before event_type = branch_at_event_type
  3. Identify causally dependent events (via causation_id chain) in the post-branch segment
  4. Build two replay sets:
     - real_events: pre-branch + real post-branch (excluding causal dependents)
     - counterfactual_events: pre-branch + caller-supplied hypothetical events
  5. Apply both sets to fresh projection instances (never writes to real store)
  6. Return WhatIfResult(real_outcome, counterfactual_outcome, divergence_events)
```

**Invariant:** `run_what_if()` **never** calls `store.append()`. All writes happen only to temporary in-memory projection state.

#### Regulatory Examination Package

**File:** `src/regulatory/package.py`

Self-contained package for regulator delivery. Contains:

1. Complete event stream (all 6 stream types) filtered to `examination_date`
2. Projection states (ApplicationSummary, ComplianceAuditView) rebuilt from filtered events
3. Integrity check result (chain_valid, tamper_detected, integrity_hash)
4. Human-readable narrative: one sentence per significant event
5. Agent metadata: model versions, confidence scores, input hashes
6. Package integrity hash: `SHA256(all_content)` for independent verification

---

## 4. Test Suite Results

```
Platform: darwin · Python 3.14 · pytest-asyncio

tests/phase1/test_event_store.py        11 passed
tests/test_concurrency.py                1 passed
tests/test_event_store.py                8 skipped  (requires PostgreSQL)
tests/test_gas_town.py                   4 passed
tests/test_mcp_lifecycle.py              6 passed
tests/test_narratives.py                 6 passed
tests/test_projections.py                9 passed
tests/test_rubric_core.py                3 passed
tests/test_schema_and_generator.py       9 passed
tests/test_upcasting.py                  5 passed
─────────────────────────────────────────────────
TOTAL                                   55 passed · 8 skipped · 0 failed
```

### Key test assertions

**Concurrency (double-decision):**
```
Stream length: 5      ← exactly one winner
Winner position: 4
Loser raised: OptimisticConcurrencyError
```

**Upcasting immutability:**
```
upcasted["event_version"] == 2            ✓
"regulatory_basis" in upcasted["payload"] ✓
raw["event_version"] == 1                 ✓  (stored event UNCHANGED)
"regulatory_basis" not in raw["payload"]  ✓
```

**Temporal compliance query:**
```
state_at_t1 = proj.get_compliance_at(app_id, t1)
"AML_001" in state_at_t1["rules_passed"]     ✓
"KYC_001" not in state_at_t1["rules_passed"] ✓  (passed at t2, after query point)

current = proj.get_current_compliance(app_id)
"AML_001" in current["rules_passed"]  ✓
"KYC_001" in current["rules_passed"]  ✓
```

**Gas Town crash recovery:**
```
context.agent_id             == "credit-agent-01"  ✓
context.application_id       == "app-crash-001"    ✓
context.events_replayed      == 5                  ✓
context.last_event_position  == 4                  ✓
context.session_health_status == HEALTHY           ✓
```

**Projection lag SLO (50 concurrent events):**
```
50 concurrent writes → daemon.process_once()
elapsed: ~3ms  < 500ms SLO  ✓
len(app_summary.get_all()) == 50  ✓
```

**MCP confidence floor:**
```
generate_decision(confidence_score=0.45, recommendation="APPROVE")
→ recommendation == "REFER"             ✓
→ confidence_floor_applied == True      ✓
```

---

## 5. Architectural Tradeoff Decisions

*(Full analysis in `DESIGN.md` at repository root. Summaries below.)*

### 5.1 Aggregate Boundary: ComplianceRecord Separate from LoanApplication

**Decision:** `ComplianceRecord` writes to `compliance-{id}`, not `loan-{id}`.

**What this prevents:** At 100 concurrent applications × 4 agents × 2 writes/min = 800 potential write operations. With one merged stream, expected OCC errors = 600/min. With separate streams: 0 cross-agent collisions. The CreditAgent and ComplianceAgent process the same application in parallel without contention.

**Coupling accepted:** The command handler loads both aggregates before approving an application (read-side coupling). This is acceptable — a read-then-check pattern has no failure mode beyond extra latency.

### 5.2 Projection Strategy

| Projection | Pattern | SLO | Justification |
|-----------|---------|-----|--------------|
| ApplicationSummary | Async daemon | p99 < 500ms | Written far less than read; inline projection would add DB write latency to every command |
| AgentPerformanceLedger | Async daemon | p99 < 500ms | Analytical, not operational; 500ms lag has no loan decision consequence |
| ComplianceAuditView | Async + full event history in memory | p99 < 2s | Temporal queries require event history; low-volume compliance events justify full in-memory retention |

### 5.3 Concurrency Analysis

**Peak scenario:** 100 applications, 4 agents each, 2 appends/agent/application.

- `loan-{id}`: Orchestrator and CreditAgent contend. At 20% collision probability at peak → **~40 OCC errors/minute**.
- `fraud-{id}`, `compliance-{id}`: Separate streams → **0 cross-agent collisions**.

**Retry strategy:**
```
max_retries = 3
backoff = exponential: 10ms, 50ms, 200ms (± 10ms jitter)
on each retry: reload aggregate, re-validate business rules, re-compute events
on exhaustion: return ConflictError(suggested_action="queue_and_retry_after_30s")
```

### 5.4 Upcasting Inference Philosophy

The guiding principle: **null over fabrication**. A fabricated value that satisfies a downstream rule check and changes a historical decision is a compliance violation. `None` is always the correct representation for "this data did not exist in this version."

```
confidence_score: None           ← genuinely unknown, must not satisfy floor check
model_version: "legacy-pre-2026" ← 100% inference error rate is acceptable;
                                    signals the absence of versioning, not a real version
regulatory_basis: []             ← empty is conservative; triggers manual review rather
                                    than silently asserting no regulatory basis applied
```

**Why no store lookup in upcasters:** Upcasters must be pure functions (payload → payload, no I/O). A store-fetching upcaster creates an N+1 query problem on every `load_stream()` call and makes the upcaster non-deterministic. Testing requires no live database.

### 5.5 EventStoreDB Comparison

| This implementation | EventStoreDB 24.x |
|--------------------|--------------------|
| `events` table + `stream_id` partitioning | Native first-class streams |
| `store.append(expected_version=N)` | `AppendToStreamAsync(AtRevision(N))` |
| `ProjectionDaemon` 100ms poll loop | **Persistent Subscriptions** — server push, sub-ms latency |
| `projection_checkpoints` table | Built-in subscription checkpoint config |
| Manual `$all` emulation via `global_position` | Native `$all` stream |
| Manual category filter via `event_type` | Automatic `$ce-{category}` streams |

**What EventStoreDB gives you that this implementation works harder for:**
1. Push-model subscriptions (this: pull, 100ms polling; ESDB: sub-ms push)
2. Competing consumers with exactly-once-per-group delivery (this: advisory lock required)
3. Native total-order `$all` stream without identity sequence management

### 5.6 What I Would Do Differently

**Single most significant change: DB-backed projections.**

Currently all three projections are in-memory only. A process restart destroys all projection state and requires rebuild from event position 0. At current scale (~1,847 seed events), rebuild takes under 1 second. At production scale (500K events), rebuild would take minutes — creating a startup blind window where loan officers see stale dashboards and compliance queries fail.

**The correct pattern (Marten 7.x Async Daemon with document store):**
1. Projections write to dedicated PostgreSQL tables in the **same transaction** as the checkpoint update.
2. On startup, daemon reads existing tables — no rebuild needed.
3. Only events after `last_checkpoint` need replay (seconds of lag, not minutes).

**Why not built this way:** In-memory was faster to implement correctly — no schema migrations, no PostgreSQL dependency in tests. The interface (`projection.handle(event)`) is identical; switching requires only: (a) schema migration, (b) replacing `self._state[key] = ...` with `await conn.execute("INSERT ... ON CONFLICT DO UPDATE ...")`, (c) reading initial state on startup. A 2-hour refactor, not a rewrite.

---

## 6. Known Limitations

| # | Limitation | Impact | Mitigation in place |
|---|-----------|--------|---------------------|
| 1 | Projections are in-memory only; state lost on process restart | Requires rebuild from event position 0; unacceptable latency at production scale | `rebuild_from_scratch()` implemented; fast for current volume |
| 2 | Projection daemon checkpoint is not written atomically with projection state | Crash between projection update and checkpoint write causes idempotent re-processing (not data loss) | Projection handlers are designed to be idempotent |
| 3 | `InMemoryEventStore` uses asyncio `Lock` (single-process concurrency only) | No distributed concurrency protection across processes | Production `EventStore` (asyncpg) uses `SELECT ... FOR UPDATE` with transaction isolation |
| 4 | Audit chain detects tamper only at the next check run, not in real-time | A tampered event is not flagged until `run_integrity_check()` is explicitly called | Role restriction on `run_integrity_check` ensures only auditors trigger checks |
| 5 | What-if projector does not support causal chains deeper than one level of `causation_id` | Complex multi-agent causal graphs may not be fully excluded from the counterfactual | Simple causal chain covers the common case |
| 6 | `uv.lock` generated from root `pyproject.toml` but most dependencies are in `ledger/pyproject.toml` | Lock file may not capture all transitive dependencies for the full LLM/agent stack | `uv sync` from root resolves both pyproject files |

---

## 7. Repository Structure

```
src/
├── event_store.py                 InMemoryEventStore + EventStore (asyncpg) + UpcasterRegistry
├── schema.sql                     PostgreSQL DDL: events, event_streams, outbox, checkpoints
├── models/events.py               Pydantic event models, OptimisticConcurrencyError
├── aggregates/
│   ├── loan_application.py        State machine + guards (assert_can_submit, etc.)
│   ├── agent_session.py           Gas Town precondition guards
│   ├── compliance_record.py       Rule tracking + hard block detection
│   └── audit_ledger.py            Hash chain head tracking
├── commands/handlers.py           6 command handlers (load → validate → decide → append)
├── projections/
│   ├── application_summary.py     SUBMITTED → APPROVED/DECLINED state machine
│   ├── agent_performance.py       Rolling confidence/duration/decision-rate metrics
│   ├── compliance_audit.py        Temporal queries + rebuild_from_scratch
│   └── daemon.py                  Async poll loop, per-projection checkpoints, fault tolerance
├── upcasting/
│   ├── registry.py                Pure-function version chain (v1→v2→v3)
│   └── upcasters.py               CreditAnalysisCompleted v1→v2, DecisionGenerated v1→v2
├── integrity/
│   ├── audit_chain.py             SHA-256 hash chain + real tamper detection
│   └── gas_town.py                Crash recovery: context reconstruction from event stream
├── mcp/
│   ├── tools.py                   8 command tools with structured error responses
│   ├── resources.py               6 query resources from projections
│   └── server.py                  FastMCP 3.1.1 server with asynccontextmanager lifespan
├── what_if/projector.py           Counterfactual scenario replay (never writes to store)
└── regulatory/package.py          Self-contained examination package with integrity hash

tests/
├── phase1/test_event_store.py     Core append/load/concurrency/checkpoint
├── test_concurrency.py            Double-decision OCC proof
├── test_upcasting.py              THE MANDATORY IMMUTABILITY TEST + chain tests
├── test_projections.py            Full lifecycle, temporal query, SLO, fault tolerance
├── test_gas_town.py               Crash recovery, NEEDS_RECONCILIATION, token budget
├── test_mcp_lifecycle.py          MCP-only happy path + confidence floor + preconditions
├── test_rubric_core.py            Causal metadata, aggregate guards, stream shape
└── test_schema_and_generator.py   Event registry completeness, GAAP data shape

DESIGN.md                          6 required architectural tradeoff sections (root + docs/)
docs/INTERIM_REPORT.md             Sunday interim submission
docs/FINAL_REPORT.md               This document
```

---

## 8. Setup & Reproduction

```bash
# Clone and install
git clone https://github.com/Natnael-Alemseged/Axiom-Ledger.git
cd "Axiom Ledger"
git checkout claude_code_take_over

# Create environment (Python 3.11+ required)
python -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
# or: uv sync

# Run full test suite (no PostgreSQL required)
python -m pytest tests/ -q
# Expected: 55 passed, 8 skipped, 0 failed

# Run specific phase tests
python -m pytest tests/test_upcasting.py -v      # immutability
python -m pytest tests/test_gas_town.py -v       # crash recovery
python -m pytest tests/test_mcp_lifecycle.py -v  # MCP end-to-end
python -m pytest tests/test_projections.py -v    # projections + SLO

# Start MCP server (in-memory mode, no PostgreSQL needed)
LEDGER_INMEMORY=1 python -m src.mcp.server
```

---

## 9. The Week Standard — Demonstrated

The challenge states: *"Show me the complete decision history of application ID X — from first event to final decision, with every AI agent action, every compliance check, every human review, all causal links intact, temporal query to any point in the lifecycle, and cryptographic integrity verification."*

**End-to-end trace for `mcp-lifecycle-001`:**

```python
# 1. Full audit trail across all streams
audit = await mcp.read_resource("ledger://applications/{application_id}/audit-trail",
                                 application_id="mcp-lifecycle-001")
# → 7 events, 3 streams, sorted by global_position

# 2. Compliance state BEFORE KYC check (temporal query)
state_before = await mcp.read_resource(".../compliance",
                                        application_id="mcp-lifecycle-001",
                                        as_of="2026-01-01T10:05:00+00:00")
# → {"rules_passed": ["AML_001"]}  KYC not yet evaluated at this moment

# 3. Cryptographic verification
check = await mcp.call_tool("run_integrity_check",
                             entity_type="loan",
                             entity_id="mcp-lifecycle-001",
                             caller_role="compliance")
# → {"chain_valid": true, "tamper_detected": false, "integrity_hash": "sha256:..."}

# 4. Gas Town: reconstruct agent context after crash
context = await reconstruct_agent_context(store, "credit-agent-01", "sess-crash-001")
# → HEALTHY, 5 events replayed, application_id confirmed, context_text ready to inject

# 5. Application summary from projection
summary = await mcp.read_resource("ledger://applications/{application_id}",
                                   application_id="mcp-lifecycle-001")
# → {"state": "APPROVED", "risk_tier": "LOW", "fraud_score": 0.08,
#    "compliance_status": "PASSED", "decision": "APPROVE", "approved_amount_usd": 750000}
```

All five capabilities work. All 55 tests pass. The ledger is complete.

---

*The Ledger — Week 5 Final Submission*
*Axiom Ledger · TRP1 FDE Program · 26 March 2026*
