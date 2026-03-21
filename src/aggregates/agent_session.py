from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable

from src.models.events import DomainError


@dataclass
class AgentSessionAggregate:
    agent_id: str
    session_id: str
    current_version: int = -1
    context_declared: bool = False
    model_version: str | None = None
    _handlers: dict[str, Callable[[dict], None]] = field(init=False, repr=False, default_factory=dict)

    def __post_init__(self) -> None:
        self._handlers = {
            "AgentContextLoaded": self._apply_agent_context_loaded,
        }

    @classmethod
    async def load(cls, store, agent_id: str, session_id: str) -> "AgentSessionAggregate":
        aggregate = cls(agent_id=agent_id, session_id=session_id)
        events = await store.load_stream(f"agent-{agent_id}-{session_id}")
        for event in events:
            aggregate.apply(event)
        return aggregate

    def apply(self, event: dict) -> None:
        handler = self._handlers.get(event["event_type"])
        if handler is not None:
            handler(event)
        self.current_version = int(event["stream_position"])

    def assert_context_loaded(self) -> None:
        if not self.context_declared:
            raise DomainError("Agent context must be loaded before producing outputs")

    def assert_model_version_current(self, command_model_version: str) -> None:
        self.assert_context_loaded()
        if self.model_version != command_model_version:
            raise DomainError(
                f"Model version mismatch (session={self.model_version}, command={command_model_version})"
            )

    def _apply_agent_context_loaded(self, event: dict) -> None:
        payload = event["payload"]
        self.context_declared = True
        self.model_version = payload["model_version"]

