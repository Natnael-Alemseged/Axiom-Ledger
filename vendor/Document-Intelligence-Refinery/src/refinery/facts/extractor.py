"""Extract key-value facts from LDUs for financial/numerical documents."""

import logging
import re
from typing import Any, List, Optional

from refinery.models import LDU
from refinery.models.document_profile import DocumentProfile

from refinery.facts.schema import FactRow
from refinery.facts.store import FactStore

logger = logging.getLogger(__name__)


def should_run_fact_extraction(profile: Optional[DocumentProfile]) -> bool:
    """Run fact extraction only if domain_hint is financial/legal or layout is table_heavy."""
    if profile is None:
        return False
    if profile.domain_hint in ("financial", "legal"):
        return True
    if profile.layout_complexity == "table_heavy":
        return True
    return False


def _extract_facts_regex(text: str, doc_id: str, page_ref: int, bbox: Optional[dict], content_hash: Optional[str], source_ldu_id: Optional[str]) -> List[FactRow]:
    """Regex-based extraction: currency, dates, quarters."""
    rows: List[FactRow] = []
    # Q1 2024, Q2 2023
    for m in re.finditer(r"\b(Q[1-4])\s*(20\d{2})\b", text, re.IGNORECASE):
        rows.append(
            FactRow(doc_id=doc_id, page_ref=page_ref, key="quarter", value=f"{m.group(1)} {m.group(2)}", bbox=bbox, content_hash=content_hash, source_ldu_id=source_ldu_id)
        )
    # $1.2B, $4.2 million
    for m in re.finditer(r"\$\s*([\d,.]+)\s*([BMKbmk])?", text):
        val = m.group(1).replace(",", "")
        unit = (m.group(2) or "").upper() or None
        if unit == "B":
            unit = "billion"
        elif unit == "M":
            unit = "million"
        elif unit == "K":
            unit = "thousand"
        rows.append(
            FactRow(doc_id=doc_id, page_ref=page_ref, key="revenue", value=val, unit=unit or "USD", bbox=bbox, content_hash=content_hash, source_ldu_id=source_ldu_id)
        )
    # Year
    for m in re.finditer(r"\b(20\d{2})\b", text):
        rows.append(
            FactRow(doc_id=doc_id, page_ref=page_ref, key="year", value=int(m.group(1)), bbox=bbox, content_hash=content_hash, source_ldu_id=source_ldu_id)
        )
    return rows


def _ldu_text(ldu: LDU) -> str:
    if ldu.content is None:
        return ""
    if isinstance(ldu.content, str):
        return ldu.content
    if isinstance(ldu.content, list):
        return " ".join(str(cell) for row in ldu.content for cell in (row if isinstance(row, (list, tuple)) else [row]))
    return str(ldu.content)


def extract_facts_from_ldus(
    doc_id: str,
    ldus: List[LDU],
    profile: Optional[DocumentProfile] = None,
    store: Optional[FactStore] = None,
) -> List[FactRow]:
    """
    Extract key-value facts from LDUs. Only runs if should_run_fact_extraction(profile).
    Uses regex for currency, dates, quarters. Optionally writes to FactStore.
    """
    if not should_run_fact_extraction(profile):
        logger.debug("Skipping fact extraction for doc_id=%s (profile does not match)", doc_id)
        return []
    all_rows: List[FactRow] = []
    seen: set = set()  # dedupe by (doc_id, page_ref, key, value)
    for ldu in ldus:
        page_ref = (ldu.page_refs[0]) if ldu.page_refs else 0
        text = _ldu_text(ldu)
        if not text.strip():
            continue
        bbox = ldu.bbox
        content_hash = ldu.content_hash
        source_ldu_id = ldu.chunk_id
        for row in _extract_facts_regex(text, doc_id, page_ref, bbox, content_hash, source_ldu_id):
            dedupe_key = (row.doc_id, row.page_ref, row.key, str(row.value))
            if dedupe_key in seen:
                continue
            seen.add(dedupe_key)
            all_rows.append(row)
    if store and all_rows:
        store.insert_many(all_rows)
        logger.info("Inserted %d facts for doc_id=%s", len(all_rows), doc_id)
    return all_rows
