"""
Block: Pre-Processing   (Excalidraw box name)

Takes a raw uploaded file (PDF / JSON / CSV) and returns:
  - raw_text: plain-text rendering for classification + LLM extraction
  - raw_structured: parsed Python object (dict for JSON, list[dict] for CSV,
                    None for PDF). The Raw Input Validator runs on this
                    BEFORE the LLM touches the document, so invalid input
                    cannot be silently healed.
  - declared_doc_type: the value of the top-level "doc_type" key if the user
                       included it in a JSON upload (trusted if it matches a
                       known DocType).
  - declared_crop: the value of the top-level "crop" key if present.

OCR for scanned PDFs is NOT implemented in Phase 1.
"""

import csv
import io
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

import pdfplumber

from app.schemas import DocType, PipelineError

BLOCK = "Pre-Processing"


@dataclass
class PreprocessResult:
    raw_text: str
    raw_structured: Any  # dict | list[dict] | None
    declared_doc_type: Optional[DocType]
    declared_crop: Optional[str]


def preprocess(filename: str, content: bytes) -> PreprocessResult:
    ext = Path(filename).suffix.lower()

    if ext == ".pdf":
        return PreprocessResult(
            raw_text=_extract_pdf(content),
            raw_structured=None,
            declared_doc_type=None,
            declared_crop=None,
        )
    if ext == ".json":
        data = _parse_json(content)
        return PreprocessResult(
            raw_text=json.dumps(data, indent=2, ensure_ascii=False),
            raw_structured=data if isinstance(data, dict) else None,
            declared_doc_type=_pick_declared_doc_type(data),
            declared_crop=_pick_declared_crop(data),
        )
    if ext == ".csv":
        rows, flat_text = _parse_csv(content)
        return PreprocessResult(
            raw_text=flat_text,
            raw_structured=rows,
            declared_doc_type=None,
            declared_crop=None,
        )

    raise PipelineError(
        block=BLOCK,
        reason=f"Unsupported file type: {ext}",
        detail="Only .pdf, .json, .csv are accepted in Phase 1",
        action_required="Re-upload the document as PDF, JSON, or CSV",
    )


# ---------------------------------------------------------------------------
# Self-declaration helpers (JSON only)
# ---------------------------------------------------------------------------


def _pick_declared_doc_type(data: Any) -> Optional[DocType]:
    if not isinstance(data, dict):
        return None
    raw = data.get("doc_type")
    if not isinstance(raw, str):
        return None
    try:
        return DocType(raw.strip().lower())
    except ValueError:
        return None


def _pick_declared_crop(data: Any) -> Optional[str]:
    if not isinstance(data, dict):
        return None
    raw = data.get("crop")
    if not isinstance(raw, str) or not raw.strip():
        return None
    return raw.strip().lower()


# ---------------------------------------------------------------------------
# Parsers
# ---------------------------------------------------------------------------


def _extract_pdf(content: bytes) -> str:
    try:
        with pdfplumber.open(io.BytesIO(content)) as pdf:
            pages = [page.extract_text() or "" for page in pdf.pages]
    except Exception as exc:
        raise PipelineError(
            block=BLOCK,
            reason="Failed to open PDF",
            detail=str(exc),
            action_required="Re-upload a valid PDF file",
        ) from exc

    text = "\n".join(pages).strip()
    if not text:
        raise PipelineError(
            block=BLOCK,
            reason="PDF has no extractable text",
            detail="File may be a scanned image. OCR is not supported in Phase 1.",
            action_required="Re-upload a text-based PDF",
        )
    return text


def _parse_json(content: bytes) -> Any:
    try:
        return json.loads(content.decode("utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise PipelineError(
            block=BLOCK,
            reason="Invalid JSON file",
            detail=str(exc),
            action_required="Re-upload a valid JSON file",
        ) from exc


def _parse_csv(content: bytes) -> tuple[list[dict[str, Any]], str]:
    try:
        text = content.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise PipelineError(
            block=BLOCK,
            reason="CSV file is not UTF-8 encoded",
            detail=str(exc),
            action_required="Re-save the CSV as UTF-8 and re-upload",
        ) from exc

    all_rows = list(csv.reader(io.StringIO(text)))
    if not all_rows:
        raise PipelineError(
            block=BLOCK,
            reason="CSV file is empty",
            action_required="Re-upload a CSV with content",
        )

    header = [h.strip() for h in all_rows[0]]
    records: list[dict[str, Any]] = []
    for raw_row in all_rows[1:]:
        if not any(cell.strip() for cell in raw_row):
            continue  # skip blank lines
        record = {}
        for i, col in enumerate(header):
            val = raw_row[i].strip() if i < len(raw_row) else ""
            record[col] = _coerce_scalar(val)
        records.append(record)

    # Feed the LLM the parsed records as JSON, not a re-joined CSV. Naive
    # ", ".join(row) drops CSV quoting — a cell containing a comma round-trips
    # as two cells and the LLM extracts the wrong values. JSON is unambiguous
    # and preserves cell boundaries by structure.
    flat_text = json.dumps(records, ensure_ascii=False, indent=2)
    return records, flat_text


def _coerce_scalar(s: str) -> Any:
    """Turn blank → None, numeric-looking → number, otherwise leave as string."""
    if s == "":
        return None
    try:
        if "." in s or "e" in s.lower():
            return float(s)
        return int(s)
    except ValueError:
        return s
