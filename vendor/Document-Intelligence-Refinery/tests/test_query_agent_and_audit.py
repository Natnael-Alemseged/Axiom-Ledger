"""Tests for query agent, audit, provenance, and PageIndex."""

import json
from pathlib import Path

import pytest

from refinery.models import ProvenanceChain, SourceCitation
from refinery.pageindex.models import SectionNode, flatten_section_nodes
from refinery.agents.chunker import ChunkingEngine, ChunkValidator
from refinery.agents.indexer import build_page_index
from refinery.agents.audit import verify_claim
from refinery.facts import FactStore, FactRow, get_default_store_path, should_run_fact_extraction
from refinery.models.document_profile import DocumentProfile


def test_source_citation_model():
    c = SourceCitation(
        document_name="doc.pdf",
        page_number=1,
        bbox={"x0": 0, "top": 0, "x1": 100, "bottom": 100},
        content_hash="abc",
        text="Revenue was 42 million USD.",
    )
    assert c.document_name == "doc.pdf"
    assert c.page_number == 1
    assert c.content_hash == "abc"
    assert c.text == "Revenue was 42 million USD."


def test_provenance_chain_citations():
    chain = ProvenanceChain(citations=[SourceCitation(document_name="d", page_number=1)], source_document="d")
    assert len(chain.citations) == 1
    assert chain.citations[0].document_name == "d"


def test_section_node_title_and_data_types():
    node = SectionNode(section_label="3.1", title="Section 3.1", page_start=5, page_end=7, data_types_present=["table", "text"])
    assert node.title == "Section 3.1"
    assert node.page_start == 5
    assert node.data_types_present == ["table", "text"]
    assert node.child_sections == []


def test_flatten_section_nodes():
    root = SectionNode(section_label="1", title="One", children=[SectionNode(section_label="1.1", title="One.1")])
    flat = flatten_section_nodes([root])
    assert len(flat) == 2
    assert flat[0].section_label == "1"
    assert flat[1].section_label == "1.1"


def test_verify_claim_not_found():
    # verify_claim uses run_query which needs ChromaDB; skip if unavailable (e.g. Python 3.14 / ChromaDB compat)
    try:
        out = verify_claim("nonexistent claim xyz", doc_id=None)
    except RuntimeError as e:
        if "ChromaDB" in str(e) or "not available" in str(e).lower():
            pytest.skip("ChromaDB/VectorStore not available")
        raise
    assert out["status"] in ("not_found", "verified", "unverifiable")
    assert "evidence" in out


def test_fact_store_insert_and_query(tmp_path):
    store = FactStore(db_path=tmp_path / "test.db")
    store.insert(FactRow(doc_id="d1", page_ref=0, key="revenue", value=42, unit="million"))
    rows = store.query_facts(doc_id="d1", limit=10)
    assert len(rows) == 1
    assert rows[0].key == "revenue"
    assert rows[0].value == 42


def test_should_run_fact_extraction():
    assert should_run_fact_extraction(None) is False
    profile = DocumentProfile(domain_hint="financial")
    assert should_run_fact_extraction(profile) is True
    profile2 = DocumentProfile(domain_hint="general", layout_complexity="table_heavy")
    assert should_run_fact_extraction(profile2) is True
