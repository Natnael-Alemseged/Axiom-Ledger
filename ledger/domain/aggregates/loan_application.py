"""
ledger/domain/aggregates/loan_application.py
=============================================
COMPLETION STATUS: STUB — implement apply() for each event, enforce business rules.

The aggregate replays its event stream to rebuild state.
Command handlers validate against current state before appending events.

BUSINESS RULES TO ENFORCE:
  1. State machine: only valid transitions allowed
  2. DocumentFactsExtracted must exist before CreditAnalysisCompleted
  3. All 6 compliance rules must complete before DecisionGenerated (unless hard block)
  4. confidence < 0.60 → recommendation must be REFER (enforced here, not in LLM)
  5. Compliance BLOCKED → only DECLINE allowed, not APPROVE or REFER
  6. Causal chain: every agent event must reference a triggering event_id

See: Section 4 of challenge document for full rule specifications.
"""
from __future__ import annotations
from dataclasses import dataclass, field
from enum import Enum

class ApplicationState(str, Enum):
    NEW = "NEW"; SUBMITTED = "SUBMITTED"; DOCUMENTS_PENDING = "DOCUMENTS_PENDING"
    DOCUMENTS_UPLOADED = "DOCUMENTS_UPLOADED"; DOCUMENTS_PROCESSED = "DOCUMENTS_PROCESSED"
    CREDIT_ANALYSIS_REQUESTED = "CREDIT_ANALYSIS_REQUESTED"; CREDIT_ANALYSIS_COMPLETE = "CREDIT_ANALYSIS_COMPLETE"
    FRAUD_SCREENING_REQUESTED = "FRAUD_SCREENING_REQUESTED"; FRAUD_SCREENING_COMPLETE = "FRAUD_SCREENING_COMPLETE"
    COMPLIANCE_CHECK_REQUESTED = "COMPLIANCE_CHECK_REQUESTED"; COMPLIANCE_CHECK_COMPLETE = "COMPLIANCE_CHECK_COMPLETE"
    PENDING_DECISION = "PENDING_DECISION"; PENDING_HUMAN_REVIEW = "PENDING_HUMAN_REVIEW"
    APPROVED = "APPROVED"; DECLINED = "DECLINED"; DECLINED_COMPLIANCE = "DECLINED_COMPLIANCE"
    REFERRED = "REFERRED"

VALID_TRANSITIONS = {
    ApplicationState.NEW: [ApplicationState.SUBMITTED],
    ApplicationState.SUBMITTED: [ApplicationState.DOCUMENTS_PENDING],
    ApplicationState.DOCUMENTS_PENDING: [ApplicationState.DOCUMENTS_UPLOADED],
    ApplicationState.DOCUMENTS_UPLOADED: [ApplicationState.DOCUMENTS_PROCESSED],
    ApplicationState.DOCUMENTS_PROCESSED: [ApplicationState.CREDIT_ANALYSIS_REQUESTED],
    ApplicationState.CREDIT_ANALYSIS_REQUESTED: [ApplicationState.CREDIT_ANALYSIS_COMPLETE],
    ApplicationState.CREDIT_ANALYSIS_COMPLETE: [ApplicationState.FRAUD_SCREENING_REQUESTED],
    ApplicationState.FRAUD_SCREENING_REQUESTED: [ApplicationState.FRAUD_SCREENING_COMPLETE],
    ApplicationState.FRAUD_SCREENING_COMPLETE: [ApplicationState.COMPLIANCE_CHECK_REQUESTED],
    ApplicationState.COMPLIANCE_CHECK_REQUESTED: [ApplicationState.COMPLIANCE_CHECK_COMPLETE],
    ApplicationState.COMPLIANCE_CHECK_COMPLETE: [ApplicationState.PENDING_DECISION, ApplicationState.DECLINED_COMPLIANCE],
    ApplicationState.PENDING_DECISION: [ApplicationState.APPROVED, ApplicationState.DECLINED, ApplicationState.PENDING_HUMAN_REVIEW],
    ApplicationState.PENDING_HUMAN_REVIEW: [ApplicationState.APPROVED, ApplicationState.DECLINED],
}

@dataclass
class LoanApplicationAggregate:
    application_id: str
    state: ApplicationState = ApplicationState.NEW
    applicant_id: str | None = None
    requested_amount_usd: float | None = None
    loan_purpose: str | None = None
    version: int = 0
    events: list[dict] = field(default_factory=list)
    compliance_overall_verdict: str | None = None
    has_hard_block: bool = False

    @classmethod
    async def load(cls, store, application_id: str) -> "LoanApplicationAggregate":
        """Load and replay event stream to rebuild aggregate state."""
        agg = cls(application_id=application_id)
        stream_events = await store.load_stream(f"loan-{application_id}")
        for event in stream_events:
            agg.apply(event)
        return agg

    def apply(self, event: dict) -> None:
        """Apply one event to update aggregate state."""
        et = event.get("event_type")
        p = event.get("payload", {})
        self.version += 1
        self.events.append(event)

        if et == "ApplicationSubmitted":
            self.state = ApplicationState.SUBMITTED
            self.applicant_id = p.get("applicant_id")
            self.requested_amount_usd = p.get("requested_amount_usd")
            self.loan_purpose = p.get("loan_purpose")
        elif et == "DocumentUploadRequested":
            self.state = ApplicationState.DOCUMENTS_PENDING
        elif et == "DocumentUploaded":
            self.state = ApplicationState.DOCUMENTS_UPLOADED
        elif et == "CreditAnalysisRequested":
            self.state = ApplicationState.CREDIT_ANALYSIS_REQUESTED
        elif et == "FraudScreeningRequested":
            self.state = ApplicationState.FRAUD_SCREENING_REQUESTED
        elif et == "ComplianceCheckRequested":
            self.state = ApplicationState.COMPLIANCE_CHECK_REQUESTED
        elif et == "ComplianceCheckCompleted":
            self.state = ApplicationState.COMPLIANCE_CHECK_COMPLETE
            self.compliance_overall_verdict = str(p.get("overall_verdict", ""))
            self.has_hard_block = bool(p.get("has_hard_block", False))
        elif et == "DecisionRequested":
            self.state = ApplicationState.PENDING_DECISION
        elif et == "DecisionGenerated":
            recommendation = str(p.get("recommendation", ""))
            confidence = float(p.get("confidence", 0.0))
            self.assert_valid_orchestrator_decision(
                recommendation=recommendation,
                confidence=confidence,
            )
            if recommendation == "APPROVE":
                self.state = ApplicationState.APPROVED
            elif recommendation == "DECLINE":
                self.state = ApplicationState.DECLINED
            elif recommendation == "REFER":
                self.state = ApplicationState.REFERRED
        elif et == "HumanReviewRequested":
            self.state = ApplicationState.PENDING_HUMAN_REVIEW
        elif et == "ApplicationApproved":
            self.state = ApplicationState.APPROVED
        elif et == "ApplicationDeclined":
            reasons = [str(r) for r in (p.get("decline_reasons") or [])]
            if any("compliance" in r.lower() for r in reasons):
                self.state = ApplicationState.DECLINED_COMPLIANCE
            else:
                self.state = ApplicationState.DECLINED

    def assert_valid_transition(self, target: ApplicationState) -> None:
        allowed = VALID_TRANSITIONS.get(self.state, [])
        if target not in allowed:
            raise ValueError(f"Invalid transition {self.state} → {target}. Allowed: {allowed}")

    def assert_valid_orchestrator_decision(self, recommendation: str, confidence: float) -> None:
        """
        Enforce hard orchestrator decision constraints at aggregate level.

        Required policy:
        - confidence < 0.60 must never be APPROVE.
        """
        rec = (recommendation or "").upper()
        if confidence < 0.60 and rec == "APPROVE":
            raise ValueError(
                "Invalid DecisionGenerated: recommendation=APPROVE is not allowed "
                "when confidence < 0.60."
            )
        if self.compliance_overall_verdict == "BLOCKED" and rec != "DECLINE":
            raise ValueError(
                "Invalid DecisionGenerated: compliance BLOCKED requires recommendation=DECLINE."
            )
