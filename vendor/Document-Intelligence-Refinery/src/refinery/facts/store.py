"""SQLite-backed fact store: insert facts, run read-only SQL, return rows with provenance."""

import json
import logging
import sqlite3
from pathlib import Path
from typing import Any, List, Optional

from refinery.facts.schema import FactRow

logger = logging.getLogger(__name__)

DEFAULT_STORE_PATH = Path(".refinery/fact_store.db")


def get_default_store_path() -> Path:
    return DEFAULT_STORE_PATH


TABLE_DDL = """
CREATE TABLE IF NOT EXISTS facts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    doc_id TEXT NOT NULL,
    page_ref INTEGER NOT NULL,
    key TEXT NOT NULL,
    value TEXT,
    unit TEXT,
    bbox TEXT,
    content_hash TEXT,
    source_ldu_id TEXT,
    created_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_facts_doc_id ON facts(doc_id);
CREATE INDEX IF NOT EXISTS idx_facts_key ON facts(key);
"""


class FactStore:
    """SQLite store for key-value facts with provenance."""

    def __init__(self, db_path: Optional[Path] = None):
        self.db_path = Path(db_path) if db_path else get_default_store_path()
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_schema()

    def _init_schema(self) -> None:
        with sqlite3.connect(str(self.db_path)) as conn:
            conn.executescript(TABLE_DDL)

    def insert(self, row: FactRow) -> None:
        with sqlite3.connect(str(self.db_path)) as conn:
            conn.execute(
                """INSERT INTO facts (doc_id, page_ref, key, value, unit, bbox, content_hash, source_ldu_id, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, datetime('now'))""",
                (
                    row.doc_id,
                    row.page_ref,
                    row.key,
                    json.dumps(row.value) if row.value is not None else None,
                    row.unit,
                    json.dumps(row.bbox) if row.bbox else None,
                    row.content_hash,
                    row.source_ldu_id,
                ),
            )

    def insert_many(self, rows: List[FactRow]) -> None:
        if not rows:
            return
        with sqlite3.connect(str(self.db_path)) as conn:
            for row in rows:
                conn.execute(
                    """INSERT INTO facts (doc_id, page_ref, key, value, unit, bbox, content_hash, source_ldu_id, created_at)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, datetime('now'))""",
                    (
                        row.doc_id,
                        row.page_ref,
                        row.key,
                        json.dumps(row.value) if row.value is not None else None,
                        row.unit,
                        json.dumps(row.bbox) if row.bbox else None,
                        row.content_hash,
                        row.source_ldu_id,
                    ),
                )

    def query_sql(self, sql: str, params: Optional[tuple] = None) -> List[dict]:
        """Run read-only SELECT; return list of dicts with column names. Includes provenance columns."""
        allowed = sql.strip().upper().startswith("SELECT")
        if not allowed:
            raise ValueError("Only SELECT queries are allowed")
        with sqlite3.connect(str(self.db_path)) as conn:
            conn.row_factory = sqlite3.Row
            cur = conn.execute(sql, params or ())
            return [dict(row) for row in cur.fetchall()]

    def query_facts(
        self,
        doc_id: Optional[str] = None,
        key: Optional[str] = None,
        limit: int = 100,
    ) -> List[FactRow]:
        """Convenience: get facts by doc_id and/or key, with provenance."""
        conditions = []
        params: List[Any] = []
        if doc_id:
            conditions.append("doc_id = ?")
            params.append(doc_id)
        if key:
            conditions.append("key = ?")
            params.append(key)
        where_clause = " WHERE " + " AND ".join(conditions) if conditions else ""
        sql = f"SELECT doc_id, page_ref, key, value, unit, bbox, content_hash, source_ldu_id FROM facts{where_clause} ORDER BY doc_id, page_ref LIMIT ?"
        params.append(limit)
        rows = self.query_sql(sql, tuple(params))
        out = []
        for r in rows:
            value = r.get("value")
            if isinstance(value, str):
                try:
                    value = json.loads(value)
                except (TypeError, ValueError):
                    pass
            bbox = r.get("bbox")
            if isinstance(bbox, str):
                try:
                    bbox = json.loads(bbox)
                except (TypeError, ValueError):
                    bbox = None
            out.append(
                FactRow(
                    doc_id=r.get("doc_id", ""),
                    page_ref=int(r.get("page_ref", 0)),
                    key=r.get("key", ""),
                    value=value,
                    unit=r.get("unit"),
                    bbox=bbox,
                    content_hash=r.get("content_hash"),
                    source_ldu_id=r.get("source_ldu_id"),
                )
            )
        return out
