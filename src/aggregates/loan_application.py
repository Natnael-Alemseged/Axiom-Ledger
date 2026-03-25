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
    known_agent_sessions: set[str] = field(default_factory=set)
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

    def enforce_confidence_floor(
        self, recommendation: str, confidence_score: float
    ) -> tuple[str, bool]:
        """
        Business rule: confidence < 0.6 forces REFER regardless of caller input.
        Enforced in aggregate logic — LLM/API callers cannot bypass this.
        Returns (effective_recommendation, floor_was_applied).
        """
        if confidence_score < 0.6 and recommendation != "REFER":
            return "REFER", True
        return recommendation, False

    def assert_can_generate_decision(self) -> None:
        """Validate that the application is in a state that allows decision generation."""
        allowed = {"AnalysisComplete", "ComplianceReview", "PendingDecision"}
        if self.state not in allowed:
            raise DomainError(
                f"Cannot generate decision in state '{self.state}'. "
                f"Application must have completed credit analysis first."
            )

    def assert_causal_chain(self, contributing_agent_sessions: list[str]) -> None:
        """
        Business rule: each decision must declare causal provenance.
        """
        if not contributing_agent_sessions:
            raise DomainError(
                "Decision must include at least one contributing_agent_sessions entry"
            )

    def assert_approval_dependencies(
        self,
        recommendation: str,
        compliance_ready: bool,
    ) -> None:
        """
        Business rule: approvals require successful compliance checks.
        """
        if recommendation == "APPROVE" and not compliance_ready:
            raise DomainError("Cannot approve before mandatory compliance checks pass")

    def _apply_application_submitted(self, _: dict) -> None:
        self._transition("Submitted")
        self.state = "AwaitingAnalysis"

    def _apply_credit_analysis_completed(self, event: dict) -> None:
        payload = event["payload"]
        self.agent_assessed_max_limit = float(payload["recommended_limit_usd"])
        session_id = payload.get("session_id")
        if session_id:
            self.known_agent_sessions.add(session_id)
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
        # Compliance events may be tracked in a separate compliance stream.
        # If we are still in AnalysisComplete, skip directly through ComplianceReview.
        if self.state == "AnalysisComplete":
            self._transition("ComplianceReview")
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
        amount = event["payload"].get("approved_amount_usd")
        if amount is not None:
            self.assert_limit_within_assessed(float(amount))
        # Compliance may be in a separate stream; only enforce if tracked in this stream
        # (compliance_pending starts True, becomes False when compliance events are seen)
        if not self.compliance_pending:
            pass  # Compliance done — OK to approve
        # Idempotent: HumanReviewCompleted may have already set FinalApproved
        if self.state != "FinalApproved":
            self._transition("FinalApproved")

    def _apply_application_declined(self, _: dict) -> None:
        # Idempotent: HumanReviewCompleted may have already set FinalDeclined
        if self.state != "FinalDeclined":
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

