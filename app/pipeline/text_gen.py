"""
Block: Text Generation

Converts the validated JSON + metadata into a flat, deterministic text string
suitable for embedding.

Explicitly NO LLM here. The text must be:
  - deterministic  (same input → same output always)
  - stable         (not prone to phrasing drift between runs)
  - chunk-ready    (Phase 1: one document = one chunk)

We use fixed markers [DOC_TYPE] and [CHUNK_TYPE] to make the embedded text
identifiable at retrieval time.
"""

import json
from typing import Any

from app.schemas import DocumentMetadata


def generate_text(doc: dict[str, Any], metadata: DocumentMetadata) -> str:
    header = (
        f"[DOC_TYPE:{metadata.type.value}] "
        f"[CHUNK_TYPE:full_document] "
        f"[ENGINE:{metadata.engine.value}] "
        f"[CROP:{metadata.crop}]"
    )
    description_line = f"Description: {metadata.description}"
    body_line = "Content:\n" + _flatten(doc)
    return f"{header}\n{description_line}\n{body_line}"


def _flatten(obj: Any, indent: int = 0) -> str:
    """
    Render a nested dict/list as plain text with stable ordering.

    We sort dict keys so the same input always produces the same output,
    regardless of the order the extractor emits them.
    """
    pad = "  " * indent

    if isinstance(obj, dict):
        lines = []
        for key in sorted(obj.keys()):
            value = obj[key]
            if isinstance(value, (dict, list)):
                lines.append(f"{pad}{key}:")
                lines.append(_flatten(value, indent + 1))
            else:
                lines.append(f"{pad}{key}: {value}")
        return "\n".join(lines)

    if isinstance(obj, list):
        lines = []
        for i, item in enumerate(obj):
            if isinstance(item, (dict, list)):
                lines.append(f"{pad}- [{i}]")
                lines.append(_flatten(item, indent + 1))
            else:
                lines.append(f"{pad}- {item}")
        return "\n".join(lines)

    return f"{pad}{json.dumps(obj, ensure_ascii=False)}"
