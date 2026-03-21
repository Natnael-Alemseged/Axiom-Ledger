# Interim Report - Week 5 (Sunday Submission)

This report is prepared against the interim deliverable requirements in `TRP1 Challenge Week 5 Agentic Event Store & Enterprise Audit Infrastructure.md`.

## 1) DOMAIN_NOTES.md Content (Complete, Graded Deliverable)

The following is the current complete `DOMAIN_NOTES.md` content.

# DOMAIN_NOTES.md — Graded Deliverable

### 1. EDA vs. ES Distinction
**Question:** A component uses callbacks (like LangChain traces) to capture event-like data. Is this Event-Driven Architecture (EDA) or Event Sourcing (ES)? If you redesigned it using The Ledger, what exactly would change in the architecture and what would you gain?

**Answer:** 
The use of callbacks for LangChain traces is an example of **Event-Driven Architecture (EDA)**. In this setup, "events" are side-effects-emitted after an action occurs primarily for observability or downstream triggers. The system's state is held in a traditional database (or just in memory), and these traces are merely logs of what happened. If the traces are lost, the system's current state remains intact.

**Redesign using The Ledger (ES):**
If redesigned with The Ledger, the architecture would shift such that the **events themselves ARE the source of truth**.
- **Architectural Change:** Instead of an agent updating a `status` column in a `sessions` table, it would append a `ThinkingStarted`, `ToolCalled`, or `DecisionFinalized` event to a stream. To determine the current state of an agentic process, the system would **replay** these events from the Ledger. The database would store no "current state" for the aggregate, only the immutable list of events.
- **Gains:**
    - **Perfect Reproducibility:** You can re-run an agent's logic on the exact sequence of events to debug a specific failure.
    - **Temporal Queries:** You can ask "What did the agent know at 10:00 AM?" by replaying events up to that timestamp.
    - **Gas Town Pattern Resilience:** If the agent process crashes, it can perfectly reconstruct its context window and internal state by replaying its own stream, eliminating "ephemeral memory" failures.

---

### 2. The Aggregate Question
**Question:** In the scenario below, you will build four aggregates. Identify one alternative boundary you considered and rejected. What coupling problem does your chosen boundary prevent?

**Answer:**
The four aggregates for this scenario are:
1. `LoanApplication`: Lifecycle state machine from submission through human-reviewed final decision.
2. `AgentSession`: Per-agent execution trace (context load, model version, inputs, and outputs).
3. `ComplianceRecord`: Regulation checks and rule-level pass/fail evidence for an application.
4. `AuditLedger`: Cross-stream integrity and causal trace for a business entity.

**Alternative Boundary Considered & Rejected:**
I considered merging `ComplianceRecord` into `LoanApplication`.

**Reason for Rejection & Coupling Prevention:**
I rejected this because compliance evidence evolves at a different cadence than loan decisioning and can be updated independently (new regulation set versions, remediation checks, re-runs). Keeping `ComplianceRecord` separate prevents **write-hot coupling** on `loan-{application_id}` streams during concurrent agent execution. Without this boundary, compliance rule updates and decision-state transitions would contend for the same expected version window, increasing OptimisticConcurrencyError rates and creating avoidable retries during high-throughput periods.

---

### 3. Concurrency in Practice
**Question:** Two AI agents simultaneously process the same loan application and both call `append_events` with `expected_version=3`. Trace the exact sequence of operations in your event store. What does the losing agent receive, and what must it do next?

**Answer:**
**Trace of Operations:**
1. **Initial State:** Stream `loan-123` is at `version: 3`.
2. **Agent A (Fraud):** Reads stream, prepares `FraudCheckPassed`, and calls `append_events(expected_version=3)`.
3. **Agent B (Compliance):** Reads stream, prepares `ComplianceCheckPassed`, and calls `append_events(expected_version=3)`.
4. **Event Store (Request A):** Starts a transaction. Checks `SELECT version FROM streams WHERE id='loan-123'`. It finds `3`. Matches `expected_version`.
5. **Event Store (Request A):** Inserts the event, updates stream version to `4`, and commits.
6. **Event Store (Request B):** Starts a transaction. Checks `SELECT version FROM streams WHERE id='loan-123'`. It finds **`4`**.
7. **Conflict Detection:** The `expected_version` (3) does not match the current version (4).

**Losing Agent Outcome:**
Agent B receives a `ConcurrencyException` (or `WrongExpectedVersionError`).

**Next Steps:**
Agent B must **Reload and Re-evaluate**. It should:
1. Reload the entire event stream (now including version 4).
2. "Apply" version 4 (`FraudCheckPassed`) to its internal state.
3. Determine if its intended action is still valid given the new fact (e.g., if fraud had failed, compliance might not even need to run).
4. If still valid, call `append_events(expected_version=4)`.

---

### 4. Projection Lag and Its Consequences
**Question:** Your `LoanApplication` projection is eventually consistent with a typical lag of 200ms. A loan officer queries "available credit limit" immediately after an agent commits a disbursement event. They see the old limit. What does your system do, and how do you communicate this to the user interface?

**Answer:**
**System Strategy:**
The system uses **Sequence-based Synchronization**. Every write to the Event Store returns the new `sequence_number` (or `position`) of the event.

**Implementation Details:**
- **Read Side:** The query API allows passing a `min_sequence` parameter.
- **Wait Strategy:** If the UI requires strong consistency for this specific query, the projection layer can "block" (with a timeout) until the Async Daemon has processed events up to that `min_sequence`.

**Communication to the UI:**
1. **Optimistic UI:** The UI can locally subtract the disbursement amount if it knows the event was successful, providing immediate feedback.
2. **Stale Data Indicator:** The UI header or the specific field should display a "Syncing..." spinner or a "Last updated: 2 seconds ago" label if the projection's checkpoint lags behind the head of the stream.
3. **Sequence Tracking:** The browser can store the last committed sequence ID. If the queried projection sequence is lower, the UI shows a "Processing newest updates..." banner to set user expectations.

---

### 5. The Upcasting Scenario
**Question:** The `CreditDecisionMade` event was defined in 2024 with `{application_id, decision, reason}`. In 2026 it needs `{application_id, decision, reason, model_version, confidence_score, regulatory_basis}`. Write the upcaster. What is your inference strategy for historical events that predate `model_version`?

**Answer:**

**Upcaster Code:**
```python
from datetime import datetime, timezone

# Deployment timeline and policy windows are immutable lookup tables.
MODEL_VERSION_BY_DATE = [
    (datetime(2024, 1, 1, tzinfo=timezone.utc), "credit-model-2024.1"),
    (datetime(2025, 4, 1, tzinfo=timezone.utc), "credit-model-2025.2"),
    (datetime(2026, 1, 1, tzinfo=timezone.utc), "credit-model-2026.1"),
]

REGULATORY_BASIS_BY_DATE = [
    (datetime(2024, 1, 1, tzinfo=timezone.utc), "BASEL-III-2024-POLICYSET-A"),
    (datetime(2025, 7, 1, tzinfo=timezone.utc), "BASEL-III-2025-POLICYSET-B"),
    (datetime(2026, 1, 1, tzinfo=timezone.utc), "BASEL-IV-2026-POLICYSET-C"),
]


def infer_model_version(recorded_at: datetime) -> str:
    selected = MODEL_VERSION_BY_DATE[0][1]
    for effective_at, model_version in MODEL_VERSION_BY_DATE:
        if recorded_at >= effective_at:
            selected = model_version
        else:
            break
    return selected


def infer_regulatory_basis(recorded_at: datetime) -> str:
    selected = REGULATORY_BASIS_BY_DATE[0][1]
    for effective_at, regulatory_basis in REGULATORY_BASIS_BY_DATE:
        if recorded_at >= effective_at:
            selected = regulatory_basis
        else:
            break
    return selected


def upcast_credit_decision_v1_to_v2(event_data: dict, recorded_at: datetime) -> dict:
    """Transforms CreditDecisionMade v1 into v2 without fabricating unknown data."""
    return {
        **event_data,
        "model_version": infer_model_version(recorded_at),
        "confidence_score": None,  # Explicitly unknown for v1 events.
        "regulatory_basis": infer_regulatory_basis(recorded_at),
        "schema_version": 2,
    }
```

**Inference Strategy:**
For historical events, we use a **timestamp-based deterministic inference** strategy:
- `model_version`: Inferred from `recorded_at` by querying a deployment timeline table (or equivalent immutable lookup), not a static hard-coded legacy string.
- `confidence_score`: Set to `null` for all v1 events because the score was never computed in the original schema. Fabricating a value (for example `1.0`) would pollute downstream analytics, bias model-performance dashboards, and misstate evidence in regulated audits.
- `regulatory_basis`: Inferred from `recorded_at` against a policy-effective-date table so the upcast event references the rule set active at decision time.

This preserves a strict distinction between **inferrable historical context** (`model_version`, `regulatory_basis`) and **genuinely unknown values** (`confidence_score = null`), while keeping the event stream immutable.

---

### 6. The Marten Async Daemon Parallel
**Question:** Marten 7.0 introduced distributed projection execution across multiple nodes. Describe how you would achieve the same pattern in your Python implementation. What coordination primitive do you use, and what failure mode does it guard against?

**Answer:**
**Implementation Pattern:**
We implement a **Leader Election** pattern for projection shards. Since we are using PostgreSQL, we can use **Advisory Locks** (`pg_advisory_lock`) or a dedicated `projection_leader` table with a heartbeating mechanism.

**Coordination Primitive:**
The primary primitive is a **Checkpoint Table** combined with **Postgres Advisory Locks**.
- Each projection (or group of projections) is assigned an integer ID.
- An async worker tries to acquire `pg_try_advisory_lock(projection_id)`.
- If successful, that node becomes the "Leader" for that projection and starts tailing the event store from the last sequence in the `checkpoints` table.

**Failure Mode Guarded Against:**
This guards against the **"Double-Processing Leak"**. In a distributed system without coordination, two nodes might process the same event simultaneously, leading to duplicate applications of state (e.g., deducting $100 twice from a balance). The checkpoint/lock pattern ensures that exactly one node is advancing the read model at any given time, while providing high availability-if the leader node crashes, its lock is released, and another node automatically picks up the work.

## 2) Architecture Diagram (Event Store Schema, Aggregate Boundaries, Command Flow)

```mermaid
flowchart LR
    subgraph APP[Command/API Layer]
      C1[submit_application]
      C2[record_credit_analysis]
      H[CommandHandler]
    end

    subgraph AGG[Aggregate Boundaries]
      A1[LoanApplication\nstream: loan-{application_id}]
      A2[AgentSession\nstream: agent-{agent_id}-{session_id}]
      A3[ComplianceRecord\nstream: compliance-{application_id}]
      A4[AuditLedger\nstream: audit-{entity}-{id}]
    end

    subgraph DB[PostgreSQL Event Store]
      T1[(events)]
      T2[(event_streams)]
      T3[(projection_checkpoints)]
      T4[(outbox)]
    end

    C1 -->|command| H
    C2 -->|command| H
    H -->|replay -> validate -> decide| A1
    H -->|replay -> validate -> decide| A2
    H -->|replay -> validate -> decide| A3
    H -->|replay -> validate -> decide| A4

    A1 -->|append events| T1
    A2 -->|append events| T1
    A3 -->|append events| T1
    A4 -->|append events| T1

    T1 -->|stream version update| T2
    T1 -->|transactional write (same transaction)| T4
    T3 -->|daemon checkpoint read/write| T1
```

## 3) Progress Summary (What Is Working / In Progress)

### Working (Phase 1 + Phase 2)

- **Phase 1 Event Store Core**
  - `src/schema.sql` includes required tables, constraints, and read-path indexes.
  - `src/event_store.py` implements:
    - `append`, `load_stream`, `load_all`, `stream_version`, `archive_stream`, `get_stream_metadata`
    - optimistic concurrency with stream locking and version checks
    - transactional `events + outbox` writes
    - causal metadata support (`correlation_id`, `causation_id`)
- **Phase 2 Domain Logic**
  - `src/aggregates/loan_application.py`: replay-first aggregate load with state transitions and guards.
  - `src/aggregates/agent_session.py`: replay-first session aggregate with context and model-version guards.
  - `src/commands/handlers.py`: load -> validate -> determine -> append structure for:
    - `handle_submit_application`
    - `handle_credit_analysis_completed`
- **Validation/Tests**
  - `tests/test_concurrency.py` passes (double-decision OCC case).
  - `tests/test_rubric_core.py` passes.
  - Existing regressions like `tests/test_schema_and_generator.py` and narrative terminal-state gate continue to pass.
- **Packaging/Dependency Locking**
  - Root `pyproject.toml` now exists for the main repo.
  - `uv.lock` has been generated from the root project definition to satisfy locked dependency deliverable requirements.

### In Progress

- **Phase 3 (Projections + Async Daemon)**
  - Projection daemon checkpoint updates and projection-state writes are not yet consistently wrapped in one atomic boundary; this creates a crash window where replay can reprocess already-applied events.
- **Phase 4 (Upcasting + Integrity + Gas Town)**
  - Upcasting implementation now matches rubric semantics, but the immutability and chain-integrity test artifacts are not yet bundled as a single reproducible evidence package in this report.
- **Phase 5 (MCP Surface)**
  - MCP command/resource contracts exist, but lifecycle validation is not yet demonstrated end-to-end through MCP-only calls with captured request/response evidence.

## 4) Concurrency Test Results (Double-Decision Passing Log Output)

Commands executed:

```bash
.venv/bin/python -m pytest tests/test_concurrency.py::test_double_decision_exactly_one_wins -q
.venv/bin/python -m pytest tests/test_concurrency.py::test_double_decision_exactly_one_wins -q --tb=line
.venv/bin/python - <<'PY'
import asyncio
from src.event_store import InMemoryEventStore
from src.models.events import OptimisticConcurrencyError

def _event(event_type: str, seq: int) -> dict:
    return {"event_type": event_type, "event_version": 1, "payload": {"seq": seq}}

async def main():
    store = InMemoryEventStore()
    stream_id = "loan-APEX-DOUBLE-001"
    await store.append(stream_id, [_event("ApplicationSubmitted", 0)], expected_version=-1)
    await store.append(stream_id, [_event("CreditAnalysisRequested", 1)], expected_version=0)
    await store.append(stream_id, [_event("FraudScreeningCompleted", 2)], expected_version=1)
    await store.append(stream_id, [_event("ComplianceRulePassed", 3)], expected_version=2)

    async def try_append(agent_name: str):
        return await store.append(
            stream_id,
            [{"event_type": "CreditAnalysisCompleted", "event_version": 2,
              "payload": {"agent": agent_name, "recommended_limit_usd": 100000}}],
            expected_version=3,
        )

    results = await asyncio.gather(try_append("A"), try_append("B"), return_exceptions=True)
    success_positions = [r[0] for r in results if isinstance(r, list)]
    failures = [r for r in results if isinstance(r, OptimisticConcurrencyError)]
    stream = await store.load_stream(stream_id)
    print(f"Stream length: {len(stream)}")
    print(f"Winning task final position: {success_positions[0] if success_positions else None}")
    print(f"Losing task raised: {type(failures[0]).__name__ if failures else None}")

asyncio.run(main())
PY
```

Output:

```text
.                                                                        [100%]
1 passed, 48 warnings in 0.03s

Stream length: 5
Winning task final position: 4
Losing task raised: OptimisticConcurrencyError
```

Interpretation:
- Baseline stream had 4 events before the concurrent write race; final length 5 confirms exactly one additional event committed.
- Winner append returned stream position 4.
- Loser path surfaced `OptimisticConcurrencyError` and was not silently swallowed.

## 5) Known Gaps and Plan for Final Submission

### Known Gaps

- Projection daemon checkpointing still has a crash-window risk when checkpoint advancement is not committed atomically with projection-state mutation.
- Temporal compliance query and snapshot evidence are incomplete because `as_of` validation cases are not yet captured in dedicated reproducible test runs.
- Upcasting immutability and cryptographic integrity-chain evidence exist as partial components, but not yet as one stitched artifact set with raw-output logs and verification assertions.
- MCP lifecycle proof is missing a strict MCP-only happy-path/failure-path transcript that demonstrates command handling and resource reads without internal shortcuts.
- Final packaging gap is operational: architecture PNG export + consolidated terminal evidence blocks are not yet assembled into the final submission bundle.

### Plan (Toward Final Submission)

1. **Projection hardening**
   - finalize projection tables and daemon process loop with retry/lag instrumentation;
   - add load tests and assert lag thresholds.
2. **Temporal + snapshot compliance view**
   - implement snapshot strategy and add temporal `as_of` query tests.
3. **Upcasting and integrity evidence**
   - complete v1->v2 upcasting chain tests with raw DB immutability checks;
   - run and capture integrity-chain verification outputs.
4. **MCP finalization**
   - finalize command tools + query resources per Week 5 interface contract;
   - add lifecycle integration test through MCP endpoints only.
5. **Submission packaging**
   - produce final report PDF with architecture diagram, test logs, known limitations, and reflection.
