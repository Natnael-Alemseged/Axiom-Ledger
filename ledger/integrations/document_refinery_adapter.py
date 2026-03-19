"""
Adapter for Week 3 Document-Intelligence-Refinery extraction.

This module provides a stable async API for ledger agents while encapsulating
vendor import-path bootstrapping and output normalization.
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
import sys
import time
from contextlib import contextmanager
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, TypedDict

from ledger.integrations.llm_key_utils import effective_google_generative_key
from ledger.integrations.llm_key_utils import is_likely_openrouter_key


_REPO_ROOT = Path(__file__).resolve().parents[2]
_REFINERY_REPO = _REPO_ROOT / "vendor" / "Document-Intelligence-Refinery"
_REFINERY_SRC = _REFINERY_REPO / "src"
_TOP_LEVEL_ENV = _REPO_ROOT / ".env"
_LOG = logging.getLogger(__name__)


class ExtractionAdapterResult(TypedDict):
    facts: dict[str, Any]
    raw_text_length: int
    tables_extracted: int
    processing_ms: int
    strategy_used: str
    status: str


def _ensure_refinery_importable() -> None:
    if not _REFINERY_REPO.exists():
        raise RuntimeError(
            "Week 3 repository not found at "
            f"{_REFINERY_REPO}. Clone it into vendor/Document-Intelligence-Refinery."
        )
    if not _REFINERY_SRC.exists():
        raise RuntimeError(
            "Week 3 source path not found at "
            f"{_REFINERY_SRC}. Ensure the cloned repo includes src/refinery."
        )
    src_str = str(_REFINERY_SRC)
    if src_str not in sys.path:
        sys.path.insert(0, src_str)


def _load_top_level_env() -> None:
    # Top-level .env may contain GEMINI_API_KEY used by vendor extraction.
    try:
        from dotenv import load_dotenv

        load_dotenv(_TOP_LEVEL_ENV, override=False)
    except Exception:
        # Keep adapter resilient if python-dotenv is absent.
        return


@contextmanager
def _prefer_gemini_provider():
    """
    Force vendor vision provider selection toward Google Gemini when key exists.

    The vendor implementation tries providers in this order:
    OpenRouter -> Groq -> Google -> SambaNova.
    To honor explicit Gemini usage, we temporarily clear earlier provider keys.
    """
    _load_top_level_env()
    raw = os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY")
    # Do not copy OpenRouter keys into GOOGLE_API_KEY — vendor Google client would reject them.
    if raw and not is_likely_openrouter_key(raw) and not os.environ.get("GOOGLE_API_KEY"):
        os.environ["GOOGLE_API_KEY"] = raw

    keys_to_mask = [
        "OPENROUTER_API_KEY",
        "OPENROUTER_KEY",
        "GROQ_API_KEY",
        "SAMBANOVA_KEY",
        "SAMBANOVA_API_KEY",
    ]
    original = {k: os.environ.get(k) for k in keys_to_mask}
    try:
        # Only strip other providers when we have a real Google key (force Gemini path in vendor).
        if effective_google_generative_key():
            for key in keys_to_mask:
                os.environ.pop(key, None)
        yield
    finally:
        for key, val in original.items():
            if val is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = val


def _normalize_space(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


def _parse_decimal(token: str) -> Decimal | None:
    cleaned = token.strip()
    if not cleaned:
        return None
    negative = cleaned.startswith("(") and cleaned.endswith(")")
    cleaned = cleaned.replace("(", "").replace(")", "")
    cleaned = cleaned.replace("$", "").replace(",", "").replace(" ", "")
    try:
        value = Decimal(cleaned)
    except InvalidOperation:
        return None
    return -value if negative else value


def _extract_amount_near_label(chunk: str, label_patterns: list[str]) -> Decimal | None:
    normalized = _normalize_space(chunk)
    lowered = normalized.lower()
    for pattern in label_patterns:
        m = re.search(pattern, lowered)
        if not m:
            continue
        tail = normalized[m.end() :]
        number_match = re.search(r"\(?-?\$?\s*\d[\d,]*(?:\.\d+)?\)?", tail)
        if number_match:
            value = _parse_decimal(number_match.group(0))
            if value is not None:
                return value
    return None


def _collect_text_and_tables(extracted_doc: Any) -> tuple[list[str], int]:
    chunks: list[str] = []
    table_count = 0
    for page in getattr(extracted_doc, "pages", []) or []:
        for block in getattr(page, "text_blocks", []) or []:
            text = getattr(block, "text", None)
            if text:
                chunks.append(str(text))
        for table in getattr(page, "tables", []) or []:
            table_count += 1
            rows = getattr(table, "data", []) or []
            for row in rows:
                if isinstance(row, (list, tuple)):
                    chunks.append(" | ".join(str(cell) for cell in row))
                else:
                    chunks.append(str(row))
    return chunks, table_count


def _extract_facts_from_chunks(chunks: list[str], document_type: str) -> dict[str, Any]:
    label_map: dict[str, list[str]] = {
        "total_revenue": [r"\btotal\s+revenue\b", r"\brevenue\b", r"\btotal\s+sales\b"],
        "gross_profit": [r"\bgross\s+profit\b"],
        "operating_expenses": [r"\boperating\s+expenses?\b"],
        "operating_income": [r"\boperating\s+income\b", r"\boperating\s+profit\b"],
        "ebitda": [r"\bebitda\b"],
        "interest_expense": [r"\binterest\s+expense\b"],
        "net_income": [r"\bnet\s+income\b", r"\bnet\s+profit\b"],
        "total_assets": [r"\btotal\s+assets\b"],
        "current_assets": [r"\bcurrent\s+assets\b"],
        "cash_and_equivalents": [r"\bcash(?:\s+and|\s*&)\s+equivalents\b"],
        "accounts_receivable": [r"\baccounts?\s+receivable\b"],
        "inventory": [r"\binventory\b"],
        "total_liabilities": [r"\btotal\s+liabilities\b"],
        "current_liabilities": [r"\bcurrent\s+liabilities\b"],
        "long_term_debt": [r"\blong[\s-]?term\s+debt\b"],
        "total_equity": [r"\btotal\s+equity\b", r"\bshareholders?\s+equity\b"],
    }
    facts: dict[str, Any] = {"field_confidence": {}, "extraction_notes": []}
    for field, patterns in label_map.items():
        found: Decimal | None = None
        for chunk in chunks:
            found = _extract_amount_near_label(chunk, patterns)
            if found is not None:
                break
        facts[field] = found
        if found is not None:
            facts["field_confidence"][field] = 0.65

    # Derived checks/ratios when possible.
    revenue = facts.get("total_revenue")
    gross_profit = facts.get("gross_profit")
    net_income = facts.get("net_income")
    if revenue and revenue != 0 and gross_profit is not None:
        facts["gross_margin"] = float(gross_profit / revenue)
    if revenue and revenue != 0 and net_income is not None:
        facts["net_margin"] = float(net_income / revenue)

    total_assets = facts.get("total_assets")
    total_liabilities = facts.get("total_liabilities")
    total_equity = facts.get("total_equity")
    if total_assets is not None and total_liabilities is not None and total_equity is not None:
        discrepancy = total_assets - (total_liabilities + total_equity)
        facts["balance_sheet_balances"] = abs(discrepancy) < Decimal("1.00")
        facts["balance_discrepancy_usd"] = discrepancy

    critical_by_doc = {
        "income_statement": ["total_revenue", "gross_profit", "net_income"],
        "balance_sheet": ["total_assets", "total_liabilities", "total_equity"],
    }
    for field in critical_by_doc.get(document_type, []):
        if facts.get(field) is None:
            facts["field_confidence"][field] = 0.0
            facts["extraction_notes"].append(
                f"Critical field missing from extraction: {field}"
            )
    return facts


def _run_week3_pipeline(file_path: Path, document_type: str) -> ExtractionAdapterResult:
    _LOG.info("week3_pipeline_start path=%s document_type=%s", file_path, document_type)
    _ensure_refinery_importable()
    _load_top_level_env()
    try:
        # Import narrowly from the Week 3 modules to avoid pulling optional
        # heavy subsystems from refinery.__init__ at adapter import time.
        from refinery.agents.extractor import run_extraction  # type: ignore
        from refinery.triage.agent import run_triage  # type: ignore
        from refinery.strategies.base import load_extraction_rules  # type: ignore
        from refinery.strategies.fast_text import FastTextExtractor  # type: ignore
    except Exception as exc:
        raise RuntimeError(
            "Failed to import Week 3 'refinery' package. Install dependencies for "
            "vendor/Document-Intelligence-Refinery (for example with uv sync there)."
        ) from exc

    t0 = time.time()
    _LOG.debug("week3_run_triage path=%s", file_path)
    profile = run_triage(file_path, save=False)
    _LOG.debug(
        "week3_profile doc_id=%s layout=%s domain=%s origin=%s notes=%s",
        getattr(profile, "doc_id", None),
        getattr(profile, "layout_complexity", None),
        getattr(profile, "domain_hint", None),
        getattr(profile, "origin_type", None),
        (profile.classification_notes or [])[:3] if getattr(profile, "classification_notes", None) else [],
    )
    fallback_note = None
    try:
        has_google = bool(effective_google_generative_key())
        _LOG.info(
            "week3_run_extraction path=%s prefer_gemini_env=%s (google_key_usable=%s)",
            file_path,
            has_google,
            has_google,
        )
        with _prefer_gemini_provider():
            extracted_doc = run_extraction(file_path, profile=profile, save=False)
    except Exception as exc:
        # Robust fallback: if layout/vision dependencies are unavailable, run
        # FastText extraction so the ledger pipeline can still proceed.
        msg = str(exc)
        if ("Docling is required" in msg) or ("VisionExtractor" in msg):
            _LOG.warning(
                "week3_extraction_fallback reason=%s msg=%s",
                type(exc).__name__,
                msg[:300],
            )
            rules = load_extraction_rules(None)
            fast = FastTextExtractor(extraction_rules=rules)
            extracted_doc, _ = fast.extract(file_path, profile)
            extracted_doc.strategy_used = "fast_text_fallback"
            fallback_note = f"Fallback to FastText due to upstream dependency issue: {type(exc).__name__}"
        else:
            _LOG.exception("week3_extraction_failed path=%s", file_path)
            raise
    chunks, table_count = _collect_text_and_tables(extracted_doc)
    facts = _extract_facts_from_chunks(chunks, document_type=document_type)
    if fallback_note:
        facts["extraction_notes"].append(fallback_note)
    if getattr(extracted_doc, "status", "completed") != "completed":
        facts["extraction_notes"].append(
            f"Week 3 extraction status: {getattr(extracted_doc, 'status', 'unknown')}"
        )
    processing_ms = int((time.time() - t0) * 1000)
    result = ExtractionAdapterResult(
        facts=facts,
        raw_text_length=sum(len(chunk) for chunk in chunks),
        tables_extracted=table_count,
        processing_ms=processing_ms,
        strategy_used=str(getattr(extracted_doc, "strategy_used", "unknown")),
        status=str(getattr(extracted_doc, "status", "unknown")),
    )
    _LOG.info(
        "week3_pipeline_done path=%s strategy=%s status=%s ms=%s tables=%s text_len=%s critical_notes=%s",
        file_path,
        result["strategy_used"],
        result["status"],
        processing_ms,
        table_count,
        result["raw_text_length"],
        len(facts.get("extraction_notes") or []),
    )
    _LOG.debug(
        "week3_facts_summary revenue=%s net_income=%s assets=%s liabilities=%s",
        facts.get("total_revenue"),
        facts.get("net_income"),
        facts.get("total_assets"),
        facts.get("total_liabilities"),
    )
    return result


async def extract_financial_facts(file_path: str | Path, document_type: str) -> ExtractionAdapterResult:
    """
    Async adapter entrypoint used by ledger agents.

    Args:
        file_path: path to a PDF document.
        document_type: expected type (e.g. income_statement, balance_sheet).
    """
    path = Path(file_path)
    if not path.exists():
        raise FileNotFoundError(f"Document path does not exist: {path}")
    if path.suffix.lower() != ".pdf":
        raise ValueError(f"Week 3 extraction supports PDF input, got: {path.suffix}")
    _LOG.debug("extract_financial_facts_async path=%s type=%s", path, document_type)
    return await asyncio.to_thread(_run_week3_pipeline, path, document_type)

