from __future__ import annotations
import asyncio
from datetime import datetime
from typing import Any

class ApplicationSummaryProjection:
    name = "application_summary"

    def __init__(self):
        self._state: dict[str, dict] = {}  # application_id -> summary

    def get(self, application_id: str) -> dict | None:
        return self._state.get(application_id)

    def get_all(self) -> list[dict]:
        return list(self._state.values())

    async def handle(self, event: dict) -> None:
        """Route event to the right handler."""
        et = event["event_type"]
        payload = event["payload"]
        app_id = payload.get("application_id")
        if app_id is None:
            return

        if et == "ApplicationSubmitted":
            self._state[app_id] = {
                "application_id": app_id,
                "state": "SUBMITTED",
                "applicant_id": payload.get("applicant_id"),
                "requested_amount_usd": payload.get("requested_amount_usd"),
                "approved_amount_usd": None,
                "risk_tier": None,
                "fraud_score": None,
                "compliance_status": None,
                "decision": None,
                "agent_sessions_completed": [],
                "last_event_type": et,
                "last_event_at": event.get("recorded_at"),
                "human_reviewer_id": None,
                "final_decision_at": None,
            }
        elif app_id in self._state:
            s = self._state[app_id]
            s["last_event_type"] = et
            s["last_event_at"] = event.get("recorded_at")

            if et == "CreditAnalysisCompleted":
                s["risk_tier"] = payload.get("risk_tier")
                s["state"] = "CREDIT_COMPLETE"
            elif et == "FraudScreeningCompleted":
                s["fraud_score"] = payload.get("fraud_score")
                s["state"] = "FRAUD_COMPLETE"
            elif et == "ComplianceCheckRequested":
                s["compliance_status"] = "PENDING"
                s["state"] = "COMPLIANCE_REVIEW"
            elif et in ("ComplianceRulePassed", "ComplianceCheckCompleted"):
                s["compliance_status"] = "PASSED"
            elif et == "ComplianceRuleFailed":
                s["compliance_status"] = "FAILED"
            elif et == "DecisionGenerated":
                s["decision"] = payload.get("recommendation")
                s["state"] = "PENDING_DECISION"
                session_id = payload.get("session_id") or payload.get("orchestrator_session_id")
                if session_id and session_id not in s["agent_sessions_completed"]:
                    s["agent_sessions_completed"].append(session_id)
            elif et == "HumanReviewCompleted":
                s["human_reviewer_id"] = payload.get("reviewer_id")
                s["state"] = "PENDING_HUMAN_REVIEW"
            elif et == "ApplicationApproved":
                s["approved_amount_usd"] = payload.get("approved_amount_usd")
                s["state"] = "APPROVED"
                s["final_decision_at"] = event.get("recorded_at")
            elif et == "ApplicationDeclined":
                s["state"] = "DECLINED"
                s["final_decision_at"] = event.get("recorded_at")
