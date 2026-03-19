"""Tests for chunking engine and validator."""

import pytest

from refinery.models import (
    Bbox,
    ExtractedDocument,
    ExtractedPage,
    ExtractedTable,
    ExtractedFigure,
    TextBlock,
)
from refinery.chunking import ChunkingEngine, ChunkValidator, load_chunking_rules, RuleViolation, ValidationResult


def test_load_chunking_rules():
    """Chunking rules load from config."""
    rules = load_chunking_rules()
    assert rules.max_tokens_per_chunk == 512
    assert rules.max_tokens_for_list == 512
    assert rules.on_violation in ("raise", "log")


def test_chunking_engine_minimal_doc():
    """ChunkingEngine on minimal ExtractedDocument: one text, one table, one figure."""
    tb = TextBlock.from_text_bbox(
        "Hello world",
        Bbox(x0=0, top=0, x1=100, bottom=20),
        page_index=0,
    )
    tbl = ExtractedTable.from_data_bbox(
        [["A", "B"], ["1", "2"]],
        Bbox(x0=0, top=30, x1=100, bottom=60),
        page_index=0,
        caption="Table 1",
    )
    fig = ExtractedFigure(
        bbox=Bbox(x0=0, top=70, x1=100, bottom=120),
        page_index=0,
        caption="Sample figure",
    )
    page = ExtractedPage(
        page_index=0,
        text_blocks=[tb],
        tables=[tbl],
        figures=[fig],
        reading_order=["e0", "e1", "e2"],
    )
    doc = ExtractedDocument(
        doc_id="test",
        page_count=1,
        strategy_used="test",
        pages=[page],
    )
    engine = ChunkingEngine()
    ldus = engine.chunk(doc)
    assert len(ldus) == 3
    kinds = {l.kind for l in ldus}
    assert "table" in kinds
    assert "figure" in kinds
    assert "text" in kinds or "list" in kinds
    for l in ldus:
        assert l.chunk_id
        assert l.content_hash
        assert l.token_count >= 0
        assert l.page_refs == [0]
    table_ldu = next(l for l in ldus if l.kind == "table")
    assert table_ldu.content == [["A", "B"], ["1", "2"]]
    figure_ldu = next(l for l in ldus if l.kind == "figure")
    assert figure_ldu.metadata.get("caption") == "Sample figure"


def test_chunk_validator_valid():
    """ChunkValidator accepts valid list."""
    from refinery.models import LDU

    ldus = [
        LDU(kind="table", content=[["H"], ["r1"]], chunk_id="t1", metadata={}),
        LDU(kind="figure", content="Fig.", chunk_id="f1", metadata={"caption": "Cap"}),
    ]
    v = ChunkValidator()
    result = v.validate(ldus)
    assert result.valid
    assert len(result.violations) == 0


def test_chunk_validator_figure_caption_violation():
    """ChunkValidator flags figure without caption in metadata."""
    from refinery.models import LDU

    ldus = [
        LDU(kind="figure", content="", chunk_id="f1", metadata={}),
    ]
    v = ChunkValidator()
    result = v.validate(ldus)
    assert not result.valid
    assert any(v.rule_id == "figure_caption" for v in result.violations)


def test_chunk_validator_table_empty_violation():
    """ChunkValidator flags table LDU with no content."""
    from refinery.models import LDU

    ldus = [
        LDU(kind="table", content=[], chunk_id="t1", metadata={}),
    ]
    v = ChunkValidator()
    result = v.validate(ldus)
    assert not result.valid
    assert any(v.rule_id == "table_header" for v in result.violations)
