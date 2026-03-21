"""Backward-compatible access to the Week 5 EventStore implementation."""

from src.event_store import EventStore, InMemoryEventStore, UpcasterRegistry
from src.models.events import OptimisticConcurrencyError

__all__ = [
    "EventStore",
    "InMemoryEventStore",
    "UpcasterRegistry",
    "OptimisticConcurrencyError",
]
