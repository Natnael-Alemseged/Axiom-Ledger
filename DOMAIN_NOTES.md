# DOMAIN_NOTES.md — Graded Deliverable

### 1. EDA vs. ES Distinction
**Question:** A component uses callbacks (like LangChain traces) to capture event-like data. Is this Event-Driven Architecture (EDA) or Event Sourcing (ES)? If you redesigned it using The Ledger, what exactly would change in the architecture and what would you gain?

**Answer:** 
The use of callbacks for LangChain traces is an example of **Event-Driven Architecture (EDA)**. In this setup, "events" are side-effects—emitted after an action occurs primarily for observability or downstream triggers. The system's state is held in a traditional database (or just in memory), and these traces are merely logs of what happened. If the traces are lost, the system's current state remains intact.

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
def upcast_credit_decision_v1_to_v2(event_data: dict) -> dict:
    """Transforms a 2024 CreditDecisionMade into the 2026 schema."""
    return {
        **event_data,
        "model_version": event_data.get("model_version", "legacy-2024.1"),
        "confidence_score": event_data.get("confidence_score", 1.0), # Assume 1.0 for historical finalized decisions
        "regulatory_basis": event_data.get("regulatory_basis", "PRE_2026_POLICY_MANUAL"),
        "schema_version": 2
    }
```

**Inference Strategy:**
For historical events, we use a **"Conservative Mapping"** strategy:
- `model_version`: Assign a static "legacy" identifier that refers to the documentation/weights of the period the event was recorded.
- `confidence_score`: We "back-fill" with a value of `1.0` if the decision was finalized, or `null` if the data cannot be recovered, ensuring the downstream projection logic knows this was a "hard" decision not based on modern probabilistic models.
- `regulatory_basis`: We map it to the static policy document ID that was active in 2024. This maintains the audit trail's integrity by explicitly stating these decisions were made under an older regulatory framework.

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
This guards against the **"Double-Processing Leak"**. In a distributed system without coordination, two nodes might process the same event simultaneously, leading to duplicate applications of state (e.g., deducting $100 twice from a balance). The checkpoint/lock pattern ensures that exactly one node is advancing the read model at any given time, while providing high availability—if the leader node crashes, its lock is released, and another node automatically picks up the work.
