## Submission Layout Notes

This folder exists to align the repository with the Week 5 deliverable paths.

- `schema.sql`: canonical PostgreSQL schema for the event store contract
- `event_store.py`: compatibility export for the implementation in `ledger/event_store.py`

Current project modules remain under `ledger/`, `datagen/`, and `tests/`.
For interim submission checks, reviewers can use `src/schema.sql` as the
authoritative migration file.

