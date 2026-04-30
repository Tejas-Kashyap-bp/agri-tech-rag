"""
Crop coverage endpoints.

GET /crops                    — list all crops that have any data in the DB
GET /crop/{crop}/coverage     — for a crop: which required sections are present / missing,
                                plus all extra crop_knowledge docs

A "section" may bundle multiple doc_types (e.g. the Pest & Disease Advisory
section contains both `ipm_schedule` and `pest_disease_condition_rule`).
A section is marked present when ANY of its children is present.
"""

from fastapi import APIRouter

from app.schemas import (
    CHILD_DOC_TYPE_LABELS,
    REQUIRED_COVERAGE_SECTIONS,
    CoverageChild,
    CoverageItem,
    CropCoverageResponse,
    DocType,
)
from app.storage.vector_store import store

router = APIRouter(tags=["coverage"])


@router.get("/crops")
def list_crops():
    """All crop names that have at least one document in the vector DB."""
    return {"crops": store.list_crops()}


@router.get("/crop/{crop}/coverage", response_model=CropCoverageResponse)
def crop_coverage(crop: str):
    """
    Returns a full coverage report for a crop:
    - required: one entry per REQUIRED_COVERAGE_SECTIONS — present or missing.
      Grouped sections (like Pest & Disease Advisory) also expose a `children`
      list so the UI can render sub-rows per child doc_type.
    - extra_knowledge: all active crop_knowledge docs for this crop
    - completeness_pct: 0-100
    """
    crop = crop.strip().lower()
    all_docs = store.get_crop_docs(crop)

    # Index active docs by their doc_type (keep highest-version on duplicates).
    active_by_type: dict[str, dict] = {}
    extra_knowledge: list[dict] = []

    for doc in all_docs:
        meta = doc["metadata"]
        if not meta.get("is_active", False):
            continue

        doc_type = meta.get("type", "")

        if doc_type == DocType.CROP_KNOWLEDGE.value:
            extra_knowledge.append({
                "doc_id": doc["doc_id"],
                "doc_key": meta.get("doc_key"),
                "description": meta.get("description"),
                "version": meta.get("version"),
                "source": meta.get("source"),
            })
            continue

        existing = active_by_type.get(doc_type)
        if existing is None or meta.get("version", 0) > existing["metadata"].get("version", 0):
            active_by_type[doc_type] = doc

    # Build required coverage list — one CoverageItem per section.
    required: list[CoverageItem] = []
    present_count = 0

    for section in REQUIRED_COVERAGE_SECTIONS:
        child_types: list[DocType] = section["doc_types"]

        # Build one CoverageChild per declared child doc_type.
        children: list[CoverageChild] = []
        first_present: dict | None = None
        first_present_type: DocType | None = None

        for child_type in child_types:
            match = active_by_type.get(child_type.value)
            label = CHILD_DOC_TYPE_LABELS.get(child_type, child_type.value)
            if match:
                meta = match["metadata"]
                children.append(CoverageChild(
                    doc_type=child_type.value,
                    label=label,
                    status="present",
                    doc_key=meta.get("doc_key"),
                    doc_id=match["doc_id"],
                    version=meta.get("version"),
                    description=meta.get("description"),
                ))
                if first_present is None:
                    first_present = match
                    first_present_type = child_type
            else:
                children.append(CoverageChild(
                    doc_type=child_type.value,
                    label=label,
                    status="missing",
                ))

        if first_present is not None:
            present_count += 1
            meta = first_present["metadata"]
            required.append(CoverageItem(
                section_key=section["key"],
                label=section["label"],
                status="present",
                doc_type=first_present_type.value if first_present_type else None,
                doc_key=meta.get("doc_key"),
                doc_id=first_present["doc_id"],
                version=meta.get("version"),
                description=meta.get("description"),
                children=children,
            ))
        else:
            required.append(CoverageItem(
                section_key=section["key"],
                label=section["label"],
                status="missing",
                children=children,
            ))

    completeness = round((present_count / len(REQUIRED_COVERAGE_SECTIONS)) * 100)

    return CropCoverageResponse(
        crop=crop,
        collection=f"{crop}_collection",
        completeness_pct=completeness,
        required=required,
        extra_knowledge=extra_knowledge,
    )
