from __future__ import annotations

from datetime import datetime, timezone

from src.upcasting.registry import UpcasterRegistry

registry = UpcasterRegistry()


def _parse_recorded_at(event: dict | None) -> datetime | None:
    """Parse recorded_at from the event envelope for inference-only upcasting."""
    if not event:
        return None
    raw = event.get("recorded_at")
    if raw is None:
        return None
    if isinstance(raw, datetime):
        return raw if raw.tzinfo else raw.replace(tzinfo=timezone.utc)
    if isinstance(raw, str):
        s = raw.replace("Z", "+00:00")
        dt = datetime.fromisoformat(s)
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    return None


def _infer_model_version_from_recorded_at(payload: dict, recorded_at: datetime | None) -> str:
    """
    Timestamp-based inference for legacy v1 events missing model_version.

    Buckets are documented and deterministic; no fabricated precision.
    """
    if payload.get("model_version"):
        return str(payload["model_version"])
    if recorded_at is None:
        return "legacy-unknown-recorded-at"
    y = recorded_at.year
    # Pre-2024: legacy credit stack naming
    if recorded_at < datetime(2024, 1, 1, tzinfo=timezone.utc):
        return f"credit-legacy-y{y}"
    return f"credit-active-y{y}"


def _infer_regulatory_basis(recorded_at: datetime | None) -> list[dict[str, str]]:
    """
    Infer regulatory_basis from rule-package versions active on recorded_at.

    Static effective-dating table — no DB; unknown date yields empty list.
    """
    if recorded_at is None:
        return []
    y = recorded_at.year
    if y < 2024:
        return [
            {"package": "FIN_BASE", "version": "2019.1", "basis": "rules_active_before_2024"},
        ]
    if y < 2026:
        return [
            {"package": "FIN_2024", "version": "2.1", "basis": "rules_active_2024_2025"},
        ]
    return [
        {"package": "FIN_2026", "version": "3.0", "basis": "rules_active_from_2026"},
    ]


def _reconstruct_model_versions_from_sessions(
    payload: dict,
    recorded_at: datetime | None,
) -> dict[str, str]:
    """
    Rebuild model_versions from contributing_agent_sessions when v1 omitted it.

    Without cross-stream lookups, we attach a per-session placeholder tied to the
    decision's recorded time bucket (documented inference, not stored mutation).
    """
    existing = payload.get("model_versions")
    if isinstance(existing, dict) and existing:
        return dict(existing)
    sessions = payload.get("contributing_agent_sessions") or []
    year = recorded_at.year if recorded_at else None
    bucket = f"y{year}" if year is not None else "undated"
    out: dict[str, str] = {}
    for sid in sessions:
        sid_str = str(sid)
        out[sid_str] = f"inferred-from-session@{bucket}:{sid_str[:32]}"
    return out


@registry.register("CreditAnalysisCompleted", from_version=1)
def upcast_credit_v1_to_v2(payload: dict, event: dict | None = None) -> dict:
    """
    CreditAnalysisCompleted v1 → v2.

    Inference strategy:
    - model_version: from payload, else timestamp-based bucket from event.recorded_at.
    - confidence_score: preserved if present, else None (never fabricated).
    - regulatory_basis: from payload, else inferred from rule packages active at recorded_at.
    """
    recorded_at = _parse_recorded_at(event)
    model_version = _infer_model_version_from_recorded_at(payload, recorded_at)
    regulatory_basis = payload.get("regulatory_basis")
    if regulatory_basis is None:
        regulatory_basis = _infer_regulatory_basis(recorded_at)
    return {
        **payload,
        "model_version": model_version,
        "confidence_score": payload.get("confidence_score"),
        "regulatory_basis": regulatory_basis,
    }


@registry.register("DecisionGenerated", from_version=1)
def upcast_decision_v1_to_v2(payload: dict, event: dict | None = None) -> dict:
    """
    DecisionGenerated v1 → v2.

    Inference strategy for model_versions:
    - If already present, keep.
    - Else reconstruct a dict keyed by each contributing_agent_sessions entry.
    """
    recorded_at = _parse_recorded_at(event)
    model_versions = _reconstruct_model_versions_from_sessions(payload, recorded_at)
    return {
        **payload,
        "model_versions": model_versions,
        "contributing_agent_sessions": payload.get("contributing_agent_sessions", []),
        "decision_basis_summary": payload.get(
            "decision_basis_summary",
            payload.get("executive_summary", ""),
        ),
    }
