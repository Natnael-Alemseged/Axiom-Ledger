"""PageIndex query: topic -> top-3 sections -> vector search within sections. Precision measurement."""

import logging
from typing import Any, Callable, List, Optional

from refinery.models import LDU

from refinery.pageindex.models import SectionNode, flatten_section_nodes

logger = logging.getLogger(__name__)


def traverse_sections(
    topic: str,
    section_nodes: List[SectionNode],
    embed_fn: Callable[[str], List[float]],
    top_k: int = 5,
) -> List[SectionNode]:
    """
    Traversal method: accept a topic or query string and return the most relevant sections.
    Uses embedding similarity over section title + summary + key_entities. Returns top-k SectionNodes.
    """
    flat = flatten_section_nodes(section_nodes) if section_nodes else []
    if not flat:
        return []
    topic_embed = embed_fn(topic)
    section_texts = [
        (n, " ".join(filter(None, [n.title, n.section_label, n.summary] + (n.key_entities or []))))
        for n in flat
    ]
    section_embeds = [(n, embed_fn(t.strip() or n.section_label or "")) for n, t in section_texts]

    def _sim(a: List[float], b: List[float]) -> float:
        n = len(a)
        if n != len(b) or n == 0:
            return 0.0
        return sum(a[i] * b[i] for i in range(n))

    scored = [(node, _sim(topic_embed, emb)) for node, emb in section_embeds]
    scored.sort(key=lambda x: -x[1])
    return [node for node, _ in scored[:top_k]]


def pageindex_query(
    topic: str,
    section_nodes: List[SectionNode],
    embed_fn: Callable[[str], List[float]],
    vector_store_query_fn: Callable[[List[float], Optional[dict], int], List[Any]],
    top_k_sections: int = 3,
    top_k_chunks: int = 5,
) -> List[Any]:
    """
    Given a topic, embed it and section summaries, pick top-k sections, then run vector search
    restricted to LDUs in those sections. section_nodes can be flat list or tree (roots); flattened internally.
    """
    flat = flatten_section_nodes(section_nodes) if section_nodes else []
    if not flat:
        return vector_store_query_fn(embed_fn(topic), None, top_k_chunks)
    topic_embed = embed_fn(topic)
    section_embeds = []
    for node in flat:
        text = (node.summary or "") + " " + (node.section_label or "") + " " + (node.title or "")
        if text.strip():
            section_embeds.append((node, embed_fn(text.strip())))
        else:
            section_embeds.append((node, embed_fn(node.section_label or node.title or "")))
    # Cosine similarity (simplified: dot product if normalized)
    def _sim(a: List[float], b: List[float]) -> float:
        n = len(a)
        if n != len(b) or n == 0:
            return 0.0
        return sum(a[i] * b[i] for i in range(n))

    scored = [(node, _sim(topic_embed, emb)) for node, emb in section_embeds]
    scored.sort(key=lambda x: -x[1])
    top_sections = [node for node, _ in scored[:top_k_sections]]
    section_labels = {n.section_label for n in top_sections}
    metadata_filter = {"parent_section": {"$in": list(section_labels)}} if section_labels else None
    return vector_store_query_fn(topic_embed, metadata_filter, top_k_chunks)


def retrieval_with_pageindex(
    topic: str,
    section_nodes: List[SectionNode],
    ldus: List[LDU],
    embed_fn: Callable[[str], List[float]],
    top_k_sections: int = 3,
    top_k: int = 5,
) -> List[LDU]:
    """
    Retrieval path A: topic -> top-k sections via embedding similarity -> filter LDUs by section ->
    return top_k by similarity to topic within those LDUs. Uses in-memory LDUs and embed_fn only.
    """
    if not section_nodes or not ldus:
        return _retrieval_flat(topic, ldus, embed_fn, top_k)
    flat = flatten_section_nodes(section_nodes)
    topic_embed = embed_fn(topic)
    section_texts = [(n, (n.summary or "") + " " + (n.section_label or "") + " " + (n.title or "")) for n in flat]
    section_embeds = [(n, embed_fn(t.strip() or n.section_label or "")) for n, t in section_texts]

    def _sim(a: List[float], b: List[float]) -> float:
        n = len(a)
        if n != len(b) or n == 0:
            return 0.0
        return sum(a[i] * b[i] for i in range(n))

    scored_sec = [(n, _sim(topic_embed, e)) for n, e in section_embeds]
    scored_sec.sort(key=lambda x: -x[1])
    top_section_labels = {n.section_label for n, _ in scored_sec[:top_k_sections]}
    filtered = [ldu for ldu in ldus if (ldu.parent_section or "(root)") in top_section_labels]
    if not filtered:
        filtered = ldus
    return _retrieval_flat(topic, filtered, embed_fn, top_k)


def _retrieval_flat(topic: str, ldus: List[LDU], embed_fn: Callable[[str], List[float]], top_k: int) -> List[LDU]:
    """In-memory: embed topic and each LDU content, return top_k by similarity."""
    if not ldus:
        return []
    topic_embed = embed_fn(topic)
    contents = []
    for ldu in ldus:
        if isinstance(ldu.content, str):
            contents.append(ldu.content)
        elif ldu.content is not None:
            contents.append(str(ldu.content)[:2000])
        else:
            contents.append("")
    embeds = [embed_fn(c) for c in contents]

    def _sim(a: List[float], b: List[float]) -> float:
        n = len(a)
        if n != len(b) or n == 0:
            return 0.0
        return sum(a[i] * b[i] for i in range(n))

    scored = [(ldus[i], _sim(topic_embed, embeds[i])) for i in range(len(ldus))]
    scored.sort(key=lambda x: -x[1])
    return [ldu for ldu, _ in scored[:top_k]]


def retrieval_without_pageindex(
    topic: str,
    ldus: List[LDU],
    embed_fn: Callable[[str], List[float]],
    top_k: int = 5,
) -> List[LDU]:
    """Retrieval path B: topic -> vector search over all LDUs. In-memory version."""
    return _retrieval_flat(topic, ldus, embed_fn, top_k)


def precision_at_k(retrieved: List[Any], relevant_ids: set, k: int) -> float:
    """Precision@k: fraction of top-k retrieved that are relevant."""
    top = retrieved[:k]
    if not top:
        return 0.0
    ids = [getattr(x, "chunk_id", None) or getattr(x, "id", str(x)) for x in top]
    return sum(1 for i in ids if i in relevant_ids) / len(top)


def recall_at_k(retrieved: List[Any], relevant_ids: set, k: int) -> float:
    """Recall@k: fraction of relevant that appear in top-k retrieved."""
    if not relevant_ids:
        return 0.0
    top = retrieved[:k]
    ids = [getattr(x, "chunk_id", None) or getattr(x, "id", str(x)) for x in top]
    return sum(1 for i in ids if i in relevant_ids) / len(relevant_ids)
