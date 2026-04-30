"""
Retrieval layer.

Hybrid retrieval: embedding similarity (sentence-transformers, same model as
ingestion) constrained by metadata filter (engine + is_active=true), unioned
with the shared common_collection.

Contract:
  retrieve(crop, engine, query_text, k=1) -> list[RetrievedDoc]

Each RetrievedDoc carries enough information for two things:
  1. The LLM to read the knowledge (`content`, `description`).
  2. The audit log to cite the source (`doc_key`, `version`, `collection`).

If `query_text` is None, the store falls back to a metadata-only fetch
sorted by priority + version (useful for engines whose retrieval is purely
deterministic, e.g. when the engine focus alone is enough).
"""

from typing import Any, Optional

from app.pipeline.embedder import embedder
from app.storage.vector_store import store


def retrieve(
    crop: str,
    engine: str,
    query_text: Optional[str] = None,
    k: int = 1,
) -> list[dict[str, Any]]:
    query_embedding = embedder.embed(query_text) if query_text else None
    return store.retrieve(
        crop=crop,
        engine=engine,
        k=k,
        query_embedding=query_embedding,
    )
