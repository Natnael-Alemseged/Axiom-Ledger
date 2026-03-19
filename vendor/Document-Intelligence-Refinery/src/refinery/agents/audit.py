"""
Audit Mode: verify a claim against the corpus.
Returns verified (with citation), not_found, or unverifiable (e.g. conflicting evidence).
"""

import logging
from typing import Any, Literal, Optional

from refinery.models import ProvenanceChain, SourceCitation
from refinery.agents.query_agent import run_query

logger = logging.getLogger(__name__)


def verify_claim(
    claim: str,
    doc_id: Optional[str] = None,
) -> dict:
    """
    Verify a claim against the document(s). Returns structured result:
    - verified: bool
    - citation: ProvenanceChain or None (when verified)
    - status: "verified" | "not_found" | "unverifiable"
    - evidence: list of nearest chunks (with bbox, content_hash, text) for inspection
    """
    result = run_query(claim, doc_id=doc_id)
    citations: list = result.get("citations", [])
    answer = result.get("answer", "")
    provenance_chain: Optional[ProvenanceChain] = result.get("provenance_chain")

    # Build evidence list from citations (nearest chunks)
    evidence = []
    for c in citations:
        if isinstance(c, SourceCitation):
            evidence.append({
                "document_name": c.document_name,
                "page_number": c.page_number,
                "page_numbers": getattr(c, "page_numbers", []),
                "bbox": c.bbox,
                "content_hash": c.content_hash,
                "text": getattr(c, "text", None),
            })
        elif isinstance(c, dict):
            evidence.append(c)

    # Heuristic: no citations -> not_found
    if not citations:
        return {
            "verified": False,
            "citation": None,
            "status": "not_found",
            "evidence": evidence,
        }

    # If answer says "No matching content" treat as not_found
    if "No matching content found" in (answer or ""):
        return {
            "verified": False,
            "citation": None,
            "status": "not_found",
            "evidence": evidence,
        }

    # Single supporting citation -> verified
    if len(citations) == 1:
        return {
            "verified": True,
            "citation": provenance_chain,
            "status": "verified",
            "evidence": evidence,
        }

    # Multiple citations: could be supporting or conflicting -> unverifiable unless single source
    return {
        "verified": False,
        "citation": provenance_chain,
        "status": "unverifiable",
        "evidence": evidence,
    }


def _llm_judge_claim(claim: str, evidence_summaries: list[str]) -> tuple[bool, str]:
    """
    Call LLM to judge whether the evidence supports the claim or is conflicting.
    Returns (supported: bool, reason: str). Uses OPENROUTER same as PageIndex summaries.
    """
    import os
    import httpx
    api_key = os.environ.get("OPENROUTER_API_KEY") or os.environ.get("OPENROUTER_KEY")
    if not api_key:
        return (False, "unverifiable (no API key for judge)")
    evidence_text = "\n".join(f"- {s[:1200]}" for s in evidence_summaries[:5])
    prompt = f"""Given this claim and the evidence excerpts below, does the evidence support the claim, or is it conflicting/ambiguous?
Claim: "{claim}"

Evidence excerpts:
{evidence_text}

Answer with exactly one line: SUPPORTED or UNVERIFIABLE, then a short reason."""
    try:
        with httpx.Client(timeout=25.0) as client:
            r = client.post(
                "https://openrouter.ai/api/v1/chat/completions",
                headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
                json={
                    "model": "google/gemini-2.0-flash-exp:free",
                    "messages": [{"role": "user", "content": prompt}],
                    "max_tokens": 150,
                },
            )
        r.raise_for_status()
        data = r.json()
        content = (data.get("choices") or [{}])[0].get("message", {}).get("content", "") or ""
        supported = "SUPPORTED" in content.upper() and "UNVERIFIABLE" not in content.split()[0].upper()
        return (supported, content.strip() or "no reason")
    except Exception as e:
        logger.warning("LLM judge failed: %s", e)
        return (False, f"unverifiable (judge error: {e})")


def verify_claim_with_judge(
    claim: str,
    doc_id: Optional[str] = None,
) -> dict:
    """
    Same as verify_claim but with an LLM judge step when multiple evidence items exist:
    LLM decides whether evidence supports the claim or is conflicting -> verified vs unverifiable.
    """
    base = verify_claim(claim, doc_id=doc_id)
    evidence = base.get("evidence", [])
    if base["status"] != "unverifiable" or len(evidence) < 2:
        return base
    # Multiple citations: use LLM judge to decide support vs conflicting
    evidence_summaries = []
    for e in evidence:
        excerpt = (e.get("text") or "").strip()
        metadata = f'{e.get("document_name", "")} p.{e.get("page_number", "")}'
        if e.get("content_hash"):
            metadata += f' {str(e.get("content_hash", ""))[:16]}'
        evidence_summaries.append(f"{metadata}: {excerpt}" if excerpt else metadata)
    supported, reason = _llm_judge_claim(claim, evidence_summaries)
    if supported:
        return {
            "verified": True,
            "citation": base.get("citation"),
            "status": "verified",
            "evidence": evidence,
            "judge_reason": reason,
        }
    return {
        "verified": False,
        "citation": base.get("citation"),
        "status": "unverifiable",
        "evidence": evidence,
        "judge_reason": reason,
    }
