## Submission Layout Notes

This folder exists to align the repository with the Week 5 deliverable paths.

- `schema.sql`: canonical PostgreSQL schema for the event store contract
- `event_store.py`: canonical async EventStore implementation
- `models/events.py`: typed event, envelope, stream metadata, and exception models
- `aggregates/`: replay-first domain aggregates
- `commands/handlers.py`: load-validate-determine-append command handlers

The runtime application still imports from `ledger/` paths where needed, with
`ledger/event_store.py` bridging to `src/event_store.py` for compatibility.

