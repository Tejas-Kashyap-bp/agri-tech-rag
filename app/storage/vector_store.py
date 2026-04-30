"""
Vector database abstraction + ChromaDB implementation.

To migrate to Pinecone / Weaviate / anything else:
  1. Create a new class that inherits VectorStore
  2. Implement the 5 abstract methods
  3. Change the module-level `store` singleton at the bottom of this file

Nothing in the pipeline needs to change.

Collections:
  - one collection per crop:  maize_collection, apple_collection, ...
  - plus common_collection for shared docs

Every document stored carries full metadata for filtering. Every search
MUST filter is_active=true — this is enforced inside the class so callers
cannot forget.
"""

from abc import ABC, abstractmethod
from typing import Optional

import chromadb
from chromadb.config import Settings as ChromaSettings

from app.config import settings
from app.schemas import StoredDocument


class VectorStore(ABC):
    @abstractmethod
    def find_active_by_doc_key(self, doc_key: str, crop: str) -> Optional[dict]:
        """
        Return the currently active document for a given doc_key, or None.

        Searches the crop's collection. Filter enforces is_active=true.
        """
        ...

    @abstractmethod
    def store(self, doc: StoredDocument, embedding: list[float]) -> None:
        """Insert a new document + its embedding into the correct collection."""
        ...

    @abstractmethod
    def set_active(self, doc_id: str, crop: str, is_active: bool) -> None:
        """Flip the is_active flag for a stored document."""
        ...

    @abstractmethod
    def delete(self, doc_id: str, crop: str) -> None:
        """Delete a document by id. Used for rollback on storage failure."""
        ...

    @abstractmethod
    def next_version(self, doc_key: str, crop: str) -> int:
        """Return the version number to use for a new doc with this key."""
        ...

    @abstractmethod
    def get_crop_docs(self, crop: str) -> list[dict]:
        """Return all documents for a specific crop (from its collection)."""
        ...

    @abstractmethod
    def list_crops(self) -> list[str]:
        """Return all crop names that have at least one document stored."""
        ...

    @abstractmethod
    def retrieve(
        self,
        crop: str,
        engine: str,
        k: int = 3,
        query_embedding: list[float] | None = None,
    ) -> list[dict]:
        """
        Hybrid retrieval — embedding similarity + metadata filter.

        Pulls active documents for the given (crop, engine) from BOTH the
        crop-specific collection and the shared common_collection (which holds
        crop-agnostic knowledge: guardrails, condition_rules, financial policy).

        When `query_embedding` is provided, results are ranked by vector
        similarity to the query (still constrained by the engine + is_active
        metadata filter). When omitted, falls back to a metadata-only fetch
        sorted by priority then version desc.

        k caps the result to keep prompt size bounded.
        """
        ...

    @abstractmethod
    def list_all(self) -> list[dict]:
        """
        Return every document stored across all collections.
        Each entry has: doc_id, collection, metadata, text_preview.
        Used by the /db/browse endpoint so developers can inspect stored data.
        """
        ...

    @abstractmethod
    def clear_all(self) -> dict:
        """
        Drop every collection through the vector-DB client's own API (so the
        client's in-memory state stays in sync). Returns a small summary dict
        of what was cleared. Prefer this over deleting the persistence folder
        manually — filesystem-level deletes don't invalidate the cache.
        """
        ...


# ---------------------------------------------------------------------------
# ChromaDB implementation
# ---------------------------------------------------------------------------


class ChromaStore(VectorStore):
    def __init__(self, persist_dir: str | None = None):
        self._client = chromadb.PersistentClient(
            path=persist_dir or settings.CHROMA_PERSIST_DIR,
            settings=ChromaSettings(anonymized_telemetry=False),
        )

    def _collection_name(self, crop: str) -> str:
        safe = crop.strip().lower().replace(" ", "_")
        # Anything not tied to a specific crop lands in common_collection.
        # "common" is the canonical value; guard against the LLM returning
        # "none", "all", "all_crops", "general", or an empty string.
        if safe in ("common", "none", "all", "all_crops", "general", ""):
            return "common_collection"
        return f"{safe}_collection"

    def _collection(self, crop: str):
        return self._client.get_or_create_collection(self._collection_name(crop))

    def find_active_by_doc_key(self, doc_key: str, crop: str) -> Optional[dict]:
        col = self._collection(crop)
        # Chroma stores booleans as-is in metadata; filter with $and
        result = col.get(
            where={
                "$and": [
                    {"doc_key": {"$eq": doc_key}},
                    {"is_active": {"$eq": True}},
                ]
            }
        )
        ids = result.get("ids") or []
        if not ids:
            return None
        return {
            "doc_id": ids[0],
            "metadata": (result.get("metadatas") or [{}])[0],
            "document": (result.get("documents") or [""])[0],
        }

    def store(self, doc: StoredDocument, embedding: list[float]) -> None:
        col = self._collection(doc.metadata.crop)
        # Chroma metadata only accepts primitives (str/int/float/bool)
        metadata = {
            "doc_key": doc.metadata.doc_key,
            "engine": doc.metadata.engine.value,
            "type": doc.metadata.type.value,
            "crop": doc.metadata.crop,
            "version": doc.metadata.version,
            "is_active": doc.metadata.is_active,
            "priority": doc.metadata.priority.value,
            "source": doc.metadata.source.value,
            "description": doc.metadata.description,
        }
        col.add(
            ids=[doc.metadata.doc_id],
            embeddings=[embedding],
            documents=[doc.text_for_embedding],
            metadatas=[metadata],
        )

    def set_active(self, doc_id: str, crop: str, is_active: bool) -> None:
        col = self._collection(crop)
        col.update(ids=[doc_id], metadatas=[{"is_active": is_active}])

    def delete(self, doc_id: str, crop: str) -> None:
        col = self._collection(crop)
        col.delete(ids=[doc_id])

    def next_version(self, doc_key: str, crop: str) -> int:
        col = self._collection(crop)
        result = col.get(where={"doc_key": {"$eq": doc_key}})
        metadatas = result.get("metadatas") or []
        if not metadatas:
            return 1
        return max(int(m.get("version", 0)) for m in metadatas) + 1

    def get_crop_docs(self, crop: str) -> list[dict]:
        col = self._collection(crop)
        result = col.get(include=["metadatas", "documents"])
        ids = result.get("ids") or []
        metadatas = result.get("metadatas") or []
        documents = result.get("documents") or []
        rows = []
        for doc_id, meta, doc_text in zip(ids, metadatas, documents):
            rows.append({
                "doc_id": doc_id,
                "collection": self._collection_name(crop),
                "metadata": meta,
                "text_preview": (doc_text or "")[:300],
            })
        return rows

    def list_crops(self) -> list[str]:
        crops = []
        for col in self._client.list_collections():
            name = col.name
            if name == "common_collection":
                continue
            if name.endswith("_collection"):
                crop = name[: -len("_collection")]
                crops.append(crop)
        return sorted(crops)

    def clear_all(self) -> dict:
        """
        Delete every collection via ChromaDB's own API. Doing it this way
        (instead of `rm -rf chroma_db/`) keeps the PersistentClient cache in
        sync, so /db/browse reflects the empty state on the very next request
        without a server restart.
        """
        names = [col.name for col in self._client.list_collections()]
        for name in names:
            try:
                self._client.delete_collection(name)
            except Exception:
                # Best-effort — keep deleting the others even if one fails
                pass
        return {"cleared": True, "collections_removed": names}

    # Priority string → numeric weight for sort. Higher = comes first.
    _PRIORITY_RANK = {"high": 3, "medium": 2, "low": 1}

    def retrieve(
        self,
        crop: str,
        engine: str,
        k: int = 3,
        query_embedding: list[float] | None = None,
    ) -> list[dict]:
        # Pull from the crop's own collection AND the shared common_collection.
        # Common docs (e.g. cross-crop guardrails, financial policy) must be
        # visible to every advisory — that's the whole point of common_collection.
        seen_ids: set[str] = set()
        rows: list[dict] = []

        where_filter = {
            "$and": [
                {"engine": {"$eq": engine}},
                {"is_active": {"$eq": True}},
            ]
        }

        for collection_name in (self._collection_name(crop), "common_collection"):
            try:
                col = self._client.get_or_create_collection(collection_name)
            except Exception:
                # If the common collection doesn't exist yet, skip silently.
                continue

            if query_embedding is not None:
                # Similarity search restricted to active docs for this engine.
                # Over-fetch by k per collection so the global merge still has
                # room to pick the best across both collections.
                qres = col.query(
                    query_embeddings=[query_embedding],
                    n_results=max(k, 1),
                    where=where_filter,
                    include=["metadatas", "documents", "distances"],
                )
                ids_list = (qres.get("ids") or [[]])[0]
                metas_list = (qres.get("metadatas") or [[]])[0]
                docs_list = (qres.get("documents") or [[]])[0]
                dists_list = (qres.get("distances") or [[]])[0]
            else:
                gres = col.get(
                    where=where_filter,
                    include=["metadatas", "documents"],
                )
                ids_list = gres.get("ids") or []
                metas_list = gres.get("metadatas") or []
                docs_list = gres.get("documents") or []
                dists_list = [None] * len(ids_list)

            for doc_id, meta, doc_text, dist in zip(
                ids_list, metas_list, docs_list, dists_list
            ):
                if doc_id in seen_ids:
                    continue
                seen_ids.add(doc_id)
                rows.append({
                    "doc_id": doc_id,
                    "doc_key": meta.get("doc_key", ""),
                    "doc_type": meta.get("type", ""),
                    "engine": meta.get("engine", ""),
                    "crop": meta.get("crop", ""),
                    "version": int(meta.get("version", 0)),
                    "priority": meta.get("priority", "medium"),
                    "description": meta.get("description", ""),
                    "collection": collection_name,
                    "content": doc_text or "",
                    "distance": dist,
                })

        if query_embedding is not None:
            # Lower distance = better. Tie-break with priority then version.
            rows.sort(
                key=lambda r: (
                    r["distance"] if r["distance"] is not None else float("inf"),
                    -self._PRIORITY_RANK.get(r["priority"], 2),
                    -r["version"],
                )
            )
        else:
            rows.sort(
                key=lambda r: (
                    -self._PRIORITY_RANK.get(r["priority"], 2),
                    -r["version"],
                    r["doc_key"],
                )
            )
        return rows[:k]

    def list_all(self) -> list[dict]:
        rows = []
        for col in self._client.list_collections():
            collection = self._client.get_collection(col.name)
            result = collection.get(include=["metadatas", "documents"])
            ids = result.get("ids") or []
            metadatas = result.get("metadatas") or []
            documents = result.get("documents") or []
            for doc_id, meta, doc_text in zip(ids, metadatas, documents):
                rows.append({
                    "doc_id": doc_id,
                    "collection": col.name,
                    "metadata": meta,
                    # First 300 chars of the stored text so UI can preview it
                    "text_preview": (doc_text or "")[:300],
                })
        # Sort: active first, then by doc_key, then by version desc
        rows.sort(key=lambda r: (
            not r["metadata"].get("is_active", False),
            r["metadata"].get("doc_key", ""),
            -int(r["metadata"].get("version", 0)),
        ))
        return rows


# Lazy singleton. Importing this module no longer touches Chroma — the
# PersistentClient is opened on first get_store() call. Tests can override by
# assigning to the module-level _store before any caller resolves it. To swap
# backends, change the class instantiated inside get_store().
_store: Optional[VectorStore] = None


def get_store() -> VectorStore:
    global _store
    if _store is None:
        _store = ChromaStore()
    return _store


# Back-compat module attribute: keep `from app.storage.vector_store import store`
# working for existing callers. Resolved lazily via __getattr__.
def __getattr__(name: str):
    if name == "store":
        return get_store()
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
