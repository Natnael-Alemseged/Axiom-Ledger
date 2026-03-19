"""ChromaDB vector store for LDUs with sentence-transformers embeddings."""

import json
import logging
from pathlib import Path
from typing import Any, List, Optional

from refinery.models import LDU

logger = logging.getLogger(__name__)

CHROMA_DIR = Path(".refinery/vector_store")
DEFAULT_EMBEDDING_MODEL = "all-MiniLM-L6-v2"


def _ldu_to_text(ldu: LDU) -> str:
    """String representation of LDU for embedding."""
    if ldu.content is None:
        return ""
    if isinstance(ldu.content, str):
        return ldu.content
    if isinstance(ldu.content, list):
        lines = []
        for row in ldu.content:
            if isinstance(row, (list, tuple)):
                lines.append(" | ".join(str(c) for c in row))
            else:
                lines.append(str(row))
        return "\n".join(lines)
    return str(ldu.content)


class VectorStore:
    """ChromaDB-backed vector store for LDUs. Persists at chroma_dir."""

    def __init__(
        self,
        collection_name: str = "refinery_ldus",
        chroma_dir: Optional[Path] = None,
        embedding_model: Optional[str] = None,
    ):
        self.collection_name = collection_name
        self.chroma_dir = chroma_dir or CHROMA_DIR
        self.embedding_model = embedding_model or DEFAULT_EMBEDDING_MODEL
        self._client = None
        self._collection = None
        self._embed_fn = None

    def _get_client(self):
        if self._client is None:
            try:
                import chromadb
                from chromadb.config import Settings
                self._client = chromadb.PersistentClient(
                    path=str(self.chroma_dir),
                    settings=Settings(anonymized_telemetry=False),
                )
            except Exception as e:
                raise RuntimeError(f"ChromaDB not available: {e}") from e
        return self._client

    def _get_collection(self):
        if self._collection is None:
            self._collection = self._get_client().get_or_create_collection(
                name=self.collection_name,
                metadata={"description": "Refinery LDUs"},
            )
        return self._collection

    def _get_embed_fn(self):
        if self._embed_fn is None:
            try:
                from sentence_transformers import SentenceTransformer
                model = SentenceTransformer(self.embedding_model)
                self._embed_fn = lambda texts: model.encode(texts if isinstance(texts, list) else [texts], convert_to_numpy=True).tolist()
            except Exception as e:
                raise RuntimeError(f"sentence-transformers not available: {e}") from e
        return self._embed_fn

    def add_ldus(self, doc_id: str, ldus: List[LDU], batch_size: int = 32) -> None:
        """Ingest LDUs into the collection. Each LDU becomes one vector with metadata."""
        if not ldus:
            return
        coll = self._get_collection()
        embed_fn = self._get_embed_fn()
        ids = []
        documents = []
        metadatas = []
        for i, ldu in enumerate(ldus):
            chunk_id = ldu.chunk_id or f"{doc_id}_chunk_{i}"
            ids.append(chunk_id)
            documents.append(_ldu_to_text(ldu))
            meta = {
                "doc_id": doc_id,
                "chunk_id": chunk_id,
                "chunk_type": ldu.kind,
                "parent_section": ldu.parent_section or "",
            }
            if ldu.page_refs:
                meta["page_refs"] = json.dumps(ldu.page_refs)
            if ldu.bbox is not None:
                meta["bbox"] = json.dumps(ldu.bbox)
            if ldu.content_hash:
                meta["content_hash"] = ldu.content_hash
            metadatas.append(meta)
        for start in range(0, len(ids), batch_size):
            end = start + batch_size
            embeds = embed_fn(documents[start:end])
            if not isinstance(embeds[0], list):
                embeds = [e.tolist() if hasattr(e, "tolist") else list(e) for e in embeds]
            coll.add(
                ids=ids[start:end],
                embeddings=embeds,
                documents=documents[start:end],
                metadatas=metadatas[start:end],
            )
        logger.info("Ingested %d LDUs for doc_id=%s", len(ldus), doc_id)

    def query(
        self,
        query_embedding: List[float],
        where: Optional[dict] = None,
        n_results: int = 5,
    ) -> List[dict]:
        """Return top n_results by similarity. where is ChromaDB metadata filter."""
        coll = self._get_collection()
        kwargs = {"query_embeddings": [query_embedding], "n_results": n_results}
        if where:
            kwargs["where"] = where
        result = coll.query(**kwargs)
        if not result or not result.get("ids"):
            return []
        out = []
        ids = result["ids"][0]
        metadatas = result.get("metadatas", [[]])[0]
        documents = result.get("documents", [[]])[0]
        for i, id_ in enumerate(ids):
            meta = metadatas[i] if i < len(metadatas) else {}
            doc = documents[i] if i < len(documents) else ""
            # Parse bbox from JSON if present (ChromaDB metadata is string)
            bbox = None
            if meta.get("bbox"):
                try:
                    bbox = json.loads(meta["bbox"]) if isinstance(meta["bbox"], str) else meta["bbox"]
                except (TypeError, ValueError):
                    pass
            out.append({
                "id": id_,
                "metadata": meta,
                "document": doc,
                "bbox": bbox,
                "content_hash": meta.get("content_hash"),
                "doc_id": meta.get("doc_id"),
                "page_refs": json.loads(meta["page_refs"]) if meta.get("page_refs") else [],
            })
        return out

    def get_embed_fn(self):
        """Return a function that embeds a single string and returns list of floats."""
        embed_fn = self._get_embed_fn()
        def fn(text: str) -> List[float]:
            out = embed_fn([text])
            return out[0] if isinstance(out[0], list) else out[0].tolist()
        return fn


def ingest_document(doc_id: str, ldus: List[LDU], collection_name: str = "refinery_ldus", chroma_dir: Optional[Path] = None) -> VectorStore:
    """Convenience: create store, add LDUs, return store for querying."""
    store = VectorStore(collection_name=collection_name, chroma_dir=chroma_dir or CHROMA_DIR)
    store.add_ldus(doc_id, ldus)
    return store
