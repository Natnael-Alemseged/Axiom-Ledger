from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable


@dataclass
class AuditLedgerAggregate:
    entity_type: str
    entity_id: str
    current_version: int = -1
    last_integrity_hash: str | None = None
    events_in_chain: int = 0
    _handlers: dict[str, Callable[[dict], None]] = field(init=False, repr=False, default_factory=dict)

    def __post_init__(self) -> None:
        self._handlers = {
            "AuditIntegrityCheckRun": self._apply_audit_integrity_check_run,
        }

    @classmethod
    async def load(cls, store, entity_type: str, entity_id: str) -> "AuditLedgerAggregate":
        aggregate = cls(entity_type=entity_type, entity_id=entity_id)
        events = await store.load_stream(f"audit-{entity_type}-{entity_id}")
        for event in events:
            aggregate.apply(event)
        return aggregate

    def apply(self, event: dict) -> None:
        handler = self._handlers.get(event["event_type"])
        if handler is not None:
            handler(event)
        self.current_version = int(event["stream_position"])

    def get_last_hash(self) -> str | None:
        return self.last_integrity_hash

    def _apply_audit_integrity_check_run(self, event: dict) -> None:
        payload = event["payload"]
        self.last_integrity_hash = payload["integrity_hash"]
        self.events_in_chain = payload["events_verified_count"]
