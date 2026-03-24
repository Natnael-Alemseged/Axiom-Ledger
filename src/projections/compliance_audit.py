from __future__ import annotations
from datetime import datetime
from typing import Any

class ComplianceAuditViewProjection:
    name = "compliance_audit"

    def __init__(self):
        # application_id -> list of compliance events (ordered, with timestamps)
        self._events: dict[str, list[dict]] = {}
        # application_id -> current summary
        self._current: dict[str, dict] = {}
        self._lag_ms: float = 0.0
        self._last_processed_at: datetime | None = None

    def get_current_compliance(self, application_id: str) -> dict | None:
        return self._current.get(application_id)

    def get_compliance_at(self, application_id: str, timestamp: datetime) -> dict | None:
        """Temporal query: reconstruct compliance state at a specific point in time."""
        events = self._events.get(application_id, [])
        # Filter events up to timestamp
        relevant = [e for e in events if e.get("recorded_at") and _to_datetime(e["recorded_at"]) <= timestamp]
        if not relevant:
            return None
        # Rebuild state from filtered events
        state = _rebuild_compliance_state(application_id, relevant)
        return state

    def get_projection_lag(self) -> float:
        return self._lag_ms

    async def rebuild_from_scratch(self, store) -> None:
        self._events.clear()
        self._current.clear()
        async for event in store.load_all(
            from_global_position=0,
            event_types=COMPLIANCE_EVENT_TYPES,
        ):
            await self.handle(event)

    async def handle(self, event: dict) -> None:
        et = event["event_type"]
        if et not in COMPLIANCE_EVENT_TYPES:
            return
        p = event["payload"]
        app_id = p.get("application_id")
        if not app_id:
            return

        # Store event history for temporal queries
        if app_id not in self._events:
            self._events[app_id] = []
        self._events[app_id].append(event)

        # Update current state
        if app_id not in self._current:
            self._current[app_id] = {
                "application_id": app_id,
                "regulation_set_version": None,
                "rules_passed": [],
                "rules_failed": [],
                "rules_noted": [],
                "overall_verdict": None,
                "has_hard_block": False,
                "checks_required": [],
                "completed_at": None,
            }
        state = self._current[app_id]

        if et == "ComplianceCheckRequested":
            state["regulation_set_version"] = p.get("regulation_set_version")
            state["checks_required"] = p.get("checks_required") or p.get("rules_to_evaluate", [])
        elif et == "ComplianceRulePassed":
            rule_id = p.get("rule_id")
            if rule_id and rule_id not in state["rules_passed"]:
                state["rules_passed"].append(rule_id)
        elif et == "ComplianceRuleFailed":
            rule_id = p.get("rule_id")
            if rule_id and rule_id not in state["rules_failed"]:
                state["rules_failed"].append(rule_id)
            if p.get("remediation_required") or p.get("is_hard_block"):
                state["has_hard_block"] = True
        elif et == "ComplianceCheckCompleted":
            state["overall_verdict"] = p.get("overall_verdict")
            state["completed_at"] = event.get("recorded_at")


COMPLIANCE_EVENT_TYPES = [
    "ComplianceCheckRequested",
    "ComplianceRulePassed",
    "ComplianceRuleFailed",
    "ComplianceRuleNoted",
    "ComplianceCheckCompleted",
    "ComplianceCheckInitiated",
]


def _to_datetime(val) -> datetime:
    if isinstance(val, datetime):
        return val
    if isinstance(val, str):
        from datetime import timezone
        dt = datetime.fromisoformat(val.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    return val


def _rebuild_compliance_state(application_id: str, events: list[dict]) -> dict:
    state = {
        "application_id": application_id,
        "regulation_set_version": None,
        "rules_passed": [],
        "rules_failed": [],
        "rules_noted": [],
        "overall_verdict": None,
        "has_hard_block": False,
        "checks_required": [],
        "completed_at": None,
    }
    for event in events:
        et = event["event_type"]
        p = event["payload"]
        if et == "ComplianceCheckRequested":
            state["regulation_set_version"] = p.get("regulation_set_version")
            state["checks_required"] = p.get("checks_required") or p.get("rules_to_evaluate", [])
        elif et == "ComplianceRulePassed":
            rule_id = p.get("rule_id")
            if rule_id and rule_id not in state["rules_passed"]:
                state["rules_passed"].append(rule_id)
        elif et == "ComplianceRuleFailed":
            rule_id = p.get("rule_id")
            if rule_id and rule_id not in state["rules_failed"]:
                state["rules_failed"].append(rule_id)
            if p.get("remediation_required") or p.get("is_hard_block"):
                state["has_hard_block"] = True
        elif et == "ComplianceCheckCompleted":
            state["overall_verdict"] = p.get("overall_verdict")
            state["completed_at"] = event.get("recorded_at")
    return state
