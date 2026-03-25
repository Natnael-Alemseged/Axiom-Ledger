from __future__ import annotations

import pytest

from src.aggregates.agent_session import AgentSessionAggregate
from src.aggregates.compliance_record import ComplianceRecordAggregate
from src.aggregates.loan_application import LoanApplicationAggregate
from src.models.events import DomainError


def test_causal_chain_requires_contributing_sessions():
    app = LoanApplicationAggregate(application_id="rule-001", state="AnalysisComplete")
    with pytest.raises(DomainError):
        app.assert_causal_chain([])


def test_approval_dependency_requires_compliance_ready():
    app = LoanApplicationAggregate(application_id="rule-002", state="PendingDecision")
    with pytest.raises(DomainError):
        app.assert_approval_dependencies(recommendation="APPROVE", compliance_ready=False)


def test_compliance_record_requires_mandatory_checks_for_approval():
    comp = ComplianceRecordAggregate(application_id="rule-003")
    with pytest.raises(DomainError):
        comp.assert_approval_preconditions()


def test_agent_model_version_locking():
    agent = AgentSessionAggregate(agent_id="agent-01", session_id="sess-01")
    agent.context_declared = True
    agent.model_version = "credit-v3.1"
    with pytest.raises(DomainError):
        agent.assert_model_version_current("credit-v3.0")
