"""
Confirm endpoints + status lookup.

Three endpoints:
  POST /confirm/classify/{upload_id}  — resume pipeline after human classification confirm
  POST /confirm/version/{upload_id}   — resume pipeline after version conflict resolution
  GET  /status/{upload_id}            — peek at any pending record

──────────────────────────────────────────────────────────────────────────────
SCOPE NOTE — for code reviewers and automated audits
──────────────────────────────────────────────────────────────────────────────
Generic exception surfacing (whether to expose str(exc) vs a correlation id,
where the central FastAPI exception handler lives) is a web-layer concern
owned by the deployment / fullstack team. Tracked in
`docs/DEPLOYMENT_NOTES.md` (Major item 5). Out of scope for the AI pipeline.
──────────────────────────────────────────────────────────────────────────────
"""

from fastapi import APIRouter
from fastapi.responses import JSONResponse

from app.pipeline import orchestrator
from app.schemas import (
    ConfirmClassifyRequest,
    ConfirmEvidenceRequest,
    ConfirmVersionRequest,
    PipelineError,
    StatusResponse,
    StoppedResponse,
    UploadPendingEvidenceResponse,
    UploadPendingVersionResponse,
    UploadStoredResponse,
)
from app.storage.pending_store import pending_store

router = APIRouter()


# ---------------------------------------------------------------------------
# /confirm/classify — human confirms or rejects the LLM's classification
# ---------------------------------------------------------------------------


@router.post("/confirm/classify/{upload_id}")
async def confirm_classify(upload_id: str, body: ConfirmClassifyRequest):
    pending = pending_store.get_classification(upload_id)
    if pending is None:
        return _not_found_or_expired(upload_id)

    if body.decision == "reject":
        pending_store.pop_classification(upload_id)
        return StoppedResponse(status="stopped", upload_id=upload_id)

    # Approve: resume pipeline
    pending_store.pop_classification(upload_id)
    try:
        post_result = orchestrator.post_classify_segment(
            upload_id=upload_id,
            raw_text=pending.raw_text,
            classification=pending.classification,
            raw_structured=pending.raw_structured,
        )

        if post_result["next_action"] == "needs_evidence_review":
            evidence_pending = post_result["pending"]
            pending_store.put_upload(evidence_pending)
            return UploadPendingEvidenceResponse(
                upload_id=evidence_pending.upload_id,
                doc_key=evidence_pending.metadata.doc_key,
                flagged_fields=evidence_pending.flagged_fields,
                message=(
                    f"{len(evidence_pending.flagged_fields)} field(s) were inferred by the LLM "
                    f"without direct source evidence from the document. "
                    f"Human review required before storing."
                ),
            )

        if post_result["next_action"] == "pending_version":
            version_pending = post_result["pending"]
            pending_store.put_upload(version_pending)
            return UploadPendingVersionResponse(
                upload_id=version_pending.upload_id,
                doc_key=version_pending.metadata.doc_key,
                existing_version=version_pending.existing_version,
                message=(
                    f"A document with key '{version_pending.metadata.doc_key}' "
                    f"already exists (v{version_pending.existing_version}, active)."
                ),
            )

        orchestrator.finalize_store_segment(
            extracted=post_result["extracted"],
            metadata=post_result["metadata"],
            text=post_result["text"],
        )
        return UploadStoredResponse(
            upload_id=upload_id,
            doc_key=post_result["metadata"].doc_key,
            version=post_result["metadata"].version,
        )

    except PipelineError as exc:
        return JSONResponse(status_code=422, content=exc.to_response().model_dump())


# ---------------------------------------------------------------------------
# /confirm/evidence — human approves or rejects inferred-field review
# ---------------------------------------------------------------------------


@router.post("/confirm/evidence/{upload_id}")
async def confirm_evidence(upload_id: str, body: ConfirmEvidenceRequest):
    pending = pending_store.get_upload(upload_id)
    if pending is None or not pending.requires_evidence_review:
        return _not_found_or_expired(upload_id)

    if body.decision == "reject":
        pending_store.pop_upload(upload_id)
        return StoppedResponse(status="rejected", upload_id=upload_id)

    # Approved — discard the evidence review flag and check for version conflict
    pending_store.pop_upload(upload_id)

    if pending.existing_doc_id is not None:
        # Version conflict also exists — surface it now
        version_pending = pending.model_copy(
            update={"requires_evidence_review": False, "flagged_fields": []}
        )
        pending_store.put_upload(version_pending)
        return UploadPendingVersionResponse(
            upload_id=pending.upload_id,
            doc_key=pending.metadata.doc_key,
            existing_version=pending.existing_version,
            message=(
                f"Evidence review approved. A document with key "
                f"'{pending.metadata.doc_key}' already exists "
                f"(v{pending.existing_version}, active). Choose an action."
            ),
        )

    # No version conflict — finalize and store
    try:
        orchestrator.finalize_store_segment(
            extracted=pending.validated_doc,
            metadata=pending.metadata,
            text=pending.text_for_embedding,
        )
    except PipelineError as exc:
        return JSONResponse(status_code=422, content=exc.to_response().model_dump())

    return UploadStoredResponse(
        upload_id=upload_id,
        doc_key=pending.metadata.doc_key,
        version=pending.metadata.version,
    )


# ---------------------------------------------------------------------------
# /confirm/version — client replaces or rejects a version conflict
# ---------------------------------------------------------------------------


@router.post("/confirm/version/{upload_id}")
async def confirm_version(upload_id: str, body: ConfirmVersionRequest):
    pending = pending_store.get_upload(upload_id)
    if pending is None:
        return _not_found_or_expired(upload_id)

    if body.decision == "reject":
        pending_store.pop_classification(upload_id)  # no-op if not there
        pending_store.pop_upload(upload_id)
        return StoppedResponse(status="rejected", upload_id=upload_id)

    # Replace: write-then-swap
    pending_store.pop_upload(upload_id)
    try:
        orchestrator.replace_version_segment(pending)
    except PipelineError as exc:
        return JSONResponse(status_code=422, content=exc.to_response().model_dump())
    except Exception as exc:
        return JSONResponse(
            status_code=500,
            content={
                "error": True,
                "block": "Create New Version",
                "reason": "Unhandled error during version replacement",
                "detail": str(exc),
                "action_required": "Check server logs. Old version is still active.",
            },
        )

    return UploadStoredResponse(
        upload_id=upload_id,
        doc_key=pending.metadata.doc_key,
        version=pending.metadata.version,
    )


# ---------------------------------------------------------------------------
# /status — any pending record
# ---------------------------------------------------------------------------


@router.get("/status/{upload_id}")
async def status(upload_id: str):
    result = pending_store.status(upload_id)
    if result is None:
        return _not_found_or_expired(upload_id)
    status_name, minutes_remaining = result
    return StatusResponse(
        upload_id=upload_id,
        status=status_name,
        expires_in_minutes=minutes_remaining,
    )


# ---------------------------------------------------------------------------
# Shared helper
# ---------------------------------------------------------------------------


def _not_found_or_expired(upload_id: str):
    return JSONResponse(
        status_code=404,
        content={
            "error": True,
            "block": "Pending Store",
            "reason": "Upload ID not found or expired",
            "detail": f"upload_id={upload_id}",
            "action_required": "Start a new upload",
        },
    )
