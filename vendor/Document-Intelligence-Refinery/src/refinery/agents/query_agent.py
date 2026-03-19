"""
Query interface agent: LangGraph with three tools (pageindex_navigate, semantic_search, structured_query).
Every answer includes a ProvenanceChain (list of SourceCitation: document_name, page_number, bbox, content_hash, text).
Tool selection: navigational queries -> PageIndex first; numerical/factual -> structured_query first; else all tools.
"""

import json
import logging
import re
from pathlib import Path
from typing import Any, List, Literal, Optional

from refinery.models import ProvenanceChain, SourceCitation
from refinery.pageindex.models import SectionNode, flatten_section_nodes
from refinery.vector_store import VectorStore
from refinery.facts import FactStore, get_default_store_path
from refinery.facts.schema import FactRow

logger = logging.getLogger(__name__)

PAGEINDEX_DIR = Path(".refinery/pageindex")

# Patterns for query-type routing (Master Thinker: tool selection logic)
NAVIGATIONAL_PATTERNS = re.compile(
    r"\b(section|where|which part|chapter|appendix|find the part|locate)\b|\babout\s+(what|which)\b",
    re.IGNORECASE,
)
NUMERICAL_PATTERNS = re.compile(
    r"\b(revenue|profit|expense|amount|total|value|percent|%|quarter|Q[1-4]|million|billion|\$|USD|figures?|numbers?)\b|\d+\.?\d*\s*%",
    re.IGNORECASE,
)


def _query_intent(query: str) -> Literal["navigational", "numerical", "general"]:
    """Classify query for tool selection: navigational -> PageIndex; numerical -> structured_query; else general."""
    q = (query or "").strip()
    if NAVIGATIONAL_PATTERNS.search(q):
        return "navigational"
    if NUMERICAL_PATTERNS.search(q):
        return "numerical"
    return "general"


def pageindex_navigate(
    query: str,
    doc_id: Optional[str] = None,
    pageindex_dir: Optional[Path] = None,
) -> List[SectionNode]:
    """
    Load PageIndex tree for doc_id, traverse by matching query to section title/summary/key_entities.
    Returns matching section(s) with their children and page_start/page_end so retrieval can be narrowed.
    """
    directory = pageindex_dir or PAGEINDEX_DIR
    if not doc_id:
        return []
    path = directory / f"{doc_id}.json"
    if not path.exists():
        logger.debug("No PageIndex at %s", path)
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        roots = [SectionNode.model_validate(n) for n in data]
    except Exception as e:
        logger.warning("Failed to load PageIndex %s: %s", path, e)
        return []
    flat = flatten_section_nodes(roots)
    query_lower = query.lower()
    matches = []
    for node in flat:
        text = " ".join(
            filter(None, [
                node.title,
                node.section_label,
                node.summary,
                " ".join(node.key_entities) if node.key_entities else None,
            ])
        ).lower()
        if query_lower in text or any(q in text for q in query_lower.split()):
            matches.append(node)
    return matches[:10]


def semantic_search(
    query: str,
    doc_id: Optional[str] = None,
    parent_section: Optional[str] = None,
    n_results: int = 5,
    vector_store: Optional[VectorStore] = None,
) -> tuple[List[dict], List[SourceCitation]]:
    """
    Vector search over LDUs. Returns (hits, citations).
    Each hit has id, document, metadata, bbox, content_hash, doc_id, page_refs.
    Citations preserve the retrieved text so audit mode can inspect the evidence.
    """
    store = vector_store or VectorStore()
    embed_fn = store.get_embed_fn()
    where = None
    if doc_id:
        where = {"doc_id": doc_id}
    if parent_section:
        where = (where or {}).copy()
        where["parent_section"] = parent_section
    emb = embed_fn(query)
    hits = store.query(emb, where=where, n_results=n_results)
    citations: List[SourceCitation] = []
    for h in hits:
        doc_name = h.get("doc_id") or h.get("metadata", {}).get("doc_id") or "unknown"
        page_refs = h.get("page_refs") or []
        page_num = (page_refs[0] + 1) if page_refs else None  # 1-based
        citations.append(
            SourceCitation(
                document_name=doc_name,
                page_number=page_num,
                page_numbers=[p + 1 for p in page_refs] if page_refs else [],
                bbox=h.get("bbox"),
                content_hash=h.get("content_hash"),
                text=(h.get("document") or "")[:2000] or None,
            )
        )
    return hits, citations


def structured_query(
    query: str,
    doc_id: Optional[str] = None,
    store: Optional[FactStore] = None,
    limit: int = 20,
) -> tuple[List[FactRow], List[SourceCitation]]:
    """
    Run read-only SQL or key lookup over fact store. Returns (rows, citations with doc_id, page_ref, bbox, content_hash, text).
    """
    s = store or FactStore(get_default_store_path())
    rows = s.query_facts(doc_id=doc_id, limit=limit)
    # Simple filter by query keywords
    query_lower = query.lower()
    filtered = []
    for r in rows:
        if not query_lower or query_lower in str(r.key).lower() or query_lower in str(r.value).lower():
            filtered.append(r)
    citations = [
        SourceCitation(
            document_name=r.doc_id,
            page_number=r.page_ref + 1,
            page_numbers=[r.page_ref + 1],
            bbox=r.bbox,
            content_hash=r.content_hash,
            text=f"{r.key}: {r.value}" + (f" {r.unit}" if r.unit else ""),
        )
        for r in filtered
    ]
    return filtered[:limit], citations


def run_query(
    query: str,
    doc_id: Optional[str] = None,
    vector_store: Optional[VectorStore] = None,
    fact_store: Optional[FactStore] = None,
) -> dict:
    """
    Run the query agent with tool selection by query type:
    - navigational: PageIndex first, then semantic search scoped to matched sections.
    - numerical: structured_query (fact table) first, then semantic search; fact citations prioritized.
    - general: all three tools (PageIndex -> semantic -> facts), merged.
    Returns dict with answer (synthesized text) and provenance_chain (list of SourceCitation).
    """
    intent = _query_intent(query)
    all_citations: List[SourceCitation] = []
    sections = pageindex_navigate(query, doc_id=doc_id)
    parent_section = None
    if sections:
        parent_section = sections[0].section_label if sections[0].section_label != "(root)" else None

    # Tool selection: navigational -> emphasize section-scoped search; numerical -> run facts first
    if intent == "navigational" and parent_section:
        # Section-first: semantic search scoped to matched sections
        hits, search_citations = semantic_search(
            query, doc_id=doc_id, parent_section=parent_section, n_results=5, vector_store=vector_store
        )
        fact_rows, fact_citations = structured_query(query, doc_id=doc_id, store=fact_store, limit=10)
        all_citations.extend(search_citations)
        all_citations.extend(fact_citations)
    elif intent == "numerical":
        # Numerical/factual: structured_query first, then semantic
        fact_rows, fact_citations = structured_query(query, doc_id=doc_id, store=fact_store, limit=10)
        hits, search_citations = semantic_search(
            query, doc_id=doc_id, parent_section=parent_section, n_results=5, vector_store=vector_store
        )
        all_citations.extend(fact_citations)
        all_citations.extend(search_citations)
    else:
        # general: current order (semantic then facts)
        hits, search_citations = semantic_search(
            query, doc_id=doc_id, parent_section=parent_section, n_results=5, vector_store=vector_store
        )
        fact_rows, fact_citations = structured_query(query, doc_id=doc_id, store=fact_store, limit=10)
        all_citations.extend(search_citations)
        all_citations.extend(fact_citations)

    # Synthesize answer: for numerical intent prefer fact rows when present
    answer_parts = []
    if intent == "numerical" and fact_rows:
        for r in fact_rows[:5]:
            answer_parts.append(f"{r.key}: {r.value}" + (f" {r.unit}" if r.unit else ""))
    if hits:
        top = hits[0]
        doc_text = (top.get("document") or "")[:500]
        if doc_text:
            answer_parts.append(doc_text.strip() + ("..." if len(doc_text) >= 500 else ""))
    if intent != "numerical" and fact_rows:
        for r in fact_rows[:5]:
            answer_parts.append(f"{r.key}: {r.value}" + (f" {r.unit}" if r.unit else ""))
    answer = "\n".join(answer_parts) if answer_parts else "No matching content found."
    provenance_chain = ProvenanceChain(
        citations=all_citations,
        source_document=doc_id,
        document_name=doc_id or "unknown",
    )
    return {
        "answer": answer,
        "provenance_chain": provenance_chain,
        "citations": all_citations,
    }


def create_query_graph():
    """
    Build LangGraph StateGraph: single node that runs run_query and sets state.
    State: query, doc_id, answer, provenance_chain.
    """
    from langgraph.graph import StateGraph, END

    def agent_node(state: dict) -> dict:
        result = run_query(
            state.get("query", ""),
            doc_id=state.get("doc_id"),
        )
        return {
            "answer": result["answer"],
            "provenance_chain": result["provenance_chain"],
            "citations": result.get("citations", []),
        }

    graph = StateGraph(dict)
    graph.add_node("agent", agent_node)
    graph.set_entry_point("agent")
    graph.add_edge("agent", END)
    return graph.compile()


def invoke_query_agent(query: str, doc_id: Optional[str] = None) -> dict:
    """Invoke the compiled query agent. Returns state with answer and provenance_chain."""
    graph = create_query_graph()
    state = graph.invoke({"query": query, "doc_id": doc_id})
    return state
