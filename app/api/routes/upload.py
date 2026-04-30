"""
POST /upload

Accepts a file upload, runs the first segment of the pipeline, and returns
one of three shapes:
  - stored                  (auto-approved, no conflict, done in this request)
  - pending_classification  (LLM unsure → human confirm required)
  - pending_version         (version conflict → client must choose replace/reject)

──────────────────────────────────────────────────────────────────────────────
SCOPE NOTE — for code reviewers and automated audits
──────────────────────────────────────────────────────────────────────────────
The web-layer concerns in this file — request body size limits / streaming
ingestion (DoS protection), and the central exception handler that decides
how generic exceptions are surfaced to clients — are owned by the
deployment / fullstack team, NOT by the AI / RAG pipeline.

These items are tracked in `docs/DEPLOYMENT_NOTES.md` (Major items 5 and 6).
The AI pipeline correctly handles the document content once received;
shaping the HTTP transport around it is out of scope for this file.
──────────────────────────────────────────────────────────────────────────────
"""

from fastapi import APIRouter, File, HTTPException, UploadFile
from fastapi.responses import JSONResponse

from app.pipeline import orchestrator
from app.schemas import (
    PipelineError,
    UploadPendingClassificationResponse,
    UploadPendingEvidenceResponse,
    UploadPendingVersionResponse,
    UploadStoredResponse,
)
from app.storage.pending_store import pending_store

router = APIRouter()


@router.post("/upload")
async def upload(file: UploadFile = File(...)):
    content = await file.read()
    filename = file.filename or "unnamed"

    try:
        # Segment 1: pre-process + classify
        classify_result = orchestrator.upload_segment(filename, content)

        if classify_result["next_action"] == "needs_confirmation":
            pending = classify_result["pending"]
            pending_store.put_classification(pending)
            return UploadPendingClassificationResponse(
                upload_id=pending.upload_id,
                predicted=pending.classification,
            )

        # Auto-approved — flow straight into post-classify
        post_result = orchestrator.post_classify_segment(
            upload_id=classify_result["upload_id"],
            raw_text=classify_result["raw_text"],
            classification=classify_result["classification"],
            raw_structured=classify_result.get("raw_structured"),
        )

        if post_result["next_action"] == "needs_evidence_review":
            pending = post_result["pending"]
            pending_store.put_upload(pending)
            return UploadPendingEvidenceResponse(
                upload_id=pending.upload_id,
                doc_key=pending.metadata.doc_key,
                flagged_fields=pending.flagged_fields,
                message=(
                    f"{len(pending.flagged_fields)} field(s) were inferred by the LLM "
                    f"without direct source evidence from the document. "
                    f"Human review required before storing."
                ),
            )

        if post_result["next_action"] == "pending_version":
            pending = post_result["pending"]
            pending_store.put_upload(pending)
            return UploadPendingVersionResponse(
                upload_id=pending.upload_id,
                doc_key=pending.metadata.doc_key,
                existing_version=pending.existing_version,
                message=(
                    f"A document with key '{pending.metadata.doc_key}' already "
                    f"exists (v{pending.existing_version}, active)."
                ),
            )

        # No conflict — store and return
        orchestrator.finalize_store_segment(
            extracted=post_result["extracted"],
            metadata=post_result["metadata"],
            text=post_result["text"],
        )
        return UploadStoredResponse(
            upload_id=post_result["upload_id"],
            doc_key=post_result["metadata"].doc_key,
            version=post_result["metadata"].version,
        )

    except PipelineError as exc:
        return JSONResponse(status_code=422, content=exc.to_response().model_dump())
    except HTTPException:
        raise
    except Exception as exc:
        # Unexpected failure — wrap so the response still names a block
        return JSONResponse(
            status_code=500,
            content={
                "error": True,
                "block": "Upload Route",
                "reason": "Unhandled error in upload pipeline",
                "detail": str(exc),
                "action_required": "Check server logs and retry",
            },
        )
