"""Submission path compatibility module.

The challenge brief expects `src/event_store.py`. This module re-exports the
project implementation located in `ledger/event_store.py`.
"""

from ledger.event_store import (  # noqa: F401
    EventStore,
    InMemoryEventStore,
    OptimisticConcurrencyError,
    UpcasterRegistry,
)

