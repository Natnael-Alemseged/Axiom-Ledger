from __future__ import annotations

from datetime import datetime
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, Field


class DomainError(Exception):
    """Domain-level invariant violation."""


class OptimisticConcurrencyError(Exception):
    """Raised when expected stream version does not match persisted version."""

    def __init__(self, stream_id: str, expected: int, actual: int):
        self.stream_id = stream_id
        self.expected = expected
        self.actual = actual
        super().__init__(
            f"Optimistic concurrency conflict on {stream_id}: expected={expected}, actual={actual}"
        )


class BaseEvent(BaseModel):
    event_type: str
    event_version: int = 1
    payload: dict[str, Any]


class StoredEvent(BaseEvent):
    event_id: UUID
    stream_id: str
    stream_position: int
    global_position: int
    metadata: dict[str, Any] = Field(default_factory=dict)
    recorded_at: datetime


class StreamMetadata(BaseModel):
    stream_id: str
    aggregate_type: str
    current_version: int
    created_at: datetime
    archived_at: datetime | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


class ApplicationSubmitted(BaseModel):
    event_type: Literal["ApplicationSubmitted"] = "ApplicationSubmitted"
    event_version: Literal[1] = 1
    application_id: str
    applicant_id: str
    requested_amount_usd: float
    loan_purpose: str
    submission_channel: str
    submitted_at: datetime


class CreditAnalysisRequested(BaseModel):
    event_type: Literal["CreditAnalysisRequested"] = "CreditAnalysisRequested"
    event_version: Literal[1] = 1
    application_id: str
    assigned_agent_id: str
    requested_at: datetime
    priority: str


class CreditAnalysisCompleted(BaseModel):
    event_type: Literal["CreditAnalysisCompleted"] = "CreditAnalysisCompleted"
    event_version: Literal[2] = 2
    application_id: str
    agent_id: str
    session_id: str
    model_version: str
    confidence_score: float
    risk_tier: str
    recommended_limit_usd: float
    analysis_duration_ms: int
    input_data_hash: str


class FraudScreeningCompleted(BaseModel):
    event_type: Literal["FraudScreeningCompleted"] = "FraudScreeningCompleted"
    event_version: Literal[1] = 1
    application_id: str
    agent_id: str
    fraud_score: float
    anomaly_flags: list[str]
    screening_model_version: str
    input_data_hash: str


class ComplianceCheckRequested(BaseModel):
    event_type: Literal["ComplianceCheckRequested"] = "ComplianceCheckRequested"
    event_version: Literal[1] = 1
    application_id: str
    regulation_set_version: str
    checks_required: list[str]


class ComplianceRulePassed(BaseModel):
    event_type: Literal["ComplianceRulePassed"] = "ComplianceRulePassed"
    event_version: Literal[1] = 1
    application_id: str
    rule_id: str
    rule_version: str
    evaluation_timestamp: datetime
    evidence_hash: str


class ComplianceRuleFailed(BaseModel):
    event_type: Literal["ComplianceRuleFailed"] = "ComplianceRuleFailed"
    event_version: Literal[1] = 1
    application_id: str
    rule_id: str
    rule_version: str
    failure_reason: str
    remediation_required: bool


class DecisionGenerated(BaseModel):
    event_type: Literal["DecisionGenerated"] = "DecisionGenerated"
    event_version: Literal[2] = 2
    application_id: str
    orchestrator_agent_id: str
    recommendation: Literal["APPROVE", "DECLINE", "REFER"]
    confidence_score: float
    contributing_agent_sessions: list[str]
    decision_basis_summary: str
    model_versions: dict[str, str]


class HumanReviewCompleted(BaseModel):
    event_type: Literal["HumanReviewCompleted"] = "HumanReviewCompleted"
    event_version: Literal[1] = 1
    application_id: str
    reviewer_id: str
    override: bool
    final_decision: str
    override_reason: str | None = None


class ApplicationApproved(BaseModel):
    event_type: Literal["ApplicationApproved"] = "ApplicationApproved"
    event_version: Literal[1] = 1
    application_id: str
    approved_amount_usd: float
    interest_rate: float
    conditions: list[str]
    approved_by: str
    effective_date: datetime


class ApplicationDeclined(BaseModel):
    event_type: Literal["ApplicationDeclined"] = "ApplicationDeclined"
    event_version: Literal[1] = 1
    application_id: str
    decline_reasons: list[str]
    declined_by: str
    adverse_action_notice_required: bool


class AgentContextLoaded(BaseModel):
    event_type: Literal["AgentContextLoaded"] = "AgentContextLoaded"
    event_version: Literal[1] = 1
    agent_id: str
    session_id: str
    context_source: str
    event_replay_from_position: int
    context_token_count: int
    model_version: str


class AuditIntegrityCheckRun(BaseModel):
    event_type: Literal["AuditIntegrityCheckRun"] = "AuditIntegrityCheckRun"
    event_version: Literal[1] = 1
    entity_id: str
    check_timestamp: datetime
    events_verified_count: int
    integrity_hash: str
    previous_hash: str | None
