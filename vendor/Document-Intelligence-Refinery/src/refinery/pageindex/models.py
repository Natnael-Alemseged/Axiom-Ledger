"""Section tree node with optional LLM summary. Persisted as .refinery/pageindex/{doc_id}.json."""

from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field


class SectionNode(BaseModel):
    """A node in the PageIndex section tree: title, page span, summary, key_entities, data_types_present, children."""

    section_label: str = ""  # backward compat; prefer title
    title: str = ""  # section title (alias or primary)
    page_start: Optional[int] = None  # 1-based start page for display
    page_end: Optional[int] = None  # 1-based end page
    summary: Optional[str] = None
    ldu_range: Optional[List[int]] = None  # [start_idx, end_idx] into List[LDU]
    page_range: Optional[List[int]] = None  # [min_page, max_page] for display (backward compat)
    key_entities: List[str] = Field(default_factory=list)  # named entities in section
    data_types_present: List[str] = Field(default_factory=list)  # e.g. ["table", "figure", "equation"]
    children: List["SectionNode"] = Field(default_factory=list)
    metadata: Dict[str, Any] = Field(default_factory=dict)

    @property
    def child_sections(self) -> List["SectionNode"]:
        """Alias for children (smart ToC / plan naming). Not serialized."""
        return self.children


SectionNode.model_rebuild()


def flatten_section_nodes(roots: List["SectionNode"]) -> List["SectionNode"]:
    """Flatten a tree of section nodes (roots with children) to a depth-first list for retrieval."""
    out: List[SectionNode] = []
    for n in roots:
        out.append(n)
        if n.children:
            out.extend(flatten_section_nodes(n.children))
    return out
