"""Strategy B: Layout-aware extraction via Docling with DoclingDocumentAdapter."""

from pathlib import Path
from typing import Any, List, Optional

from refinery.models import DocumentProfile, Bbox, ExtractedDocument, ExtractedPage, ExtractedTable, ExtractedFigure, TextBlock
from refinery.strategies.base import load_extraction_rules
from refinery.strategies.config_models import ExtractionRules


def _bbox_from_docling(prov: Any) -> Optional[Bbox]:
    if prov is None:
        return None
    if hasattr(prov, "bbox") and prov.bbox is not None:
        b = prov.bbox
        if hasattr(b, "l") and hasattr(b, "t"):
            return Bbox(x0=b.l, top=b.t, x1=getattr(b, "r", b.l), bottom=getattr(b, "b", b.t))
        if isinstance(b, (list, tuple)) and len(b) >= 4:
            return Bbox(x0=b[0], top=b[1], x1=b[2], bottom=b[3])
    return None


def _page_from_docling(prov: Any) -> int:
    if prov is None:
        return 0
    if hasattr(prov, "page_no"):
        return int(prov.page_no) - 1 if prov.page_no else 0
    if hasattr(prov, "page_index"):
        return int(prov.page_index)
    return 0


class DoclingDocumentAdapter:
    """Maps Docling document to normalized schema with content_hash and reading_order."""

    def __init__(self, docling_document: Any, page_count: int, doc_id: str, source_path: Optional[Path] = None):
        self.doc = docling_document
        self.page_count = page_count
        self.doc_id = doc_id
        self.source_path = source_path
        self._page_index_to_page: dict[int, ExtractedPage] = {i: ExtractedPage(page_index=i, text_blocks=[], tables=[], figures=[], reading_order=[]) for i in range(page_count)}

    def _ensure_pages(self) -> None:
        doc = self.doc
        elem_id = 0
        if hasattr(doc, "texts") and doc.texts:
            for t in doc.texts:
                text = getattr(t, "text", None) or str(getattr(t, "content", ""))
                prov = getattr(t, "prov", None) or getattr(t, "provenance", None)
                bbox = _bbox_from_docling(prov)
                page_idx = _page_from_docling(prov)
                tb = TextBlock.from_text_bbox(text, bbox, page_index=page_idx)
                eid = f"e{elem_id}"
                elem_id += 1
                if page_idx in self._page_index_to_page:
                    self._page_index_to_page[page_idx].text_blocks.append(tb)
                    self._page_index_to_page[page_idx].reading_order.append(eid)
        if hasattr(doc, "tables") and doc.tables:
            for t in doc.tables:
                prov = getattr(t, "prov", None) or getattr(t, "provenance", None)
                page_idx = _page_from_docling(prov)
                data: List[List[Any]] = []
                if hasattr(t, "export_to_dataframe"):
                    try:
                        data = t.export_to_dataframe().values.tolist()
                    except Exception:
                        pass
                if not data:
                    data = getattr(t, "data", [])
                bbox = _bbox_from_docling(prov)
                et = ExtractedTable.from_data_bbox(data, bbox, page_index=page_idx)
                eid = f"e{elem_id}"
                elem_id += 1
                if page_idx in self._page_index_to_page:
                    self._page_index_to_page[page_idx].tables.append(et)
                    self._page_index_to_page[page_idx].reading_order.append(eid)
        if hasattr(doc, "pictures") and doc.pictures:
            for p in doc.pictures:
                prov = getattr(p, "prov", None) or getattr(p, "provenance", None)
                page_idx = _page_from_docling(prov)
                bbox = _bbox_from_docling(prov)
                caption = getattr(p, "caption", None) or getattr(p, "title", None)
                fig = ExtractedFigure(bbox=bbox, page_index=page_idx, caption=caption)
                eid = f"e{elem_id}"
                elem_id += 1
                if page_idx in self._page_index_to_page:
                    self._page_index_to_page[page_idx].figures.append(fig)
                    self._page_index_to_page[page_idx].reading_order.append(eid)
        if elem_id == 0 and hasattr(doc, "export_to_dict"):
            try:
                self._parse_export_dict(doc.export_to_dict(), elem_id)
            except Exception:
                pass

    def _parse_export_dict(self, d: dict, start_id: int) -> None:
        if not isinstance(d, dict):
            return
        body = d.get("body") or d.get("content") or d.get("items") or []
        if isinstance(body, dict):
            body = body.get("children", body.get("items", []))
        if not isinstance(body, list):
            return
        elem_id = start_id
        for item in body:
            if not isinstance(item, dict):
                continue
            kind = item.get("type") or item.get("label") or ""
            page_idx = item.get("page_no", item.get("page_index", 0))
            if isinstance(page_idx, int) and page_idx > 0:
                page_idx -= 1
            text = item.get("text") or item.get("content") or ""
            if "text" in kind.lower() or text:
                bbox = None
                if "bbox" in item:
                    b = item["bbox"]
                    if isinstance(b, (list, tuple)) and len(b) >= 4:
                        bbox = Bbox(x0=b[0], top=b[1], x1=b[2], bottom=b[3])
                tb = TextBlock.from_text_bbox(text, bbox, page_index=page_idx)
                eid = f"e{elem_id}"
                elem_id += 1
                if page_idx in self._page_index_to_page:
                    self._page_index_to_page[page_idx].text_blocks.append(tb)
                    self._page_index_to_page[page_idx].reading_order.append(eid)
            if "table" in kind.lower():
                data = item.get("data", item.get("rows", []))
                bbox = None
                if "bbox" in item:
                    b = item["bbox"]
                    if isinstance(b, (list, tuple)) and len(b) >= 4:
                        bbox = Bbox(x0=b[0], top=b[1], x1=b[2], bottom=b[3])
                et = ExtractedTable.from_data_bbox(data, bbox, page_index=page_idx)
                eid = f"e{elem_id}"
                elem_id += 1
                if page_idx in self._page_index_to_page:
                    self._page_index_to_page[page_idx].tables.append(et)
                    self._page_index_to_page[page_idx].reading_order.append(eid)

    def to_extracted_document(self) -> ExtractedDocument:
        self._ensure_pages()
        pages = [self._page_index_to_page[i] for i in range(self.page_count) if i in self._page_index_to_page]
        if not pages:
            pages = list(self._page_index_to_page.values())
        return ExtractedDocument(
            doc_id=self.doc_id,
            source_path=self.source_path,
            page_count=len(pages),
            strategy_used="layout",
            pages=sorted(pages, key=lambda p: p.page_index),
            status="completed",
        )


class LayoutExtractor:
    """Extract using Docling. Low confidence if profile is table_heavy but 0 tables detected."""

    def __init__(self, extraction_rules: ExtractionRules | None = None):
        self.rules = extraction_rules or load_extraction_rules()

    def extract(self, pdf_path: Path, profile: DocumentProfile) -> tuple[ExtractedDocument, float]:
        try:
            from docling.document_converter import DocumentConverter
        except ImportError as e:
            raise RuntimeError("Docling is required for LayoutExtractor") from e

        converter = DocumentConverter()
        result = converter.convert(str(pdf_path))
        docling_doc = getattr(result, "document", None)
        if docling_doc is None:
            return ExtractedDocument(
                doc_id=profile.doc_id or "",
                source_path=pdf_path,
                page_count=0,
                strategy_used="layout",
                pages=[],
                status="partial_failure",
            ), 0.0

        page_count = 1
        if hasattr(result, "pages") and result.pages is not None:
            try:
                page_count = len(result.pages)
            except TypeError:
                pass
        if page_count <= 0 and hasattr(docling_doc, "page_count"):
            page_count = getattr(docling_doc, "page_count", 1) or 1
        if page_count <= 0:
            import pdfplumber
            with pdfplumber.open(pdf_path) as pdf:
                page_count = len(pdf.pages)

        adapter = DoclingDocumentAdapter(docling_doc, page_count=page_count, doc_id=profile.doc_id or "", source_path=pdf_path)
        doc = adapter.to_extracted_document()
        total_tables = sum(len(p.tables) for p in doc.pages)
        confidence = 0.0 if (profile.layout_complexity == "table_heavy" and total_tables == 0) else 1.0
        return doc, confidence
