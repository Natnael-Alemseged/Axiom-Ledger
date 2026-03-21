# Week 5 Rubric Evidence Map

This document maps each rubric criterion to implementation artifacts in this repository.

## 1) Database Schema Design

- Required SQL file: `src/schema.sql`
- Required tables:
  - `events`
  - `event_streams`
  - `projection_checkpoints`
  - `outbox`
- Structural correctness evidence:
  - `events.global_position` uses identity generation
  - unique `(stream_id, stream_position)` constraint
  - `recorded_at` uses `clock_timestamp()`
  - `outbox.event_id` foreign key to `events.event_id`
  - `event_streams.archived_at` nullable archival column
- Index coverage:
  - stream-ordered reads: `idx_events_stream_id`
  - global feed reads: `idx_events_global_pos`
  - event-type filtering: `idx_events_type`
  - time-range reads: `idx_events_recorded`
  - outbox dispatch hot path: `idx_outbox_unpublished`

## 2) EventStore Implementation

- Runtime implementation: `src/event_store.py`
- Backward-compatible import path: `ledger/event_store.py`
- Required methods:
  - `append`
  - `load_stream`
  - `load_all` (async generator)
  - `stream_version`
  - `archive_stream`
  - `get_stream_metadata`
- Transactional and OCC evidence:
  - `append` writes `events` and `outbox` in the same DB transaction
  - OCC enforced with `SELECT ... FOR UPDATE` against `event_streams`
  - raises typed `OptimisticConcurrencyError(stream_id, expected, actual)`
  - stores causal metadata (`correlation_id`, `causation_id`) in event metadata
- Concurrency test evidence:
  - `tests/test_concurrency.py`

## 3) Domain Event and Exception Models

- Typed models: `src/models/events.py`
- Envelope separation:
  - `BaseEvent` (domain-owned fields)
  - `StoredEvent` (store-assigned envelope)
- Stream state model:
  - `StreamMetadata`
- Exception model:
  - `OptimisticConcurrencyError` with structured fields
  - `DomainError` as separate domain exception
- Catalogue coverage:
  - includes the challenge event catalogue models (13 types)

## 4) Aggregate Design and State Reconstruction

- Loan aggregate: `src/aggregates/loan_application.py`
- Agent session aggregate: `src/aggregates/agent_session.py`
- Replay-first load:
  - both aggregates expose `load()` that replays stream events
- Per-event dispatch:
  - both use handler map (`event_type` -> `_apply_*`)
- Domain guards:
  - invalid transition and invariant checks in aggregate methods
  - agent context-loaded and model-version guards enforced on aggregate

## 5) Command Handler Pattern

- Handlers: `src/commands/handlers.py`
- Required structure evidence:
  - load aggregate(s)
  - call aggregate guard methods
  - construct event(s) without DB queries
  - append using tracked aggregate version
- Causal metadata threading:
  - handlers accept and pass `correlation_id` and `causation_id`
- Multi-aggregate loading:
  - `handle_credit_analysis_completed` loads both loan and agent session aggregates

## Verification Tests

- Concurrency gate: `tests/test_concurrency.py`
- Handler/aggregate behavior checks: `tests/test_rubric_core.py`
- Existing phase tests: `tests/phase1/test_event_store.py`
