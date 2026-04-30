"""
Unit tests for replace_version_segment using a fake vector store.

WHY this exists:
  The version-replace path is two unstaged writes on Chroma (no transactions).
  The activation order matters: NEW must flip to active BEFORE OLD flips to
  inactive. Reversing the order silently leaves zero active docs after a
  crash. This is exactly the kind of correctness gap that test_retrieval.py
  cannot catch — retrieval works fine if the data is right, and the bug is
  in HOW the data got there.

The fake store records every call so we can assert on order. We also assert
the rollback path deletes the half-stored doc when the embedding/store call
raises, to confirm the partial-write protection is intact.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.pipeline import orchestrator as pipeline_orch  # noqa: E402
from app.schemas import (  # noqa: E402
    DocType, DocumentMetadata, Engine, PendingUpload, Priority, Source,
)
from datetime import datetime  # noqa: E402


class _FakeStore:
    def __init__(self, fail_on_store: bool = False):
        self.calls: list[tuple] = []
        self.fail_on_store = fail_on_store

    def store(self, stored, embedding):
        self.calls.append(("store", stored.metadata.doc_id, stored.metadata.is_active))
        if self.fail_on_store:
            raise RuntimeError("simulated chroma failure")

    def set_active(self, doc_id, crop, is_active):
        self.calls.append(("set_active", doc_id, is_active))

    def delete(self, doc_id, crop):
        self.calls.append(("delete", doc_id))


class _FakeEmbedder:
    def embed(self, _text):
        return [0.0] * 8


def _make_pending() -> PendingUpload:
    md = DocumentMetadata(
        doc_id="new-doc-id",
        doc_key="maize_stage_definition",
        engine=Engine.STAGE,
        type=DocType.STAGE_DEFINITION,
        crop="maize",
        version=2,
        is_active=True,
        priority=Priority.MEDIUM,
        source=Source.CLIENT_UPLOAD,
        description="Test doc",
    )
    return PendingUpload(
        upload_id="u-1",
        created_at=datetime.utcnow(),
        validated_doc={"crop": "maize", "stages": []},
        metadata=md,
        text_for_embedding="text body",
        existing_doc_id="old-doc-id",
        existing_version=1,
    )


def test_replace_activates_new_before_deactivating_old(monkeypatch):
    fake = _FakeStore()
    monkeypatch.setattr(pipeline_orch, "vector_store", fake)
    monkeypatch.setattr(pipeline_orch.embedder_mod, "embedder", _FakeEmbedder())

    pipeline_orch.replace_version_segment(_make_pending())

    set_active_calls = [c for c in fake.calls if c[0] == "set_active"]
    assert len(set_active_calls) == 2
    # Crash safety invariant: NEW activated first, OLD deactivated second.
    # Reverse order would leave zero active for this doc_key on a mid-flip
    # process kill, which silently breaks retrieval.
    assert set_active_calls[0] == ("set_active", "new-doc-id", True)
    assert set_active_calls[1] == ("set_active", "old-doc-id", False)


def test_replace_rolls_back_partial_store(monkeypatch):
    fake = _FakeStore(fail_on_store=True)
    monkeypatch.setattr(pipeline_orch, "vector_store", fake)
    monkeypatch.setattr(pipeline_orch.embedder_mod, "embedder", _FakeEmbedder())

    try:
        pipeline_orch.replace_version_segment(_make_pending())
    except RuntimeError:
        pass

    # No set_active should have been called — old version stays active.
    assert not any(c[0] == "set_active" for c in fake.calls)
    # The half-stored new doc must be deleted.
    assert ("delete", "new-doc-id") in fake.calls
