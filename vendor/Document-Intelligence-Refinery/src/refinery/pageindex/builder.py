"""PageIndex tree builder: hierarchical sections, LLM summaries, key_entities, data_types_present."""

import json
import logging
import re
from pathlib import Path
from typing import List, Optional, Tuple

from refinery.models import LDU
from refinery.strategies.base import load_extraction_rules
from refinery.strategies.config_models import ExtractionRules, PageIndexConfig

from refinery.pageindex.models import SectionNode

logger = logging.getLogger(__name__)

# Lazy token count for truncation
_tiktoken_enc = None


def _token_count_approx(text: str) -> int:
    try:
        import tiktoken
        global _tiktoken_enc
        if _tiktoken_enc is None:
            _tiktoken_enc = tiktoken.get_encoding("cl100k_base")
        return len(_tiktoken_enc.encode(text))
    except Exception:
        return len(text) // 4


def _truncate_to_tokens(text: str, max_tokens: int) -> str:
    """Truncate text to approximately max_tokens."""
    if _token_count_approx(text) <= max_tokens:
        return text
    low, high = 0, len(text)
    while low < high - 1:
        mid = (low + high) // 2
        if _token_count_approx(text[:mid]) <= max_tokens:
            low = mid
        else:
            high = mid
    return text[:low] + " [...]"


def _summarize_with_llm(text: str, model_id: str, max_tokens: int) -> Optional[str]:
    """Call cheap LLM to summarize section text. Returns summary or None on failure."""
    prompt = f"Summarize this section in 2-3 sentences. Section text:\n\n{_truncate_to_tokens(text, max_tokens)}"
    messages = [{"role": "user", "content": prompt}]
    api_key = __import__("os").environ.get("OPENROUTER_API_KEY") or __import__("os").environ.get("OPENROUTER_KEY")
    if not api_key:
        logger.warning("No OPENROUTER_API_KEY; skipping LLM summary")
        return None
    try:
        import httpx
        with httpx.Client(timeout=30.0) as client:
            r = client.post(
                "https://openrouter.ai/api/v1/chat/completions",
                headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
                json={"model": model_id, "messages": messages, "max_tokens": 150},
            )
        r.raise_for_status()
        data = r.json()
        choice = (data.get("choices") or [None])[0]
        if not choice:
            return None
        return (choice.get("message") or {}).get("content", "").strip()
    except Exception as e:
        logger.debug("LLM summary failed: %s", e)
        return None


def _extract_key_entities_regex(text: str) -> List[str]:
    """Lightweight regex-based entity extraction: dates, currency, numbers."""
    entities: List[str] = []
    # Dates: 2024, Q3 2024, etc.
    for m in re.finditer(r"\b(20\d{2})\b", text):
        entities.append(m.group(1))
    for m in re.finditer(r"\bQ[1-4]\s*20\d{2}\b", text, re.IGNORECASE):
        entities.append(m.group(0))
    # Currency: $1.2B, USD 4.2 billion
    for m in re.finditer(r"\$[\d.,]+(?:\s*[BMKbmk])?\b|\d+(?:\.\d+)?\s*(?:million|billion|M|B)\b", text):
        entities.append(m.group(0))
    return list(dict.fromkeys(entities))[:15]  # dedupe, cap


def _ldu_content_text(ldu: LDU) -> str:
    """Extract string content from LDU for section text."""
    if ldu.content is None:
        return ""
    if isinstance(ldu.content, str):
        return ldu.content
    if isinstance(ldu.content, list):
        return " ".join(str(cell) for row in ldu.content for cell in (row if isinstance(row, (list, tuple)) else [row]))
    return str(ldu.content)


def _section_parent(section_label: str) -> Optional[str]:
    """Return parent section label for hierarchy. None = root. E.g. '3.1' -> '3', '3' -> None."""
    if not section_label or section_label == "(root)":
        return None
    # Numbered subsection: 3.1, 3.2 -> parent 3
    m = re.match(r"^(\d+)(\.\d+)*\.?\s*$", section_label.strip())
    if m:
        parts = section_label.strip().split(".")
        if len(parts) >= 2 and parts[0].isdigit():
            return parts[0]
        return None  # top-level number like "3"
    return None


def build_page_index_tree(
    ldus: List[LDU],
    doc_id: str,
    rules: Optional[ExtractionRules] = None,
    persist_path: Optional[Path] = None,
) -> List[SectionNode]:
    """
    Build hierarchical section tree from LDUs: group by parent_section, compute data_types_present,
    optional key_entities, LLM summary per section. Returns list of root SectionNodes (tree).
    Persists to .refinery/pageindex/{doc_id}.json by default.
    """
    rules = rules or load_extraction_rules()
    pageindex_cfg: PageIndexConfig = rules.pageindex
    vision = rules.vision
    model_id = pageindex_cfg.summary_model_id or vision.model_cheap
    max_input = pageindex_cfg.max_input_tokens
    batch_size = pageindex_cfg.batch_size
    key_entities_enabled = getattr(pageindex_cfg, "key_entities_enabled", False)
    data_types_from_ldu = getattr(pageindex_cfg, "data_types_from_ldu", True)

    # Group LDUs by parent_section
    section_to_indices: dict[Optional[str], List[int]] = {}
    for i, ldu in enumerate(ldus):
        sec = ldu.parent_section or "(root)"
        section_to_indices.setdefault(sec, []).append(i)

    # Sort sections by first LDU index
    section_labels_order = sorted(section_to_indices.keys(), key=lambda s: (min(section_to_indices[s]), str(s)))

    # Build flat list of SectionNodes with all fields
    flat_nodes: List[Tuple[str, SectionNode]] = []
    for section_label in section_labels_order:
        indices = section_to_indices[section_label]
        if not indices:
            continue
        text_parts = [_ldu_content_text(ldus[i]) for i in indices]
        section_text = "\n\n".join(p for p in text_parts if p.strip())
        summary = _summarize_with_llm(section_text, model_id, max_input) if section_text.strip() else None
        page_refs: List[int] = []
        for i in indices:
            page_refs.extend(ldus[i].page_refs or [])
        page_min, page_max = (min(page_refs), max(page_refs)) if page_refs else (None, None)
        # 1-based for display
        page_start = (page_min + 1) if page_min is not None else None
        page_end = (page_max + 1) if page_max is not None else None
        page_range = [page_min, page_max] if page_min is not None and page_max is not None else None

        data_types_present: List[str] = []
        if data_types_from_ldu:
            kinds = set()
            for i in indices:
                k = ldus[i].kind
                if k in ("table", "figure", "text", "heading", "list", "other"):
                    kinds.add(k)
            data_types_present = sorted(kinds)
            if section_text.strip() and re.search(r"\\\(|\\\)|\\\\frac|\\\\sum|equation", section_text):
                data_types_present.append("equation")

        key_entities: List[str] = []
        if key_entities_enabled and section_text.strip():
            key_entities = _extract_key_entities_regex(section_text)

        title = section_label if section_label != "(root)" else "Document"
        node = SectionNode(
            section_label=section_label,
            title=title,
            page_start=page_start,
            page_end=page_end,
            page_range=page_range,
            summary=summary,
            ldu_range=[min(indices), max(indices) + 1],
            key_entities=key_entities,
            data_types_present=data_types_present,
            children=[],
        )
        flat_nodes.append((section_label, node))

    # Build hierarchy: assign children to parents
    label_to_node = {label: node for label, node in flat_nodes}
    for section_label, node in flat_nodes:
        parent_label = _section_parent(section_label)
        if parent_label is not None and parent_label in label_to_node:
            label_to_node[parent_label].children.append(node)
        # else: root

    roots = [node for label, node in flat_nodes if _section_parent(label) is None]

    # Persist to .refinery/pageindex/ (or persist_path / config output_dir)
    out_dir = Path(".refinery/pageindex")
    if getattr(pageindex_cfg, "output_dir", None):
        out_dir = Path(pageindex_cfg.output_dir)
    if persist_path is not None:
        out_dir = persist_path.parent
        out_path = persist_path
    elif doc_id:
        out_dir.mkdir(parents=True, exist_ok=True)
        out_path = out_dir / f"{doc_id}.json"
    else:
        out_path = None

    if out_path is not None:
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(
            json.dumps([n.model_dump() for n in roots], indent=2),
            encoding="utf-8",
        )
        logger.info("PageIndex tree saved to %s", out_path)

    return roots
