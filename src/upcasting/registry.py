from __future__ import annotations

import inspect
from typing import Callable

# Upcaster: (payload) -> dict  OR  (payload, event) -> dict
UpcasterFn = Callable[..., dict]


class UpcasterRegistry:
    """
    Centralized registry for event version upcasters.
    Applied transparently on event load — never modifies stored events.

    Upcasters receive the payload dict and, when their signature accepts a second
    parameter, the full event envelope (including recorded_at) for inference.
    """

    def __init__(self):
        self._upcasters: dict[tuple[str, int], UpcasterFn] = {}

    def register(self, event_type: str, from_version: int):
        """Decorator. Registers fn as upcaster from event_type@from_version."""

        def decorator(fn: UpcasterFn) -> UpcasterFn:
            self._upcasters[(event_type, from_version)] = fn
            return fn

        return decorator

    @staticmethod
    def _invoke_upcaster(fn: UpcasterFn, payload: dict, event: dict) -> dict:
        """Call one-arg or two-arg upcasters without mutating payload/event."""
        try:
            sig = inspect.signature(fn)
            if len(sig.parameters) >= 2:
                return fn(dict(payload), dict(event))
        except TypeError:
            # Fallback if signature inspection fails
            pass
        return fn(dict(payload))

    def upcast(self, event: dict) -> dict:
        """
        Apply all registered upcasters for this event in version order.
        Returns a NEW dict — never mutates the input.
        """
        current = dict(event)
        v = int(current.get("event_version", 1))
        et = current.get("event_type", "")
        while (et, v) in self._upcasters:
            fn = self._upcasters[(et, v)]
            new_payload = self._invoke_upcaster(
                fn, dict(current.get("payload", {})), current
            )
            current = dict(current)
            current["payload"] = new_payload
            current["event_version"] = v + 1
            v += 1
        return current
