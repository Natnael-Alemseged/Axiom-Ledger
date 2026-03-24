from __future__ import annotations
from src.upcasting.registry import UpcasterRegistry

registry = UpcasterRegistry()


@registry.register("CreditAnalysisCompleted", from_version=1)
def upcast_credit_v1_to_v2(payload: dict) -> dict:
    """
    CreditAnalysisCompleted v1 → v2.

    Inference strategy:
    - model_version: set to "legacy-pre-2026" — best available inference without store lookup.
    - confidence_score: preserved if present, else set to None. Fabrication would be worse
      than None because downstream systems treat None as "unknown" but a fabricated value
      would be trusted as accurate.
    - regulatory_basis: default to [] — genuinely unknown for historical events.
    """
    return {
        **payload,
        "model_version": payload.get("model_version", "legacy-pre-2026"),
        "confidence_score": payload.get("confidence_score"),  # None if missing — never fabricate
        "regulatory_basis": payload.get("regulatory_basis", []),
    }


@registry.register("DecisionGenerated", from_version=1)
def upcast_decision_v1_to_v2(payload: dict) -> dict:
    """
    DecisionGenerated v1 → v2.

    Inference strategy for model_versions{}:
    - We cannot load contributing sessions here (no store reference in upcaster — by design,
      upcasters must be pure functions to avoid circular dependency and performance issues).
    - Set to empty dict {}. The correct approach is to rebuild from contributing_agent_sessions
      via a separate query if needed.
    - Performance implication: store lookup in upcaster would be called on EVERY event load,
      creating N+1 query problems. Pure functions are the correct architecture.
    """
    return {
        **payload,
        "model_versions": payload.get("model_versions", {}),
        "contributing_agent_sessions": payload.get("contributing_agent_sessions", []),
        "decision_basis_summary": payload.get("decision_basis_summary",
                                               payload.get("executive_summary", "")),
    }
