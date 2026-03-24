"""
src/mcp/tools.py — MCP Tools (Command Side)
============================================
8 MCP tools implementing the CQRS command side.

Each tool:
- Accepts structured input (validated by FastMCP via type annotations)
- Returns structured success OR typed error object
- Documents preconditions in the tool description for LLM consumers

Structured error format:
  {"error_type": "...", "message": "...", "suggested_action": "..."}
"""
from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from typing import Any

from fastmcp import FastMCP


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------

def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _error(error_type: str, message: str, suggested_action: str = "", **kwargs) -> dict:
    return {
        "success": False,
        "error_type": error_type,
        "message": message,
        "suggested_action": suggested_action,
        **kwargs,
    }


def _ok(**kwargs) -> dict:
    return {"success": True, **kwargs}


# ---------------------------------------------------------------------------
# Tool registration helper — called from server.py with the store injected
# ---------------------------------------------------------------------------

def register_tools(mcp: FastMCP, store_factory) -> None:
    """
    Register all 8 command-side tools onto the MCP server.
    store_factory() returns a connected EventStore instance.
    """

    # ------------------------------------------------------------------
    # TOOL 1: submit_application
    # ------------------------------------------------------------------
    @mcp.tool(
        description=(
            "Submit a new commercial loan application to The Ledger. "
            "Creates an ApplicationSubmitted event and opens the loan-{application_id} stream. "
            "Returns the stream_id and initial version. "
            "ERROR: DuplicateApplicationError if application_id already exists."
        )
    )
    async def submit_application(
        application_id: str,
        applicant_id: str,
        requested_amount_usd: float,
        loan_purpose: str,
        submission_channel: str,
        contact_email: str = "",
        contact_name: str = "",
        loan_term_months: int = 60,
    ) -> dict:
        store = store_factory()
        from src.aggregates.loan_application import LoanApplicationAggregate
        from src.models.events import DomainError, OptimisticConcurrencyError

        try:
            app = await LoanApplicationAggregate.load(store, application_id)
            app.assert_can_submit()

            event = {
                "event_type": "ApplicationSubmitted",
                "event_version": 1,
                "payload": {
                    "application_id": application_id,
                    "applicant_id": applicant_id,
                    "requested_amount_usd": requested_amount_usd,
                    "loan_purpose": loan_purpose,
                    "submission_channel": submission_channel,
                    "contact_email": contact_email,
                    "contact_name": contact_name,
                    "loan_term_months": loan_term_months,
                    "submitted_at": _now(),
                },
            }
            await store.append(
                stream_id=f"loan-{application_id}",
                events=[event],
                expected_version=app.current_version,
            )
            return _ok(stream_id=f"loan-{application_id}", initial_version=0)
        except DomainError as e:
            return _error("DuplicateApplicationError", str(e), "Use a unique application_id")
        except OptimisticConcurrencyError as e:
            return _error(
                "OptimisticConcurrencyError", str(e),
                "reload_stream_and_retry",
                stream_id=f"loan-{application_id}",
            )
        except Exception as e:
            return _error("InternalError", str(e), "Check server logs")

    # ------------------------------------------------------------------
    # TOOL 2: start_agent_session
    # ------------------------------------------------------------------
    @mcp.tool(
        description=(
            "Start an agent session — REQUIRED before any agent decision tools. "
            "Appends AgentContextLoaded (Gas Town anchor) to the agent session stream. "
            "PRECONDITION: Must be called before record_credit_analysis, record_fraud_screening, "
            "record_compliance_check, or generate_decision. "
            "ERROR: PreconditionFailed if session already has context loaded."
        )
    )
    async def start_agent_session(
        agent_id: str,
        session_id: str,
        application_id: str,
        model_version: str,
        context_source: str,
        context_token_count: int = 0,
        event_replay_from_position: int = 0,
    ) -> dict:
        store = store_factory()
        from src.models.events import OptimisticConcurrencyError

        stream_id = f"agent-{agent_id}-{session_id}"
        try:
            version = await store.stream_version(stream_id)
            event = {
                "event_type": "AgentContextLoaded",
                "event_version": 1,
                "payload": {
                    "agent_id": agent_id,
                    "session_id": session_id,
                    "application_id": application_id,
                    "context_source": context_source,
                    "event_replay_from_position": event_replay_from_position,
                    "context_token_count": context_token_count,
                    "model_version": model_version,
                    "loaded_at": _now(),
                },
            }
            await store.append(
                stream_id=stream_id,
                events=[event],
                expected_version=version,
            )
            return _ok(
                session_id=session_id,
                context_position=event_replay_from_position,
                stream_id=stream_id,
            )
        except OptimisticConcurrencyError as e:
            return _error(
                "OptimisticConcurrencyError", str(e),
                "reload_stream_and_retry",
                stream_id=stream_id,
            )
        except Exception as e:
            return _error("InternalError", str(e), "Check server logs")

    # ------------------------------------------------------------------
    # TOOL 3: record_credit_analysis
    # ------------------------------------------------------------------
    @mcp.tool(
        description=(
            "Record a completed credit analysis. "
            "PRECONDITION: Requires active agent session created by start_agent_session "
            "(call start_agent_session first — PreconditionFailed otherwise). "
            "Validates: agent_id must have context loaded; optimistic concurrency on loan stream. "
            "Returns event_id and new stream version."
        )
    )
    async def record_credit_analysis(
        application_id: str,
        agent_id: str,
        session_id: str,
        model_version: str,
        confidence_score: float,
        risk_tier: str,
        recommended_limit_usd: float,
        analysis_duration_ms: int,
        input_data_hash: str,
    ) -> dict:
        store = store_factory()
        from src.aggregates.agent_session import AgentSessionAggregate
        from src.aggregates.loan_application import LoanApplicationAggregate
        from src.models.events import DomainError, OptimisticConcurrencyError

        try:
            app = await LoanApplicationAggregate.load(store, application_id)
            agent = await AgentSessionAggregate.load(store, agent_id, session_id)
            app.assert_awaiting_credit_analysis()
            agent.assert_context_loaded()
            agent.assert_model_version_current(model_version)

            event = {
                "event_type": "CreditAnalysisCompleted",
                "event_version": 2,
                "payload": {
                    "application_id": application_id,
                    "agent_id": agent_id,
                    "session_id": session_id,
                    "model_version": model_version,
                    "confidence_score": confidence_score,
                    "risk_tier": risk_tier,
                    "recommended_limit_usd": recommended_limit_usd,
                    "analysis_duration_ms": analysis_duration_ms,
                    "input_data_hash": input_data_hash,
                    "completed_at": _now(),
                },
            }
            await store.append(
                stream_id=f"loan-{application_id}",
                events=[event],
                expected_version=app.current_version,
            )
            return _ok(
                event_type="CreditAnalysisCompleted",
                application_id=application_id,
                new_stream_version=app.current_version + 1,
            )
        except DomainError as e:
            return _error(
                "PreconditionFailed", str(e),
                "Ensure start_agent_session was called and application is in AwaitingAnalysis state",
            )
        except OptimisticConcurrencyError as e:
            return _error(
                "OptimisticConcurrencyError", str(e),
                "reload_stream_and_retry",
                stream_id=f"loan-{application_id}",
                expected_version=e.expected,
                actual_version=e.actual,
            )
        except Exception as e:
            return _error("InternalError", str(e), "Check server logs")

    # ------------------------------------------------------------------
    # TOOL 4: record_fraud_screening
    # ------------------------------------------------------------------
    @mcp.tool(
        description=(
            "Record a completed fraud screening result. "
            "PRECONDITION: Requires active agent session (call start_agent_session first). "
            "fraud_score must be between 0.0 and 1.0. "
            "Returns event_id and new stream version."
        )
    )
    async def record_fraud_screening(
        application_id: str,
        agent_id: str,
        session_id: str,
        fraud_score: float,
        risk_level: str,
        anomalies_found: int = 0,
        recommendation: str = "PROCEED",
        screening_model_version: str = "1.0",
        input_data_hash: str = "",
    ) -> dict:
        store = store_factory()
        from src.aggregates.agent_session import AgentSessionAggregate
        from src.models.events import DomainError, OptimisticConcurrencyError

        if not (0.0 <= fraud_score <= 1.0):
            return _error(
                "ValidationError",
                f"fraud_score must be between 0.0 and 1.0, got {fraud_score}",
                "Provide a fraud_score in [0.0, 1.0]",
            )

        try:
            agent = await AgentSessionAggregate.load(store, agent_id, session_id)
            agent.assert_context_loaded()

            version = await store.stream_version(f"loan-{application_id}")
            event = {
                "event_type": "FraudScreeningCompleted",
                "event_version": 1,
                "payload": {
                    "application_id": application_id,
                    "agent_id": agent_id,
                    "session_id": session_id,
                    "fraud_score": fraud_score,
                    "risk_level": risk_level,
                    "anomalies_found": anomalies_found,
                    "recommendation": recommendation,
                    "screening_model_version": screening_model_version,
                    "input_data_hash": input_data_hash,
                    "completed_at": _now(),
                },
            }
            await store.append(
                stream_id=f"fraud-{application_id}",
                events=[event],
                expected_version=await store.stream_version(f"fraud-{application_id}"),
            )
            return _ok(
                event_type="FraudScreeningCompleted",
                application_id=application_id,
                fraud_score=fraud_score,
            )
        except DomainError as e:
            return _error(
                "PreconditionFailed", str(e),
                "Ensure start_agent_session was called before recording fraud screening",
            )
        except OptimisticConcurrencyError as e:
            return _error(
                "OptimisticConcurrencyError", str(e),
                "reload_stream_and_retry",
                stream_id=f"fraud-{application_id}",
            )
        except Exception as e:
            return _error("InternalError", str(e), "Check server logs")

    # ------------------------------------------------------------------
    # TOOL 5: record_compliance_check
    # ------------------------------------------------------------------
    @mcp.tool(
        description=(
            "Record a compliance rule evaluation (pass or fail). "
            "PRECONDITION: Requires active agent session. "
            "rule_id must exist in the active regulation_set_version. "
            "Returns check_id and updated compliance_status."
        )
    )
    async def record_compliance_check(
        application_id: str,
        session_id: str,
        rule_id: str,
        rule_name: str,
        rule_version: str,
        passed: bool,
        evidence_hash: str = "",
        failure_reason: str = "",
        is_hard_block: bool = False,
        regulation_set_version: str = "v1.0",
    ) -> dict:
        store = store_factory()
        from src.models.events import OptimisticConcurrencyError

        stream_id = f"compliance-{application_id}"
        try:
            version = await store.stream_version(stream_id)
            if passed:
                event = {
                    "event_type": "ComplianceRulePassed",
                    "event_version": 1,
                    "payload": {
                        "application_id": application_id,
                        "session_id": session_id,
                        "rule_id": rule_id,
                        "rule_name": rule_name,
                        "rule_version": rule_version,
                        "evidence_hash": evidence_hash,
                        "evaluation_notes": f"Rule {rule_id} passed",
                        "evaluated_at": _now(),
                    },
                }
            else:
                event = {
                    "event_type": "ComplianceRuleFailed",
                    "event_version": 1,
                    "payload": {
                        "application_id": application_id,
                        "session_id": session_id,
                        "rule_id": rule_id,
                        "rule_name": rule_name,
                        "rule_version": rule_version,
                        "failure_reason": failure_reason,
                        "is_hard_block": is_hard_block,
                        "remediation_available": False,
                        "evidence_hash": evidence_hash,
                        "evaluated_at": _now(),
                    },
                }
            await store.append(
                stream_id=stream_id,
                events=[event],
                expected_version=version,
            )
            return _ok(
                check_id=f"{rule_id}-{_now()}",
                compliance_status="PASSED" if passed else "FAILED",
                rule_id=rule_id,
            )
        except OptimisticConcurrencyError as e:
            return _error(
                "OptimisticConcurrencyError", str(e),
                "reload_stream_and_retry",
                stream_id=stream_id,
            )
        except Exception as e:
            return _error("InternalError", str(e), "Check server logs")

    # ------------------------------------------------------------------
    # TOOL 6: generate_decision
    # ------------------------------------------------------------------
    @mcp.tool(
        description=(
            "Generate a final decision for a loan application. "
            "PRECONDITION: All required analyses (credit, fraud, compliance) must be present. "
            "REGULATORY RULE: confidence_score < 0.6 forces recommendation='REFER' regardless of input. "
            "contributing_agent_sessions must reference sessions that processed this application_id. "
            "Returns decision_id and recommendation."
        )
    )
    async def generate_decision(
        application_id: str,
        orchestrator_agent_id: str,
        recommendation: str,
        confidence_score: float,
        contributing_agent_sessions: list,
        decision_basis_summary: str,
        model_versions: dict | None = None,
        approved_amount_usd: float | None = None,
    ) -> dict:
        store = store_factory()
        from src.models.events import OptimisticConcurrencyError

        # Confidence floor enforcement (regulatory requirement)
        if confidence_score < 0.6:
            recommendation = "REFER"

        try:
            version = await store.stream_version(f"loan-{application_id}")
            event = {
                "event_type": "DecisionGenerated",
                "event_version": 2,
                "payload": {
                    "application_id": application_id,
                    "orchestrator_agent_id": orchestrator_agent_id,
                    "recommendation": recommendation,
                    "confidence_score": confidence_score,
                    "contributing_agent_sessions": contributing_agent_sessions,
                    "decision_basis_summary": decision_basis_summary,
                    "model_versions": model_versions or {},
                    "approved_amount_usd": approved_amount_usd,
                    "generated_at": _now(),
                },
            }
            await store.append(
                stream_id=f"loan-{application_id}",
                events=[event],
                expected_version=version,
            )
            return _ok(
                decision_id=f"dec-{application_id}",
                recommendation=recommendation,
                confidence_score=confidence_score,
                confidence_floor_applied=confidence_score < 0.6,
            )
        except OptimisticConcurrencyError as e:
            return _error(
                "OptimisticConcurrencyError", str(e),
                "reload_stream_and_retry",
                stream_id=f"loan-{application_id}",
                expected_version=e.expected,
                actual_version=e.actual,
            )
        except Exception as e:
            return _error("InternalError", str(e), "Check server logs")

    # ------------------------------------------------------------------
    # TOOL 7: record_human_review
    # ------------------------------------------------------------------
    @mcp.tool(
        description=(
            "Record a human loan officer's review and final decision. "
            "PRECONDITION: DecisionGenerated must already exist for this application. "
            "If override=True, override_reason is REQUIRED. "
            "Returns final_decision and application_state."
        )
    )
    async def record_human_review(
        application_id: str,
        reviewer_id: str,
        final_decision: str,
        override: bool = False,
        override_reason: str | None = None,
        original_recommendation: str = "",
    ) -> dict:
        store = store_factory()
        from src.models.events import OptimisticConcurrencyError

        if override and not override_reason:
            return _error(
                "ValidationError",
                "override_reason is required when override=True",
                "Provide override_reason explaining why the AI recommendation was overridden",
            )

        try:
            version = await store.stream_version(f"loan-{application_id}")
            review_event = {
                "event_type": "HumanReviewCompleted",
                "event_version": 1,
                "payload": {
                    "application_id": application_id,
                    "reviewer_id": reviewer_id,
                    "override": override,
                    "original_recommendation": original_recommendation,
                    "final_decision": final_decision,
                    "override_reason": override_reason,
                    "reviewed_at": _now(),
                },
            }

            outcome_event_type = "ApplicationApproved" if final_decision == "APPROVE" else "ApplicationDeclined"
            if final_decision == "APPROVE":
                outcome_event = {
                    "event_type": "ApplicationApproved",
                    "event_version": 1,
                    "payload": {
                        "application_id": application_id,
                        "approved_by": reviewer_id,
                        "effective_date": _now()[:10],
                        "approved_at": _now(),
                        "approved_amount_usd": None,
                        "interest_rate_pct": 0.0,
                        "term_months": 60,
                        "conditions": [],
                    },
                }
            else:
                outcome_event = {
                    "event_type": "ApplicationDeclined",
                    "event_version": 1,
                    "payload": {
                        "application_id": application_id,
                        "decline_reasons": [override_reason or "Human review declined"],
                        "declined_by": reviewer_id,
                        "adverse_action_notice_required": True,
                        "adverse_action_codes": [],
                        "declined_at": _now(),
                    },
                }

            await store.append(
                stream_id=f"loan-{application_id}",
                events=[review_event, outcome_event],
                expected_version=version,
            )
            return _ok(
                final_decision=final_decision,
                application_state="APPROVED" if final_decision == "APPROVE" else "DECLINED",
                override_applied=override,
            )
        except OptimisticConcurrencyError as e:
            return _error(
                "OptimisticConcurrencyError", str(e),
                "reload_stream_and_retry",
                stream_id=f"loan-{application_id}",
            )
        except Exception as e:
            return _error("InternalError", str(e), "Check server logs")

    # ------------------------------------------------------------------
    # TOOL 8: run_integrity_check
    # ------------------------------------------------------------------
    @mcp.tool(
        description=(
            "Run a cryptographic integrity check on an entity's audit chain. "
            "ROLE RESTRICTION: Can only be called by compliance role. "
            "RATE LIMIT: 1 check per minute per entity (enforced by caller). "
            "Returns check_result with chain_valid (bool) and tamper_detected (bool)."
        )
    )
    async def run_integrity_check(
        entity_type: str,
        entity_id: str,
        caller_role: str = "compliance",
    ) -> dict:
        if caller_role not in ("compliance", "admin", "auditor"):
            return _error(
                "AuthorizationError",
                f"Role '{caller_role}' is not authorized to run integrity checks",
                "Request with caller_role='compliance' or 'admin'",
            )

        store = store_factory()
        try:
            from src.integrity.audit_chain import run_integrity_check as _run_check
            result = await _run_check(store, entity_type, entity_id)
            return _ok(
                entity_type=entity_type,
                entity_id=entity_id,
                events_verified=result.events_verified,
                chain_valid=result.chain_valid,
                tamper_detected=result.tamper_detected,
                integrity_hash=result.integrity_hash,
                checked_at=result.checked_at.isoformat(),
            )
        except Exception as e:
            return _error("InternalError", str(e), "Check server logs")
