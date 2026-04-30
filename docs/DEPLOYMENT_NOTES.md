# Deployment & Web-Layer Notes

**Audience:** the team owning the deployment surface — CORS, auth, request handling, logging, infra, secret management.

This document is a heads-up from the AI / RAG side. The items below sit on the **web / deployment / DevOps** boundary — they are not RAG correctness issues and are deliberately out of scope for the AI pipeline work. They were surfaced by a recent production audit and are listed here so they don't fall through the cracks during go-live.

The AI pipeline (ingestion + advisory) has been hardened separately. What follows is everything *around* it.

---

## Why this list exists

The audit covered the full repository, including web framework configuration, request handling, and deployment posture. Several items are infrastructure / web concerns rather than AI / data concerns. They're documented here in plain language so the right team can pick them up. None of these are about implementation style — they affect security, availability, or observability of the deployed service.

Severity tags below mirror the audit's own taxonomy: 🔴 critical (block production), 🟠 major (fix before scale), 🟡 minor.

---

## 🔴 Critical — should be addressed before opening the API to public traffic

### 1. CORS is currently fully open

**Location:** `app/main.py:36-41`

The current configuration allows requests from any origin, with any method, any header. The inline comment "Tighten this in production" flags the intent.

**Why it matters:** the API exposes mutating endpoints. With wildcard CORS, any webpage a user visits can issue requests against the API from that user's browser. If the deployment ever ends up reachable from a real browser session, this is exploitable.

**Suggested direction (heads-up only — final approach is up to whoever owns the web layer):**
- Drive `allow_origins` from a settings value (e.g. `CORS_ORIGINS`), default empty.
- Permit `*` only when `ENVIRONMENT == "dev"`.
- Refuse to boot in non-dev environments if the allow-list is empty.

### 2. Unauthenticated DELETE endpoints

**Location:** `app/api/routes/browse.py:40-65`

`DELETE /db/delete/{doc_id}` and `DELETE /db/clear-all` are reachable by anyone who can hit the API. The file describes itself as "dev-only" but nothing enforces that — both routes are mounted unconditionally on the same app, alongside the production endpoints.

**Why it matters:** a single curl wipes the entire vector knowledge base. There is no audit log, no soft-delete, no recovery path. Combined with the open CORS above, a victim's browser could trigger this on their behalf.

**Suggested direction:**
- Only register `browse_router` when `settings.ENVIRONMENT == "dev"`.
- If these endpoints are needed in production for ops, gate them behind an admin token header AND a structured audit log of every delete.
- Either way, soft-delete is safer than hard-delete here — the doc-versioning machinery already supports `is_active=false`.

---

## 🟠 Major — should be addressed before scaling beyond a small pilot

### 3. No startup secret validation

**Location:** `app/config.py`

Required keys (e.g. `GEMINI_API_KEY`, Supabase credentials) default to empty strings. A misconfigured deployment boots cleanly, accepts uploads, then 500s on the first LLM call inside a real request — after the user has waited.

**Suggested direction:** add a startup hook that asserts non-empty values for the active provider's keys + Supabase. Fail closed at boot. This is purely a deployment-confidence improvement; nothing about the AI pipeline changes.

### 4. `datetime.utcnow()` is used in several places

**Locations include:** `app/storage/pending_store.py`, `app/pipeline/orchestrator.py`, `app/api/routes/farm_advisory.py`, and elsewhere.

`datetime.utcnow()` is deprecated as of Python 3.12. It returns naive datetimes, which can cause subtle TTL-math bugs when crossing serialization boundaries (e.g. naive vs aware comparisons). The fix is mechanical: `datetime.now(timezone.utc)`.

**Suggested direction:** if there is a planned Python upgrade, batch this with that effort. Otherwise it can be picked up as routine hygiene. No behavior change is intended.

### 5. Bare `except Exception` returning `str(exc)` to clients

**Locations:** `app/api/routes/upload.py:96-105`, `app/api/routes/confirm.py:171-181`

Internal exception messages are returned in the HTTP response body, and tracebacks are not logged. Two issues with the same root cause:
- **Information leakage:** internal paths, dependency names, occasionally secrets buried in third-party error strings end up visible to the caller.
- **Operability:** without server-side `log.exception(...)`, root causes are invisible to whoever is debugging.

**Suggested direction:** `log.exception(...)` server-side, return a generic message + correlation ID to the caller. If a central FastAPI exception handler is desirable, that is also a good fit here.

### 6. Unbounded upload size

**Location:** `app/api/routes/upload.py:29`

The endpoint does `await file.read()` — the entire upload is buffered into memory with no size cap. A single very large POST can OOM-kill the worker.

**Suggested direction:** enforce a `Content-Length` ceiling appropriate for expected document sizes (5–10 MB is generous for the document types this system handles). Reject early with HTTP 413.

### 7. `logging.basicConfig` runs at import time

**Location:** `app/main.py:22-25`

`logging.basicConfig(...)` is called as a side effect of importing `main`. This silently overrides any logger configuration set by uvicorn's `--log-config`, by tests, or by a deployment-wrapper logger (e.g. structured-logging adapters).

**Suggested direction:** move this into a startup hook, or into a dedicated `app/logging_config.py` invoked from the entry point. Lets the deployment supply its own logging without conflict.

---

## 🟡 Minor — pick up opportunistically

### 8. `allow_credentials` not set on CORS middleware

Becomes moot once CORS is properly tightened (item 1).

### 9. `_INTEGRATED_PATH` resolution is silent on missing directory

**Location:** `app/config.py:30`

If the sibling `Agri-integrated` repo is not present, the path is silently ignored. A debug-level log when the sibling is missing would make first-deploy issues much easier to diagnose.

### 10. Some over-commented blocks

A few modules carry comments restating what the code already says. Style preference — trim opportunistically when touching the file for other reasons.

### 11. Verify routing non-overlap

There is an import path `from app.api.routes import advisory` that should be verified against `farm_advisory.py` to make sure both routers are not double-registering paths. Quick sanity check during deployment review.

---

## What's already been addressed (for context)

The AI pipeline side has been hardened along these axes — listed here so the deployment review can rely on these as fixed:

- Pending-state concurrency: an `RLock` now guards the in-process pending store. **Single-worker constraint is documented in `app/storage/pending_store.py`** — please run with `--workers 1` until/unless the store is migrated to SQLite or Redis. This is the most important deployment guardrail in this document.
- Version replace order is now crash-safe (new doc activated before old is deactivated).
- LLM provider and vector store are no longer instantiated at import time.
- Per-request thread-pool was removed in `/farm-advisory`; weather + soil are now fetched via `asyncio.to_thread + gather`.
- Per-engine LLM timeout halved to keep retries inside the orchestrator's per-engine budget.
- Transient transport errors against Gemini get one jittered retry inside the provider.
- CSV preprocessing now feeds JSON to the LLM (previous code dropped CSV quoting).
- `body` field in stored documents is now valid JSON, not Python `repr`.
- Engine dependency graph centralized — adding a new engine is one edit.
- Unit tests added for evidence checker proximity logic, version replace ordering / rollback, and orchestrator deadline math.

---

## Single most important item

If only one thing on this list gets done before public traffic: **CORS + the unauthenticated DELETE endpoints** (items 1 and 2). Everything else has a much smaller blast radius.
