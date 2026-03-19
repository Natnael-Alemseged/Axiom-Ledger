"""ChunkingEngine: ExtractedDocument -> List[LDU] with five chunking rules."""

import hashlib
import json
import logging
import re
from typing import Any, Dict, List, Optional, Tuple

from refinery.models import (
    Bbox,
    ExtractedDocument,
    ExtractedPage,
    ExtractedTable,
    LDU,
    LDUKind,
)
from refinery.strategies.config_models import ChunkingRules

from refinery.chunking.config import load_chunking_rules
from refinery.chunking.validator import ChunkValidator

logger = logging.getLogger(__name__)

# Tiktoken encoding (cl100k_base); lazy init
_tiktoken_encoding = None


def _get_tiktoken_encoding():
    global _tiktoken_encoding
    if _tiktoken_encoding is None:
        try:
            import tiktoken
            _tiktoken_encoding = tiktoken.get_encoding("cl100k_base")
        except Exception as e:
            logger.warning("tiktoken not available: %s; using heuristic token count", e)
    return _tiktoken_encoding


def _token_count(text: str, lang_fallback: bool = False) -> int:
    """Token count via tiktoken cl100k_base; fallback to heuristic for non-Latin/multilingual."""
    enc = _get_tiktoken_encoding()
    if enc is None:
        return max(0, len(text) // 4)
    try:
        n = len(enc.encode(text))
        if lang_fallback and n == 0 and text.strip():
            return max(0, len(text) // 4)
        return n
    except Exception:
        return max(0, len(text) // 4)


def _content_hash_ldu(content: Any, bbox: Optional[Dict[str, float]] = None) -> str:
    """MD5 of content + bbox for LDU (same pattern as extraction_schema)."""
    payload = json.dumps(content, sort_keys=True) if not isinstance(content, str) else content
    if bbox:
        payload += json.dumps(bbox, sort_keys=True)
    return hashlib.md5(payload.encode("utf-8")).hexdigest()


def _bbox_to_dict(bbox: Optional[Bbox]) -> Optional[Dict[str, float]]:
    if bbox is None:
        return None
    return bbox.model_dump()


def _global_reading_order(pages: List[ExtractedPage]) -> List[Tuple[str, Any, int]]:
    """Build (type, element, page_index) in global reading order. Type in ('text', 'table', 'figure')."""
    out: List[Tuple[str, Any, int]] = []
    for page in sorted(pages, key=lambda p: p.page_index):
        elements: List[Tuple[str, Any, int]] = []
        for tb in page.text_blocks:
            elements.append(("text", tb, page.page_index))
        for t in page.tables:
            elements.append(("table", t, page.page_index))
        for f in page.figures:
            elements.append(("figure", f, page.page_index))
        if page.reading_order and len(page.reading_order) == len(elements):
            out.extend(elements)
        else:
            out.extend(elements)
    return out


def _detect_section_heading(text: str, font_size: Optional[float], patterns: List[str]) -> Optional[str]:
    """Return section label if text looks like a heading (regex or larger font)."""
    line = (text or "").strip().split("\n")[0] if text else ""
    if not line:
        return None
    for pat in patterns:
        try:
            if re.search(pat, line):
                return line
        except re.error:
            continue
    if font_size is not None and font_size >= 14 and len(line) < 200:
        return line
    return None


def _is_numbered_list_line(text: str) -> bool:
    return bool(re.match(r"^\s*\d+[.)]\s", text))


def _table_content_to_string(table: ExtractedTable) -> str:
    """String representation of table for content and token count."""
    rows = table.data or []
    if not rows:
        return ""
    lines = [" | ".join(str(c) for c in row) for row in rows]
    return "\n".join(lines)


class ChunkingEngine:
    """Converts ExtractedDocument into List[LDU] with five chunking rules enforced."""

    def __init__(self, rules: Optional[ChunkingRules] = None):
        self.rules = rules or load_chunking_rules()
        self.validator = ChunkValidator()

    def chunk(self, doc: ExtractedDocument, on_violation: Optional[str] = None) -> List[LDU]:
        """
        Convert ExtractedDocument to List[LDU]. Applies all five chunking rules.
        on_violation: "raise" | "log" (default from rules).
        """
        on_violation = on_violation or self.rules.on_violation
        ordered = _global_reading_order(doc.pages)
        section_patterns = self.rules.section_heading_patterns or []
        current_section: Optional[str] = None
        table_index = 0
        figure_index = 0
        ldus: List[LDU] = []
        ref_map: Dict[str, str] = {}  # "Table 1" -> chunk_id, "Figure 2" -> chunk_id

        i = 0
        while i < len(ordered):
            typ, element, page_idx = ordered[i]

            if typ == "table":
                table = element
                content_str = _table_content_to_string(table)
                token_count = _token_count(content_str)
                bbox = _bbox_to_dict(table.bbox)
                content_hash = table.content_hash or _content_hash_ldu(table.data, bbox)
                table_index += 1
                label = f"Table {table_index}"
                chunk_id = f"{doc.doc_id}_chunk_{len(ldus)}"
                if content_hash:
                    chunk_id = f"{doc.doc_id}_{content_hash[:8]}"
                # Uniqueness: if collision, append index
                seen = {ldu.chunk_id for ldu in ldus}
                if chunk_id in seen:
                    chunk_id = f"{doc.doc_id}_chunk_{len(ldus)}"
                ref_map[label] = chunk_id
                if table.caption:
                    ref_map[table.caption.strip()] = chunk_id
                ldu = LDU(
                    kind="table",
                    content=table.data,
                    page_refs=[page_idx],
                    token_count=token_count,
                    bbox=bbox,
                    content_hash=content_hash,
                    parent_section=current_section,
                    chunk_id=chunk_id,
                    metadata={"caption": table.caption} if table.caption else {},
                )
                ldus.append(ldu)
                i += 1
                continue

            if typ == "figure":
                fig = element
                caption = fig.caption or ""
                content_str = f"Figure. {caption}" if caption else "Figure."
                token_count = _token_count(content_str)
                bbox = _bbox_to_dict(fig.bbox)
                content_hash = _content_hash_ldu(content_str, bbox)
                figure_index += 1
                label = f"Figure {figure_index}"
                chunk_id = f"{doc.doc_id}_{content_hash[:8]}"
                seen = {ldu.chunk_id for ldu in ldus}
                if chunk_id in seen:
                    chunk_id = f"{doc.doc_id}_chunk_{len(ldus)}"
                ref_map[label] = chunk_id
                if caption:
                    ref_map[caption.strip()] = chunk_id
                ldu = LDU(
                    kind="figure",
                    content=content_str,
                    page_refs=[page_idx],
                    token_count=token_count,
                    bbox=bbox,
                    content_hash=content_hash,
                    parent_section=current_section,
                    chunk_id=chunk_id,
                    metadata={"caption": caption},
                )
                ldus.append(ldu)
                i += 1
                continue

            # text: possibly heading, numbered list, or plain text
            assert typ == "text"
            tb = element
            text = tb.text or ""
            font_size = tb.font_info.size if tb.font_info else None
            heading = _detect_section_heading(text, font_size, section_patterns)
            if heading:
                current_section = heading
            if _is_numbered_list_line(text):
                # Collect contiguous numbered list lines
                list_lines = [text]
                list_pages = [page_idx]
                j = i + 1
                while j < len(ordered):
                    t2, elem2, p2 = ordered[j]
                    if t2 != "text":
                        break
                    t2_text = (elem2.text or "").strip()
                    if not _is_numbered_list_line(t2_text):
                        break
                    list_lines.append(t2_text)
                    if p2 not in list_pages:
                        list_pages.append(p2)
                    j += 1
                    combined = "\n".join(list_lines)
                    if _token_count(combined) > self.rules.max_tokens_for_list:
                        list_lines = list_lines[:-1]
                        list_pages = list_pages[:-1] if len(list_pages) > 1 else list_pages
                        j -= 1
                        break
                combined = "\n".join(list_lines)
                token_count = _token_count(combined)
                bbox = _bbox_to_dict(tb.bbox)
                content_hash = _content_hash_ldu(combined, bbox)
                chunk_id = f"{doc.doc_id}_{content_hash[:8]}"
                seen = {ldu.chunk_id for ldu in ldus}
                if chunk_id in seen:
                    chunk_id = f"{doc.doc_id}_chunk_{len(ldus)}"
                ldus.append(
                    LDU(
                        kind="list",
                        content=combined,
                        page_refs=sorted(set(list_pages)),
                        token_count=token_count,
                        bbox=bbox,
                        content_hash=content_hash,
                        parent_section=current_section,
                        chunk_id=chunk_id,
                        metadata={},
                    )
                )
                i = j
                continue

            # Plain text: merge short adjacent, split by max_tokens_per_chunk
            text_lines = [text]
            text_pages = [page_idx]
            j = i + 1
            while j < len(ordered):
                t2, elem2, p2 = ordered[j]
                if t2 != "text":
                    break
                t2_text = (elem2.text or "").strip()
                if _is_numbered_list_line(t2_text) or _detect_section_heading(t2_text, getattr(elem2.font_info, "size", None) if hasattr(elem2, "font_info") else None, section_patterns):
                    break
                next_merged = "\n\n".join(text_lines + [t2_text])
                if _token_count(next_merged) > self.rules.max_tokens_per_chunk:
                    break
                text_lines.append(t2_text)
                if p2 not in text_pages:
                    text_pages.append(p2)
                j += 1
                # Keep merging if still below merge threshold (avoid tiny LDUs)
                if self.rules.merge_short_text_chars and _token_count(next_merged) * 4 < self.rules.merge_short_text_chars:
                    continue
                # Otherwise we have a reasonable chunk; could stop or keep going until max_tokens
                if _token_count(next_merged) >= self.rules.max_tokens_per_chunk * 0.8:
                    break
            merged = "\n\n".join(text_lines)
            token_count = _token_count(merged)
            bbox = _bbox_to_dict(tb.bbox)
            content_hash = _content_hash_ldu(merged, bbox)
            chunk_id = f"{doc.doc_id}_{content_hash[:8]}"
            seen = {ldu.chunk_id for ldu in ldus}
            if chunk_id in seen:
                chunk_id = f"{doc.doc_id}_chunk_{len(ldus)}"
            ldus.append(
                LDU(
                    kind="text",
                    content=merged,
                    page_refs=sorted(set(text_pages)),
                    token_count=token_count,
                    bbox=bbox,
                    content_hash=content_hash,
                    parent_section=current_section,
                    chunk_id=chunk_id,
                    metadata={},
                )
            )
            i = j

        # Post-process: cross-refs
        for ldu in ldus:
            content = ldu.content if isinstance(ldu.content, str) else json.dumps(ldu.content) if ldu.content is not None else ""
            cross_refs: List[Dict[str, Any]] = []
            for pat_str in self.rules.cross_ref_patterns or []:
                try:
                    pat = re.compile(pat_str)
                    for m in pat.finditer(content):
                        raw = m.group(0)
                        target = ref_map.get(raw)
                        if m.lastindex and not target:
                            target = ref_map.get(f"Table {m.group(1)}") or ref_map.get(f"Figure {m.group(1)}")
                        cross_refs.append({"text": raw, "target_chunk_id": target} if target else {"text": raw})
                except (IndexError, re.error):
                    continue
            if cross_refs:
                ldu.metadata = dict(ldu.metadata or {})
                ldu.metadata["cross_refs"] = cross_refs

        result = self.validator.validate(ldus, max_tokens_for_list=getattr(self.rules, "max_tokens_for_list", None))
        if not result.valid:
            for v in result.violations:
                logger.warning("ChunkValidator: %s - %s (chunk_id=%s)", v.rule_id, v.message, v.chunk_id)
            if on_violation == "raise":
                raise ValueError(f"ChunkValidator found {len(result.violations)} violation(s): {result.violations[0].message}")

        return ldus
