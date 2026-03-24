from __future__ import annotations
from datetime import datetime

class AgentPerformanceLedgerProjection:
    name = "agent_performance"

    def __init__(self):
        self._state: dict[tuple, dict] = {}  # (agent_id, model_version) -> metrics

    def get(self, agent_id: str, model_version: str = None) -> list[dict]:
        if model_version:
            k = (agent_id, model_version)
            v = self._state.get(k)
            return [v] if v else []
        return [v for (a, _), v in self._state.items() if a == agent_id]

    def _ensure(self, agent_id: str, model_version: str, first_seen: datetime) -> dict:
        k = (agent_id, model_version)
        if k not in self._state:
            self._state[k] = {
                "agent_id": agent_id,
                "model_version": model_version,
                "analyses_completed": 0,
                "decisions_generated": 0,
                "avg_confidence_score": 0.0,
                "avg_duration_ms": 0.0,
                "approve_rate": 0.0,
                "decline_rate": 0.0,
                "refer_rate": 0.0,
                "human_override_rate": 0.0,
                "_confidence_sum": 0.0,
                "_duration_sum": 0.0,
                "_approve_count": 0,
                "_decline_count": 0,
                "_refer_count": 0,
                "_override_count": 0,
                "_review_count": 0,
                "first_seen_at": first_seen,
                "last_seen_at": first_seen,
            }
        return self._state[k]

    async def handle(self, event: dict) -> None:
        et = event["event_type"]
        p = event["payload"]
        ts = event.get("recorded_at")

        if et == "CreditAnalysisCompleted":
            agent_id = p.get("agent_id", "unknown")
            model_version = p.get("model_version", "unknown")
            m = self._ensure(agent_id, model_version, ts)
            m["analyses_completed"] += 1
            conf = p.get("confidence_score", 0)
            dur = p.get("analysis_duration_ms", 0)
            m["_confidence_sum"] += conf
            m["_duration_sum"] += dur
            m["avg_confidence_score"] = m["_confidence_sum"] / m["analyses_completed"]
            m["avg_duration_ms"] = m["_duration_sum"] / m["analyses_completed"]
            m["last_seen_at"] = ts

        elif et == "DecisionGenerated":
            agent_id = p.get("orchestrator_agent_id") or p.get("orchestrator_session_id", "unknown")
            model_version = (p.get("model_versions") or {}).get("orchestrator", "unknown")
            m = self._ensure(agent_id, model_version, ts)
            m["decisions_generated"] += 1
            rec = p.get("recommendation", "")
            if rec == "APPROVE":
                m["_approve_count"] += 1
            elif rec == "DECLINE":
                m["_decline_count"] += 1
            elif rec == "REFER":
                m["_refer_count"] += 1
            total = m["decisions_generated"]
            m["approve_rate"] = m["_approve_count"] / total
            m["decline_rate"] = m["_decline_count"] / total
            m["refer_rate"] = m["_refer_count"] / total
            m["last_seen_at"] = ts

        elif et == "HumanReviewCompleted":
            if p.get("override"):
                # Find the session — use reviewer as proxy key
                for k, m in self._state.items():
                    m["_review_count"] = m.get("_review_count", 0) + 1
                    m["_override_count"] = m.get("_override_count", 0) + 1
                    if m["_review_count"] > 0:
                        m["human_override_rate"] = m["_override_count"] / m["_review_count"]
