"""Fact table: key-value facts extracted from documents for structured query."""

from refinery.facts.schema import FactRow
from refinery.facts.store import FactStore, get_default_store_path
from refinery.facts.extractor import extract_facts_from_ldus, should_run_fact_extraction

__all__ = [
    "FactRow",
    "FactStore",
    "get_default_store_path",
    "extract_facts_from_ldus",
    "should_run_fact_extraction",
]
