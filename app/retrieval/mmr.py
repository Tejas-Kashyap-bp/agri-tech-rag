"""
Maximal Marginal Relevance (MMR) retrieval.

Problem this solves:
  Pure cosine top-k can return 3 near-duplicate docs about the same sub-topic
  while ignoring other relevant material. For E3 nutrition on apple, that
  could mean 3 NPK-schedule docs surface and the INM doc is squeezed out.

MMR balances two forces per pick:
  - relevance to the query
  - novelty vs. already-selected docs

Algorithm (Carbonell & Goldstein, 1998):
  1. Fetch fetch_k candidates by similarity (with their embeddings).
  2. Greedily pick the doc maximizing:
        score(d) = λ · sim(q, d) − (1 − λ) · max_{d' in picked} sim(d, d')
     λ = 1.0  → pure relevance (degenerates to top-k)
     λ = 0.0  → pure diversity (ignores the query)
     λ = 0.5  → balanced (default)
  3. Stop after k picks.

WHY native instead of a library:
  We already have everything: query embedding from `embedder`, doc
  embeddings from Chroma's `include=["embeddings"]`, numpy for the math.
  Adding a retriever framework just to call this 30-line loop is overkill.

Distance vs similarity note:
  Chroma returns squared L2 distance for normalized embeddings, where
  sim ≈ 1 − distance/2. We work in a "score" space where higher = better
  by using `1 − distance` consistently for both query→doc and doc→doc.
"""

import logging
from typing import Any

import numpy as np

from app.config import settings
from app.pipeline.embedder import embedder
from app.storage.vector_store import get_store

log = logging.getLogger("retrieval.mmr")


def _cosine(a: np.ndarray, b: np.ndarray) -> float:
    """Cosine similarity. Embeddings are already L2-normalized by MiniLM,
    so this is effectively a dot product, but we normalize defensively."""
    na = np.linalg.norm(a)
    nb = np.linalg.norm(b)
    if na == 0.0 or nb == 0.0:
        return 0.0
    return float(np.dot(a, b) / (na * nb))


def _fetch_candidates(
    crop: str,
    engine: str,
    query_embedding: list[float],
    fetch_k: int,
) -> list[dict[str, Any]]:
    """
    Pull fetch_k candidates from BOTH the crop collection and common_collection,
    INCLUDING their embeddings (which ChromaStore.retrieve does not expose).
    Mirrors ChromaStore.retrieve's filter and merge logic.
    """
    store = get_store()
    client = store._client  # intentional: we need raw Chroma access for embeddings
    collection_name = store._collection_name(crop)

    where_filter = {
        "$and": [
            {"engine": {"$eq": engine}},
            {"is_active": {"$eq": True}},
        ]
    }

    seen: set[str] = set()
    rows: list[dict[str, Any]] = []

    for col_name in (collection_name, "common_collection"):
        try:
            col = client.get_or_create_collection(col_name)
        except Exception:
            continue
        try:
            qres = col.query(
                query_embeddings=[query_embedding],
                n_results=max(fetch_k, 1),
                where=where_filter,
                include=["metadatas", "documents", "distances", "embeddings"],
            )
        except Exception as exc:
            log.warning("mmr: query failed on %s (%s)", col_name, exc)
            continue

        ids = (qres.get("ids") or [[]])[0]
        metas = (qres.get("metadatas") or [[]])[0]
        docs = (qres.get("documents") or [[]])[0]
        dists = (qres.get("distances") or [[]])[0]
        embs = (qres.get("embeddings") or [[]])[0]

        for doc_id, meta, doc_text, dist, emb in zip(ids, metas, docs, dists, embs):
            if doc_id in seen:
                continue
            seen.add(doc_id)
            rows.append({
                "doc_id": doc_id,
                "doc_key": meta.get("doc_key", ""),
                "doc_type": meta.get("type", ""),
                "engine": meta.get("engine", ""),
                "crop": meta.get("crop", ""),
                "version": int(meta.get("version", 0)),
                "priority": meta.get("priority", "medium"),
                "description": meta.get("description", ""),
                "collection": col_name,
                "content": doc_text or "",
                "distance": dist,
                "_embedding": np.asarray(emb, dtype=np.float32),
            })

    return rows


def retrieve_mmr(
    crop: str,
    engine: str,
    query_text: str,
    k: int = 1,
) -> list[dict[str, Any]]:
    """
    MMR re-rank. Pulls MMR_FETCH_K candidates by similarity, then greedily
    selects k that balance relevance to the query against novelty vs. each
    other.
    """
    lam = max(0.0, min(1.0, settings.MMR_LAMBDA))
    fetch_k = max(k, settings.MMR_FETCH_K)

    query_embedding = embedder.embed(query_text)
    candidates = _fetch_candidates(crop, engine, query_embedding, fetch_k)

    if not candidates:
        return []
    if len(candidates) <= k:
        # Strip the internal embedding before returning.
        for r in candidates:
            r.pop("_embedding", None)
        return candidates

    q_vec = np.asarray(query_embedding, dtype=np.float32)
    # Pre-compute query→doc relevance for every candidate.
    rel = np.array([_cosine(q_vec, c["_embedding"]) for c in candidates])

    selected_idx: list[int] = []
    remaining = set(range(len(candidates)))

    # First pick is always the most relevant — diversity term is 0 (no peers).
    first = int(np.argmax(rel))
    selected_idx.append(first)
    remaining.discard(first)

    while len(selected_idx) < k and remaining:
        best_score = -float("inf")
        best_i = -1
        for i in remaining:
            # Diversity penalty: max similarity to anything already picked.
            div = max(
                _cosine(candidates[i]["_embedding"], candidates[j]["_embedding"])
                for j in selected_idx
            )
            score = lam * rel[i] - (1.0 - lam) * div
            if score > best_score:
                best_score = score
                best_i = i
        selected_idx.append(best_i)
        remaining.discard(best_i)

    log.info(
        "mmr: picked %d/%d (fetch_k=%d, lambda=%.2f) engine=%s crop=%s",
        len(selected_idx), len(candidates), fetch_k, lam, engine, crop,
    )

    out = []
    for idx in selected_idx:
        row = candidates[idx]
        row.pop("_embedding", None)
        out.append(row)
    return out
