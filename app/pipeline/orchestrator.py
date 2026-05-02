"""
Pipeline orchestrator.

Each function here corresponds to one "segment" of the full pipeline.
The segments exist because the pipeline pauses at WAIT points:

  upload_segment          → pre-process, prefilter, classify
                            returns either:
                              - classification ready to continue (auto-approve)
                              - pending_classification (needs human confirm)

  post_classify_segment   → raw-input validate → extract → evidence-check →
                            strip sources → post-extract validate → check version
                            returns:
                              - ready_to_store (no conflict)
                              - pending_version (needs replace/reject)
                              - needs_evidence_review (inferred fields)

  finalize_store_segment  → embed + store a brand-new document

  replace_version_segment → write-then-swap when a replacement is confirmed

The route handlers stay thin — they only worry about WAIT-state persistence
and HTTP shapes.

Why raw-input validation runs inside post_classify_segment (after
classification but before extraction): we need the classification to know
which per-doc_type structure/logical rules to apply. The LLM NEVER sees an
invalid file that the raw-input validator would have rejected — it raises
before `extractor.extract` is called.
"""

import json
import uuid
from datetime import datetime
from typing import Any, Optional

from app.config import settings
from app.pipeline import (
    classifier,
    embedder as embedder_mod,
    evidence_checker,
    extractor,
    heuristic,
    metadata as metadata_mod,
    preprocessor,
    raw_input_validator,
    text_gen,
    validator,
)
from app.schemas import (
    Classification,
    DocType,
    DocumentMetadata,
    Engine,
    PendingClassification,
    PendingUpload,
    StoredDocument,
)
from app.storage.vector_store import store as vector_store


# ---------------------------------------------------------------------------
# Segment 1 — upload → classify
# ---------------------------------------------------------------------------


def upload_segment(filename: str, content: bytes) -> dict[str, Any]:
    """
    Runs: Pre-Processing → Heuristic Pre-Filter → LLM Classification.

    Auto-approval rules (any ONE of these sends the doc straight through):
      1. The JSON file declared a valid doc_type (+ crop) at its root.
      2. LLM confidence >= AUTO_APPROVE_THRESHOLD AND the heuristic did not
         flag the doc as spanning multiple engines.

    Otherwise the doc goes to pending_classification for human confirmation.
    """
    pre = preprocessor.preprocess(filename, content)
    possible_types = heuristic.prefilter(pre.raw_text)

    # Short-circuit: JSON declared its own doc_type → trust it, skip LLM.
    declared = _classification_from_declaration(pre.declared_doc_type, pre.declared_crop)
    if declared is not None:
        return {
            "next_action": "auto_approved",
            "upload_id": str(uuid.uuid4()),
            "raw_text": pre.raw_text,
            "raw_structured": pre.raw_structured,
            "classification": declared,
        }

    classification = classifier.classify(pre.raw_text, possible_types)
    upload_id = str(uuid.uuid4())

    auto_approve = (
        classification.confidence >= settings.AUTO_APPROVE_THRESHOLD
        and not heuristic.spans_multiple_engines(possible_types)
    )

    if auto_approve:
        return {
            "next_action": "auto_approved",
            "upload_id": upload_id,
            "raw_text": pre.raw_text,
            "raw_structured": pre.raw_structured,
            "classification": classification,
        }

    pending = PendingClassification(
        upload_id=upload_id,
        created_at=datetime.utcnow(),
        raw_text=pre.raw_text,
        raw_structured=pre.raw_structured,
        possible_types=possible_types,
        classification=classification,
        original_filename=filename,
    )
    return {
        "next_action": "needs_confirmation",
        "pending": pending,
    }


def _classification_from_declaration(
    declared_doc_type: Optional[DocType],
    declared_crop: Optional[str],
) -> Optional[Classification]:
    """Build a high-confidence Classification from a JSON's self-declared fields."""
    if declared_doc_type is None or not declared_crop:
        return None
    engine = _engine_for(declared_doc_type)
    if engine is None:
        return None
    return Classification(
        engine=engine,
        crop=declared_crop,
        doc_type=declared_doc_type,
        confidence=1.0,
        reason="Document declared doc_type and crop at its top level",
    )


# Map required DocTypes to their engine. Supporting types aren't listed —
# we fall back to LLM classification for those so the LLM picks the engine.
_DOC_TYPE_ENGINE: dict[DocType, Engine] = {
    DocType.STAGE_DEFINITION:      Engine.STAGE,
    # IRRIGATION_PARAMETERS deliberately omitted — e2 removed for apple. Such
    # docs (if uploaded) fall through to LLM classification.
    DocType.FERTIGATION_SCHEDULE:  Engine.NUTRITION,
    DocType.IPM_SCHEDULE:          Engine.PEST_DISEASE_RISK,
    DocType.PEST_DISEASE_CONDITION_RULE: Engine.PEST_DISEASE_RISK,
    DocType.YIELD_PARAMETERS:      Engine.YIELD,
    # MARKET_DATA omitted — e6_financial removed for the apple build. Such
    # docs (if uploaded) fall through to LLM classification.
}


def _engine_for(doc_type: DocType) -> Optional[Engine]:
    return _DOC_TYPE_ENGINE.get(doc_type)


# ---------------------------------------------------------------------------
# Segment 2 — classification confirmed → validate raw → extract → validate extracted
# ---------------------------------------------------------------------------


def post_classify_segment(
    upload_id: str,
    raw_text: str,
    classification: Classification,
    raw_structured: Any = None,
) -> dict[str, Any]:
    """
    Runs:
      Raw Input Validation (on parsed structured input, if any)
      → Structured Extraction (LLM)
      → Evidence Check
      → Validation (on LLM output — safety net)
      → Check Existing Active Version

    Returns one of:
      - "ready_to_store":        clean doc, no conflict
      - "pending_version":       clean doc, version conflict exists
      - "needs_evidence_review": LLM inferred fields without source evidence
    """
    # 1. Raw-input validation — BEFORE the LLM, so invalid data cannot be healed.
    #    No-op when raw_structured is None (PDFs).
    raw_input_validator.validate_raw(raw_structured, classification)

    # 2. Extract.
    #    A pre-structured JSON upload (raw_structured is a dict) IS already the
    #    canonical document — the LLM extractor's job would be the identity
    #    map, and the *_source citation contract has no meaning when the data
    #    is the source. In that case we bypass the LLM and the evidence check.
    #    Defense in depth: the post-extract validator below still re-runs the
    #    full structure/range/logical pass on the same dict.
    if isinstance(raw_structured, dict):
        extracted = raw_structured
        unsupported: list[str] = []
    else:
        extracted_raw = extractor.extract(raw_text, classification)
        # 3. Evidence check BEFORE stripping source fields. Raw text is passed
        #    so the checker can verify source strings actually appear in the
        #    document (anti-hallucination) and numeric values match their source.
        unsupported = evidence_checker.find_unsupported_fields(extracted_raw, raw_text)
        extracted = evidence_checker.strip_source_fields(extracted_raw)

    # 4. Post-extract validation — same checks as raw pass, against LLM output.
    #    Catches anything the LLM introduced (hallucinations, bad ranges, etc.).
    #    For PDFs this is the only validation pass.
    validator.validate(extracted, classification)

    # 5. Version check + metadata/text build (both are needed regardless of
    #    which branch the rest of the routing takes).
    doc_key = f"{classification.crop}_{classification.doc_type.value}"
    existing = vector_store.find_active_by_doc_key(doc_key, classification.crop)

    if existing is None:
        metadata = metadata_mod.build_metadata(extracted, classification, version=1)
        existing_doc_id = None
        existing_version = None
    else:
        next_version = vector_store.next_version(doc_key, classification.crop)
        metadata = metadata_mod.build_metadata(extracted, classification, version=next_version)
        existing_doc_id = existing["doc_id"]
        existing_version = int(existing["metadata"].get("version", 0))

    text = text_gen.generate_text(extracted, metadata)

    # 6. Evidence issues take routing priority — human must confirm before storing.
    if unsupported:
        pending = PendingUpload(
            upload_id=upload_id,
            created_at=datetime.utcnow(),
            validated_doc=extracted,
            metadata=metadata,
            text_for_embedding=text,
            existing_doc_id=existing_doc_id,
            existing_version=existing_version,
            flagged_fields=unsupported,
            requires_evidence_review=True,
        )
        return {
            "next_action": "needs_evidence_review",
            "pending": pending,
        }

    if existing is None:
        return {
            "next_action": "ready_to_store",
            "upload_id": upload_id,
            "extracted": extracted,
            "metadata": metadata,
            "text": text,
        }

    pending = PendingUpload(
        upload_id=upload_id,
        created_at=datetime.utcnow(),
        validated_doc=extracted,
        metadata=metadata,
        text_for_embedding=text,
        existing_doc_id=existing_doc_id,
        existing_version=existing_version,
    )
    return {
        "next_action": "pending_version",
        "pending": pending,
    }


# ---------------------------------------------------------------------------
# Segment 3 — store a brand-new document (no conflict path)
# ---------------------------------------------------------------------------


def finalize_store_segment(
    extracted: dict[str, Any],
    metadata: DocumentMetadata,
    text: str,
) -> None:
    embedding = embedder_mod.embedder.embed(text)
    stored = StoredDocument(
        metadata=metadata,
        body={"raw_text": json.dumps(extracted, ensure_ascii=False)},
        text_for_embedding=text,
    )
    vector_store.store(stored, embedding)


# ---------------------------------------------------------------------------
# Segment 4 — replace existing version (write-then-swap, rollback-safe)
# ---------------------------------------------------------------------------


def replace_version_segment(pending: PendingUpload) -> None:
    """
    Option B from decisions.md: keep the old version active until the new
    one is fully stored, then flip.

    Steps:
      1. Store new doc with is_active=False
      2. On any failure during store: delete the partial new doc, leave old
         untouched, re-raise
      3. Only after successful store: set old is_active=False, new is_active=True
    """
    inactive_metadata = pending.metadata.model_copy(update={"is_active": False})
    stored = StoredDocument(
        metadata=inactive_metadata,
        body={"raw_text": json.dumps(pending.validated_doc, ensure_ascii=False)},
        text_for_embedding=pending.text_for_embedding,
    )

    try:
        embedding = embedder_mod.embedder.embed(pending.text_for_embedding)
        vector_store.store(stored, embedding)
    except Exception:
        try:
            vector_store.delete(pending.metadata.doc_id, pending.metadata.crop)
        except Exception:
            pass
        raise

    # Activation order matters under crash. Two writes, no transaction:
    #   1) flip NEW → active
    #   2) flip OLD → inactive
    # If the process dies between the two, retrieval briefly sees BOTH active
    # (caller's tie-breaker picks the higher version — correct one wins). The
    # reverse order would leave ZERO active for this doc_key after a crash,
    # which silently breaks every advisory until the next successful upload.
    vector_store.set_active(
        pending.metadata.doc_id, pending.metadata.crop, is_active=True
    )
    vector_store.set_active(
        pending.existing_doc_id, pending.metadata.crop, is_active=False
    )
