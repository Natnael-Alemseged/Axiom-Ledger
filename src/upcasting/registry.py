from __future__ import annotations
from typing import Callable

class UpcasterRegistry:
    """
    Centralized registry for event version upcasters.
    Applied transparently on event load — never modifies stored events.
    """

    def __init__(self):
        self._upcasters: dict[tuple[str, int], Callable[[dict], dict]] = {}

    def register(self, event_type: str, from_version: int):
        """Decorator. Registers fn as upcaster from event_type@from_version."""
        def decorator(fn: Callable[[dict], dict]) -> Callable[[dict], dict]:
            self._upcasters[(event_type, from_version)] = fn
            return fn
        return decorator

    def upcast(self, event: dict) -> dict:
        """
        Apply all registered upcasters for this event in version order.
        Returns a NEW dict — never mutates the input.
        """
        current = dict(event)
        v = int(current.get("event_version", 1))
        et = current.get("event_type", "")
        while (et, v) in self._upcasters:
            new_payload = self._upcasters[(et, v)](dict(current.get("payload", {})))
            current = dict(current)
            current["payload"] = new_payload
            current["event_version"] = v + 1
            v += 1
        return current
