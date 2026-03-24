# DESIGN.md — The Ledger: Architectural Tradeoff Analysis

> Six required sections. Each section presents a decision and its tradeoff analysis.
> Architecture is about tradeoffs. A decision without a tradeoff analysis is not an architectural decision — it is a default.

---

## 1. Aggregate Boundary Justification

### Decision: ComplianceRecord is a Separate Aggregate from LoanApplication

**The boundary:** `LoanApplication` stream (`loan-{id}`) records the application lifecycle.
`ComplianceRecord` stream (`compliance-{id}`) records regulatory rule evaluations and verdicts.

**What would couple if merged:**

If ComplianceRecord events were written to the `loan-{id}` stream, every agent that reads the
LoanApplication aggregate would need to process compliance events, even agents whose decisions
are independent of compliance status (e.g., the CreditAnalysisAgent that runs before compliance).

More critically: **concurrent write conflicts would multiply.** Under the current design:
- CreditAnalysisAgent writes to `loan-{id}`
- ComplianceAgent writes to `compliance-{id}`

These two agents can process the same application concurrently without collision because they
write to different streams. If merged, both agents would contend for the same stream version,
driving the OptimisticConcurrencyError rate from near-zero to near-100% at any meaningful parallelism.

**The specific failure mode avoided:**

At 100 concurrent applications with 4 agents each, if all 4 agents wrote to one stream,
expected concurrency errors per minute = (n_agents - 1) × n_applications × writes_per_agent_per_minute.
With 4 agents × 100 applications × 2 writes/min = 600 expected collisions/minute.
With separate streams: 0 cross-agent collisions. Only the intra-aggregate pattern (two fraud
agents on the same application) produces collisions — and those are the collisions we *want*
to detect.

**The coupling we accepted:**

LoanApplicationAggregate holds a reference (`compliance_pending: bool`) that it reads from
ComplianceRecord events that have been separately appended. This requires the command handler to
load both aggregates before approving an application. This is an acceptable read-side coupling
(load and check) vs. write-side coupling (append to same stream), which would be catastrophic.

---

## 2. Projection Strategy

### ApplicationSummary

**Async (not inline).** SLO: p99 < 500ms lag.

Justification: Application summary is read far more often than it is written.
Inline projection would add a DB write per event append, increasing write latency under load.
Async daemon allows writes to complete at their natural rate and projections to catch up.
500ms is acceptable for a loan officer dashboard — decisions are not made in milliseconds.

### AgentPerformanceLedger

**Async.** SLO: p99 < 500ms lag.

Justification: Performance metrics are analytical, not operational. A 500ms lag in
"has agent v2.3 been making different decisions?" has no operational consequence — the
question is answered in batch, not in real-time.

### ComplianceAuditView (critical)

**Async with in-memory snapshot strategy.** SLO: p99 < 2 seconds lag.

**Snapshot strategy:** The ComplianceAuditView maintains a full event history list per
application (`_events: dict[str, list[dict]]`). Temporal queries (`get_compliance_at`)
filter this list by timestamp and replay in-memory. This is the "event-count trigger"
approach where the full history is always in memory — a snapshot is the in-memory state itself.

**Snapshot invalidation:** On `rebuild_from_scratch()`, the in-memory state is cleared and all
events are replayed from global position 0. This is safe for compliance views because compliance
events are low-volume (tens per application, not thousands). For very large deployments, a
time-trigger snapshot strategy (every 1000 events or every 24 hours) would be preferred, with
snapshot records stored in a `compliance_snapshots` table.

**Tradeoff accepted:** In-memory temporal state means that if the server restarts, the projection
must rebuild from scratch before serving temporal queries. This is acceptable because:
(a) compliance queries are low-frequency, (b) rebuild completes in seconds for current volume,
(c) the alternative (storing snapshots per timestamp in Postgres) adds schema complexity that
is not justified until volume exceeds 100K events per application.

---

## 3. Concurrency Analysis

### Expected OptimisticConcurrencyError Rate Under Peak Load

**Peak scenario:** 100 concurrent applications, 4 agents each, each agent makes 2 appends
per application (context load + decision).

**Per-stream conflict probability:**

Each `loan-{id}` stream is written by:
- CreditAnalysisAgent (1 append)
- FraudDetectionAgent (1 append to `fraud-{id}` — different stream, no conflict)
- ComplianceAgent (1 append to `compliance-{id}` — different stream)
- DecisionOrchestratorAgent (1 append, after all analyses)

Sequential design means only the Orchestrator and CreditAgent contend on `loan-{id}`.
If both arrive within the same 10ms window (common under load), one will fail.

**Estimate:** At 100 applications × 2 contentious appends each × 20% collision probability
at peak = **40 OptimisticConcurrencyErrors/minute** on loan streams.

FraudDetection and Compliance write to separate streams: **0 cross-agent collisions**.

**Retry strategy:**

```
max_retries = 3
backoff = exponential: 10ms, 50ms, 200ms (+ jitter ±10ms)
```

1. Catch `OptimisticConcurrencyError`.
2. Sleep `backoff[attempt] × (1 + random uniform(0, 0.1))`.
3. Reload aggregate: `app = await LoanApplicationAggregate.load(store, application_id)`.
4. Re-validate business rules against new state.
5. Re-compute events if state changed (e.g., CreditAnalysis already completed by another agent).
6. Re-attempt append.
7. After 3 retries: return `ConflictError` to caller with `suggested_action: "queue_and_retry_after_30s"`.

**Maximum retry budget:** 3 retries × 200ms = 600ms worst case. Beyond that, the application
is experiencing hot contention and needs queue-based serialization (one writer at a time via a
distributed lock or command queue), which is implemented in production via a `command_inbox` table.

---

## 4. Upcasting Inference Decisions

### CreditAnalysisCompleted v1 → v2

**Fields added:** `model_version`, `confidence_score`, `regulatory_basis`

| Field | Inference Strategy | Error Rate | Downstream Consequence of Error |
|-------|-------------------|------------|----------------------------------|
| `model_version` | `"legacy-pre-2026"` — documented sentinel | 100% (all v1 events predate model versioning) | Audit records show "legacy" — acceptable, signals lack of version data |
| `confidence_score` | `None` — genuinely unknown | 0% (null is always correct for unknown) | Downstream must handle null; no fabricated confidence is passed to decision logic |
| `regulatory_basis` | `[]` — empty list | ~80% (most v1 events had no explicit basis) | Compliance queries show no regulatory basis — conservative, triggers manual review |

**Why null over inference for confidence_score:**
Fabricating a confidence score (e.g., `0.75`) would be processed by downstream systems as a real
value, potentially satisfying the confidence floor check (≥0.6) and changing the decision outcome.
A fabricated regulatory fact that changes a historical decision is a compliance violation.
Null explicitly signals "this data did not exist" — downstream systems can then show "N/A" in the
UI instead of a fabricated number that appears authoritative.

### DecisionGenerated v1 → v2

**Fields added:** `model_versions{}`

| Field | Inference Strategy | Error Rate | Downstream Consequence |
|-------|-------------------|------------|------------------------|
| `model_versions` | `{}` — empty dict | ~100% (v1 had no per-agent versioning) | Performance ledger cannot break down by model version for historical decisions |

**Why no store lookup in upcaster:**
The challenge specification considers loading contributing sessions during upcasting to reconstruct
`model_versions`. This is architecturally incorrect: upcasters must be **pure functions** (payload
in → payload out, no I/O). If an upcaster performs a store lookup, then:
1. Every `load_stream()` call triggers N additional DB queries (N+1 problem).
2. The upcaster becomes non-deterministic (its output depends on the current store state).
3. Testing requires a live database instead of simple dict fixtures.

The correct pattern: applications that need `model_versions` for historical v1 events should
query contributing sessions separately via a dedicated read model.

---

## 5. EventStoreDB Comparison

### Mapping PostgreSQL Implementation to EventStoreDB Concepts

| This Implementation | EventStoreDB 24.x Equivalent |
|--------------------|------------------------------|
| `events` table with `stream_id` partitioning | Native event streams — every stream is a first-class concept, no shared table |
| `event_streams` table with `current_version` | Built-in stream metadata: `$stream-metadata` system stream |
| `store.append(expected_version=N)` | `AppendToStreamAsync(streamName, StreamState.AtRevision(N))` |
| `store.load_stream(stream_id)` | `ReadStreamAsync(Direction.Forwards, streamName)` |
| `store.load_all(from_global_position=N)` | `ReadAllAsync(Direction.Forwards, new Position(N, N))` — the `$all` stream |
| `ProjectionDaemon` polling loop | EventStoreDB **Persistent Subscriptions** — server-side fan-out, exactly-once delivery per group |
| `projection_checkpoints` table | Persistent subscription `checkpointAfter` and `checkpointLowerBound` config |
| `outbox` table for at-least-once delivery | EventStoreDB subscriptions guarantee at-least-once delivery natively |
| `UpcasterRegistry` applied on `load_stream()` | EventStoreDB `$projections` — server-side; or client-side transform in `EventAppeared` handler |

**What EventStoreDB gives you that this implementation must work harder to achieve:**

1. **Persistent subscriptions with competing consumers:** EventStoreDB delivers each event to
   exactly one consumer in a group, enabling parallel projection processing across nodes. This
   implementation requires a distributed lock (advisory lock in PostgreSQL via `pg_try_advisory_lock`)
   to prevent multiple projection daemons from processing the same event.

2. **The `$all` stream:** EventStoreDB maintains a global total-order stream natively. This
   implementation emulates it with `global_position BIGINT GENERATED ALWAYS AS IDENTITY` — which
   is correct but requires explicit management of the identity sequence under failover scenarios.

3. **Native gRPC subscriptions:** EventStoreDB pushes events to subscribers (push model). This
   implementation polls on a 100ms interval (pull model). Under low load, the pull model wastes
   CPU; under high load, the 100ms interval creates unnecessary lag. EventStoreDB's push model
   delivers events with sub-millisecond latency.

4. **Category projections:** EventStoreDB automatically creates `$ce-{category}` streams
   (e.g., `$ce-loan`) containing all events across streams of that type. This implementation
   requires explicit `event_type` filter in `load_all()`.

---

## 6. What I Would Do Differently

**The single most significant architectural decision I would reconsider: the projection storage strategy.**

Currently, all three projections (ApplicationSummary, AgentPerformanceLedger, ComplianceAuditView)
are **in-memory only**. When the process restarts, all projection state is lost and must be rebuilt
from event position 0. For a system with 29 seed applications and ~1,847 events, rebuild takes
under 1 second. At production scale (10,000 applications, 500K events), rebuild would take minutes
— creating a startup blind window where health endpoints return stale data and compliance queries fail.

**What I would build instead:**

The correct pattern (which Marten 7.x implements as "Async Daemon with document store") is:
1. Projections write to **dedicated PostgreSQL tables** (`application_summary_view`,
   `compliance_audit_snapshots`, etc.) in the same transaction as the checkpoint update.
2. On startup, the daemon reads existing projection tables (no rebuild needed — already current
   as of last checkpoint).
3. Only events after `last_checkpoint` need to be replayed, which is typically seconds of lag.

The atomic boundary between checkpoint save and projection write (wrapped in one PostgreSQL
transaction) also eliminates the crash-recovery replay problem noted in the INTERIM_REPORT.md:
if the process crashes between projection update and checkpoint save, replaying the same events
produces the same projection state (idempotent by design) because the checkpoint and projection
state advance together.

**Why I did not build it this way:**

The in-memory approach was faster to implement correctly (no schema migrations, no Postgres
dependency for tests). For a week-5 submission, demonstrating the pattern is more valuable than
the production-grade persistence layer. The interface (`projection.handle(event)`) is identical
— switching to DB-backed projections requires only: (a) a schema migration, (b) replacing
`self._state[key] = ...` with `await conn.execute("INSERT ... ON CONFLICT DO UPDATE ...")`, and
(c) loading initial state from the table on startup. This is a 2-hour refactor, not a rewrite.

---

*DESIGN.md — Axiom Ledger, Week 5 Final Submission*
*Author: Axiom Ledger Team | Date: 2026-03-26*
