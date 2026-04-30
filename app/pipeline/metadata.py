"""
Block: Add Metadata

Given a validated document + its classification, build the full DocumentMetadata
that will be attached when the document is stored.

doc_key rules:
  - Standard types:   {crop}_{doc_type}  (e.g. maize_stage_definition)
  - crop_knowledge:   {crop}_crop_knowledge_{knowledge_title_slug}
                      Multiple crop_knowledge docs can be active per crop because
                      each has a unique title/slug → unique doc_key.

version is supplied by the caller (1 for new, N+1 for replacement).
description is LLM-generated from the document content.
"""

import json
import re
import uuid
from typing import Any

from app.llm.gemini_provider import llm
from app.schemas import (
    Classification,
    DocType,
    DocumentMetadata,
    PipelineError,
    Priority,
    Source,
)

BLOCK = "Add Metadata"

_DESCRIPTION_SYSTEM = (
    "You write short, factual descriptions of agricultural documents. "
    "Return one or two sentences. Plain text. No markdown."
)

_TITLE_SYSTEM = (
    "You generate short 3-6 word titles for agricultural knowledge documents. "
    "The title must be unique and specific to the content. "
    "Return ONLY the title. No punctuation. No extra text."
)


def build_metadata(
    doc: dict[str, Any],
    classification: Classification,
    version: int,
    source: Source = Source.CLIENT_UPLOAD,
    priority: Priority = Priority.MEDIUM,
) -> DocumentMetadata:
    description = _generate_description(doc)
    doc_key = _build_doc_key(doc, classification, description)

    return DocumentMetadata(
        doc_id=str(uuid.uuid4()),
        doc_key=doc_key,
        engine=classification.engine,
        type=classification.doc_type,
        crop=classification.crop,
        version=version,
        is_active=True,
        priority=priority,
        source=source,
        description=description,
    )


def _build_doc_key(
    doc: dict[str, Any],
    classification: Classification,
    description: str,
) -> str:
    if classification.doc_type == DocType.CROP_KNOWLEDGE:
        # Each crop_knowledge doc gets a unique key based on its topic title
        # so multiple can coexist as active docs for the same crop.
        title_slug = _get_knowledge_slug(doc, description)
        return f"{classification.crop}_crop_knowledge_{title_slug}"

    return f"{classification.crop}_{classification.doc_type.value}"


def _get_knowledge_slug(doc: dict[str, Any], description: str) -> str:
    """Ask the LLM for a short title, then slugify it."""
    try:
        title = llm.complete(
            prompt=(
                f"Document description: {description}\n\n"
                f"Document content (summary): {json.dumps(doc, ensure_ascii=False)[:500]}\n\n"
                "Give this crop knowledge document a unique 3-6 word title."
            ),
            system=_TITLE_SYSTEM,
        ).strip()
    except Exception:
        # Fallback to a random suffix rather than failing the whole pipeline
        title = str(uuid.uuid4())[:8]

    return _slugify(title)


def _slugify(text: str) -> str:
    text = text.lower().strip()
    text = re.sub(r"[^a-z0-9]+", "_", text)
    text = text.strip("_")
    return text[:60]  # cap length


def _generate_description(doc: dict[str, Any]) -> str:
    try:
        response = llm.complete(
            prompt=(
                "Write a 1-2 sentence description of this agricultural document:\n"
                f"{json.dumps(doc, indent=2, ensure_ascii=False)}"
            ),
            system=_DESCRIPTION_SYSTEM,
        )
    except Exception as exc:
        raise PipelineError(
            block=BLOCK,
            reason="LLM call for description generation failed",
            detail=str(exc),
            action_required="Retry the upload",
        ) from exc

    text = response.strip()
    if not text:
        raise PipelineError(
            block=BLOCK,
            reason="LLM returned empty description",
            action_required="Retry the upload",
        )
    return text
