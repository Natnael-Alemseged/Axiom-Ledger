from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Callable

from src.models.events import DomainError


@dataclass
class LoanApplicationAggregate:
    application_id: str
    state: str = "Submitted"
    current_version: int = -1
    compliance_pending: bool = True
    agent_assessed_max_limit: float | None = None
    approved_limit: float | None = None
    _handlers: dict[str, Callable[[dict], None]] = field(init=False, repr=False, default_factory=dict)

    def __post_init__(self) -> None:
        self._handlers = {
            "ApplicationSubmitted": self._apply_application_submitted,
            "CreditAnalysisCompleted": self._apply_credit_analysis_completed,
            "ComplianceCheckRequested": self._apply_compliance_check_requested,
            "ComplianceRulePassed": self._apply_compliance_rule_passed,
            "ComplianceRuleFailed": self._apply_compliance_rule_failed,
            "DecisionGenerated": self._apply_decision_generated,
            "HumanReviewCompleted": self._apply_human_review_completed,
            "ApplicationApproved": self._apply_application_approved,
            "ApplicationDeclined": self._apply_application_declined,
        }

    @classmethod
    async def load(cls, store, application_id: str) -> "LoanApplicationAggregate":
        aggregate = cls(application_id=application_id)
        events = await store.load_stream(f"loan-{application_id}")
        for event in events:
            aggregate.apply(event)
        return aggregate

    def apply(self, event: dict) -> None:
        event_type = event["event_type"]
        handler = self._handlers.get(event_type)
        if handler is not None:
            handler(event)
        self.current_version = int(event["stream_position"])

    def assert_awaiting_credit_analysis(self) -> None:
        if self.state != "AwaitingAnalysis":
            raise DomainError(f"Application is not awaiting analysis (state={self.state})")

    def assert_can_submit(self) -> None:
        if self.current_version != -1:
            raise DomainError("Application already exists")

    def assert_limit_within_assessed(self, approved_amount_usd: float) -> None:
        if self.agent_assessed_max_limit is None:
            return
        if approved_amount_usd > self.agent_assessed_max_limit:
            raise DomainError("Approved limit exceeds agent-assessed maximum")

    def _apply_application_submitted(self, _: dict) -> None:
        self._transition("Submitted")
        self.state = "AwaitingAnalysis"

    def _apply_credit_analysis_completed(self, event: dict) -> None:
        payload = event["payload"]
        self.agent_assessed_max_limit = float(payload["recommended_limit_usd"])
        self._transition("AnalysisComplete")

    def _apply_compliance_check_requested(self, _: dict) -> None:
        self._transition("ComplianceReview")
        self.compliance_pending = True

    def _apply_compliance_rule_passed(self, _: dict) -> None:
        self.compliance_pending = False

    def _apply_compliance_rule_failed(self, _: dict) -> None:
        self.compliance_pending = False

    def _apply_decision_generated(self, event: dict) -> None:
        payload = event["payload"]
        recommendation = payload["recommendation"]
        if self.state == "ComplianceReview":
            self._transition("PendingDecision")
        if recommendation == "APPROVE":
            self._transition("ApprovedPendingHuman")
        elif recommendation in ("DECLINE", "REFER"):
            self._transition("DeclinedPendingHuman")

    def _apply_human_review_completed(self, event: dict) -> None:
        final_decision = event["payload"]["final_decision"]
        if final_decision == "APPROVE":
            self._transition("FinalApproved")
        else:
            self._transition("FinalDeclined")

    def _apply_application_approved(self, event: dict) -> None:
        self.assert_limit_within_assessed(float(event["payload"]["approved_amount_usd"]))
        if self.compliance_pending:
            raise DomainError("Cannot approve while compliance check is pending")
        self._transition("FinalApproved")

    def _apply_application_declined(self, _: dict) -> None:
        self._transition("FinalDeclined")

    def _transition(self, new_state: str) -> None:
        allowed = {
            "Submitted": {"AwaitingAnalysis"},
            "AwaitingAnalysis": {"AnalysisComplete"},
            "AnalysisComplete": {"ComplianceReview"},
            "ComplianceReview": {"PendingDecision", "DeclinedPendingHuman"},
            "PendingDecision": {"ApprovedPendingHuman", "DeclinedPendingHuman"},
            "ApprovedPendingHuman": {"FinalApproved", "FinalDeclined"},
            "DeclinedPendingHuman": {"FinalApproved", "FinalDeclined"},
            "FinalApproved": set(),
            "FinalDeclined": set(),
        }
        if new_state == "Submitted":
            return
        if self.state not in allowed:
            raise DomainError(f"Unknown state: {self.state}")
        if new_state not in allowed[self.state]:
            if not (self.state == "ComplianceReview" and new_state in {"PendingDecision", "DeclinedPendingHuman"}):
                raise DomainError(f"Invalid transition {self.state} -> {new_state}")
        self.state = new_state

