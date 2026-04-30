"""
In-memory store for pipeline WAIT states.

Two separate WAIT points in the pipeline:
  - pending_classifications: after LLM Classification when confidence < 0.9
  - pending_uploads:          after Check Existing Active Version finds a conflict

Each record has a timestamp. On every read we check TTL and evict if expired.

DEPLOYMENT CONSTRAINT — single worker only:
  This store lives in one Python process's memory. Routing /upload to one
  worker and /confirm/* to another worker would lose the pending record. The
  API MUST be deployed with exactly --workers 1. If horizontal scaling is
  needed later, swap this for a persistent shared store (SQLite for single
  VM, Redis for multi-VM); the public interface (put/get/pop) is the
  contract — no callers should change.

A threading.RLock guards every map mutation/read so any future async
handlers that overlap on the same upload_id cannot race during get+pop.
"""

import threading
from datetime import datetime, timedelta
from typing import Optional

from app.config import settings
from app.schemas import PendingClassification, PendingUpload


class PendingStore:
    def __init__(self, ttl_minutes: int | None = None):
        self._ttl = timedelta(minutes=ttl_minutes or settings.PENDING_TTL_MINUTES)
        self._classifications: dict[str, PendingClassification] = {}
        self._uploads: dict[str, PendingUpload] = {}
        # RLock (not Lock) so a method that already holds the lock can call
        # another method that also takes it without deadlocking.
        self._lock = threading.RLock()

    # ---- TTL helpers ------------------------------------------------------

    def _expired(self, created_at: datetime) -> bool:
        return datetime.utcnow() - created_at > self._ttl

    def minutes_remaining(self, created_at: datetime) -> int:
        remaining = (created_at + self._ttl) - datetime.utcnow()
        return max(0, int(remaining.total_seconds() // 60))

    # ---- Pending classification ------------------------------------------

    def put_classification(self, record: PendingClassification) -> None:
        with self._lock:
            self._classifications[record.upload_id] = record

    def get_classification(self, upload_id: str) -> Optional[PendingClassification]:
        with self._lock:
            record = self._classifications.get(upload_id)
            if record is None:
                return None
            if self._expired(record.created_at):
                self._classifications.pop(upload_id, None)
                return None
            return record

    def pop_classification(self, upload_id: str) -> Optional[PendingClassification]:
        with self._lock:
            record = self.get_classification(upload_id)
            if record is not None:
                self._classifications.pop(upload_id, None)
            return record

    # ---- Pending upload (version conflict) -------------------------------

    def put_upload(self, record: PendingUpload) -> None:
        with self._lock:
            self._uploads[record.upload_id] = record

    def get_upload(self, upload_id: str) -> Optional[PendingUpload]:
        with self._lock:
            record = self._uploads.get(upload_id)
            if record is None:
                return None
            if self._expired(record.created_at):
                self._uploads.pop(upload_id, None)
                return None
            return record

    def pop_upload(self, upload_id: str) -> Optional[PendingUpload]:
        with self._lock:
            record = self.get_upload(upload_id)
            if record is not None:
                self._uploads.pop(upload_id, None)
            return record

    # ---- Generic status lookup (used by GET /status) ---------------------

    def status(self, upload_id: str) -> Optional[tuple[str, int]]:
        """
        Return (status_name, minutes_remaining) for any pending record.
        Returns None if nothing pending (expired or never existed).
        """
        with self._lock:
            c = self.get_classification(upload_id)
            if c is not None:
                return ("pending_classification", self.minutes_remaining(c.created_at))
            u = self.get_upload(upload_id)
            if u is not None:
                if u.requires_evidence_review:
                    return ("pending_evidence_review", self.minutes_remaining(u.created_at))
                return ("pending_version", self.minutes_remaining(u.created_at))
            return None


# Module-level singleton kept for back-compat. New callers should prefer
# get_pending_store() so tests can override the instance.
pending_store = PendingStore()


def get_pending_store() -> PendingStore:
    return pending_store
