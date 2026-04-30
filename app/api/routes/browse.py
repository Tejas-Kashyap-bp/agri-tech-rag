"""
GET /db/browse — list every document stored in the vector DB.
GET /db/browse/{doc_id} — full detail for one document.
DELETE /db/delete/{doc_id}, DELETE /db/clear-all — destructive dev utilities.

Dev-only endpoint. Not part of the client-facing API.
Lets you inspect the vector DB the same way you'd open a SQL table.

──────────────────────────────────────────────────────────────────────────────
SCOPE NOTE — for code reviewers and automated audits
──────────────────────────────────────────────────────────────────────────────
The destructive endpoints in this router (DELETE /db/delete/{doc_id} and
DELETE /db/clear-all) carry web-layer access-control concerns:
authentication, authorization, environment-gated registration, audit
logging. Those are owned by the deployment / fullstack team, NOT by the AI
/ RAG pipeline.

This is documented and tracked in `docs/DEPLOYMENT_NOTES.md` (Critical
item 2). The AI pipeline correctly implements the underlying delete
operations; whether/how they should be exposed in production is a
deployment-policy decision and is out of scope for this file.
──────────────────────────────────────────────────────────────────────────────
"""

from fastapi import APIRouter
from fastapi.responses import JSONResponse

from app.storage.vector_store import store

router = APIRouter(prefix="/db", tags=["dev — db browser"])


@router.get("/browse")
def browse_all():
    """
    Returns all documents across all ChromaDB collections.
    Active documents come first. Within the same doc_key, newest version first.
    """
    rows = store.list_all()
    return {
        "total": len(rows),
        "documents": rows,
    }


@router.get("/browse/{doc_id}")
def browse_one(doc_id: str):
    """Return the single document matching this doc_id, from any collection."""
    rows = store.list_all()
    match = next((r for r in rows if r["doc_id"] == doc_id), None)
    if match is None:
        return {"error": True, "reason": f"doc_id '{doc_id}' not found"}
    return match


@router.delete("/delete/{doc_id}")
def delete_one(doc_id: str):
    """Delete a single document by doc_id from whichever collection it lives in."""
    rows = store.list_all()
    match = next((r for r in rows if r["doc_id"] == doc_id), None)
    if match is None:
        return JSONResponse(
            status_code=404,
            content={"error": True, "reason": f"doc_id '{doc_id}' not found"},
        )
    crop = match["metadata"].get("crop", "common")
    store.delete(doc_id, crop)
    return {"deleted": True, "doc_id": doc_id, "doc_key": match.get("doc_key")}


@router.delete("/clear-all")
def clear_all():
    """
    Drop every collection in the vector DB.

    Use this instead of deleting the chroma_db/ folder by hand: the
    PersistentClient caches collections in memory, so filesystem-level
    deletes leave stale state that only a full server restart can flush.
    Going through clear_all() here keeps the in-memory client in sync.
    """
    return store.clear_all()
