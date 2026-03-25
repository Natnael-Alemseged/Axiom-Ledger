from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable

from src.models.events import DomainError

MANDATORY_RULES = {"AML_CHECK", "KYC_CHECK", "SANCTIONS_CHECK"}


@dataclass
class ComplianceRecordAggregate:
    application_id: str
    current_version: int = -1
    regulation_set_version: str | None = None
    rules_evaluated: set[str] = field(default_factory=set)
    rules_passed: set[str] = field(default_factory=set)
    rules_failed: set[str] = field(default_factory=set)
    mandatory_checks_passed: bool = False
    has_hard_block: bool = False
    _handlers: dict[str, Callable[[dict], None]] = field(init=False, repr=False, default_factory=dict)

    def __post_init__(self) -> None:
        self._handlers = {
            "ComplianceCheckRequested": self._apply_compliance_check_requested,
            "ComplianceRulePassed": self._apply_compliance_rule_passed,
            "ComplianceRuleFailed": self._apply_compliance_rule_failed,
        }

    @classmethod
    async def load(cls, store, application_id: str) -> "ComplianceRecordAggregate":
        aggregate = cls(application_id=application_id)
        events = await store.load_stream(f"compliance-{application_id}")
        for event in events:
            aggregate.apply(event)
        return aggregate

    def apply(self, event: dict) -> None:
        handler = self._handlers.get(event["event_type"])
        if handler is not None:
            handler(event)
        self.current_version = int(event["stream_position"])

    def assert_all_mandatory_checks_complete(self) -> None:
        if not MANDATORY_RULES.issubset(self.rules_passed):
            missing = MANDATORY_RULES - self.rules_passed
            raise DomainError(
                f"Mandatory compliance checks not all passed. Missing or failed: {missing}"
            )

    def assert_no_hard_block(self) -> None:
        if self.has_hard_block:
            raise DomainError(
                f"Compliance hard block exists due to failed rules: {self.rules_failed}"
            )

    def assert_approval_preconditions(self) -> None:
        """
        Business rule: an application cannot be approved until mandatory checks pass
        and no hard-blocking compliance failures remain.
        """
        self.assert_all_mandatory_checks_complete()
        self.assert_no_hard_block()

    def _apply_compliance_check_requested(self, event: dict) -> None:
        payload = event["payload"]
        self.regulation_set_version = payload.get("regulation_set_version")

    def _apply_compliance_rule_passed(self, event: dict) -> None:
        payload = event["payload"]
        rule_id = payload["rule_id"]
        self.rules_evaluated.add(rule_id)
        self.rules_passed.add(rule_id)
        self.rules_failed.discard(rule_id)
        if MANDATORY_RULES.issubset(self.rules_passed):
            self.mandatory_checks_passed = True

    def _apply_compliance_rule_failed(self, event: dict) -> None:
        payload = event["payload"]
        rule_id = payload["rule_id"]
        self.rules_evaluated.add(rule_id)
        self.rules_failed.add(rule_id)
        self.rules_passed.discard(rule_id)
        if payload.get("remediation_required", False):
            self.has_hard_block = True
        if MANDATORY_RULES.issubset(self.rules_passed):
            self.mandatory_checks_passed = True
        else:
            self.mandatory_checks_passed = False
