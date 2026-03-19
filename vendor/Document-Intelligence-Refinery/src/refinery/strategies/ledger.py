"""Extraction audit ledger: JSONL log of strategy selection, confidence, cost."""

import json
import os
import time
from pathlib import Path
from typing import Optional

REFINERY_LEDGER_PATH = Path(os.environ.get("REFINERY_EXTRACTION_LEDGER", ".refinery/extraction_ledger.jsonl"))


def _ensure_ledger_dir() -> Path:
    p = REFINERY_LEDGER_PATH.resolve()
    p.parent.mkdir(parents=True, exist_ok=True)
    return p


def log_extraction(
    doc_id: str,
    strategy: str,
    confidence: float,
    cost_usd: float,
    time_ms: float,
    status: str,
    extra: Optional[dict] = None,
) -> None:
    """Append one line to .refinery/extraction_ledger.jsonl."""
    path = _ensure_ledger_dir()
    record = {
        "doc_id": doc_id,
        "strategy": strategy,
        "confidence": confidence,
        "cost_usd": round(cost_usd, 6),
        "time_ms": round(time_ms, 2),
        "status": status,
        "timestamp": time.time(),
    }
    if extra:
        record["extra"] = extra
    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps(record, ensure_ascii=False) + "\n")
