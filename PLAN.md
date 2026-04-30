# AGRI-RAG Part 1 — Implementation Plan

> Last updated: 2026-04-23
> Scope: Data Storage Pipeline only
> Environment: conda agri
> Do not code Part 2 here.

---

## Quick Reference: What We Are Building

A REST API that accepts a document upload and runs it through a pipeline that ends with the document stored in ChromaDB as an embedding with full metadata.

Every step in this plan maps directly to a box in the Excalidraw diagram. If something breaks, the error response will tell you exactly which box failed.

---

## Environment

- **Conda env:** `agri`
- **Python:** 3.10+
- **Run all installs as:** `conda run -n agri pip install <package>`
- **Start server as:** `conda run -n agri uvicorn app.main:app --reload`

---

## Tech Stack — What and Why

| Tool | Purpose | Why |
|------|---------|-----|
| FastAPI | Web framework | Async, fast, auto-docs, easy for client handoff |
| ChromaDB | Vector database | Free, local, persistent. Client may switch to Pinecone later. |
| sentence-transformers | Embeddings | Local, free, no API key. Works if we go fully open source. |
| Groq | LLM provider | Current choice. Abstracted so it can be swapped. |
| pdfplumber | PDF text extraction | Better than PyPDF2 for complex PDFs |
| Pydantic | Data validation | Schema enforcement, readable error messages |
| python-dotenv | Env var loading | Keeps secrets out of code |
| pandas | CSV parsing | Standard, reliable |
| uvicorn | ASGI server | Runs FastAPI |

---

## Project Structure (Every File Explained)

```
agri-rag/
│
├── .env                          ← API keys, config. NEVER commit this.
├── requirements.txt              ← All pip dependencies
├── PROJECT_OVERVIEW.md           ← What this project is (read this first)
├── decisions.md                  ← Every decision we made and why
├── PLAN.md                       ← This file. How we build it.
├── DEPLOYMENT_NOTES.md           ← For client: how to migrate DB/LLM
│
└── app/
    │
    ├── main.py                   ← FastAPI app entry point. Registers routes.
    ├── config.py                 ← Loads .env, exposes settings to all modules
    │
    ├── schemas.py                ← ALL Pydantic models in one file.
    │                               Document structure, API request/response
    │                               shapes, error format, pending state shapes.
    │
    ├── api/
    │   └── routes/
    │       ├── upload.py         ← POST /upload
    │       │                       Accepts file, starts pipeline.
    │       │                       Returns upload_id immediately.
    │       │
    │       └── confirm.py        ← POST /confirm/classify/{upload_id}
    │                               POST /confirm/version/{upload_id}
    │                               GET  /status/{upload_id}
    │                               Handles both WAIT states.
    │
    ├── llm/
    │   ├── base.py               ← Abstract class LLMProvider.
    │   │                           Any LLM must implement this interface.
    │   │                           Changing LLM = swap one class, nothing else.
    │   │
    │   └── groq_provider.py      ← Groq implementation of LLMProvider.
    │                               Reads GROQ_API_KEY from .env.
    │
    ├── storage/
    │   ├── vector_store.py       ← Abstract class VectorStore + ChromaDB class.
    │   │                           Handles: store, search_by_doc_key, deactivate.
    │   │                           Changing DB = swap ChromaStore class only.
    │   │                           Collections: {crop}_collection, common_collection
    │   │
    │   └── pending_store.py      ← In-memory store for WAIT states.
    │                               Two dicts: pending_classifications, pending_uploads
    │                               Each record has a timestamp for 30-min TTL check.
    │                               PHASE 2: make this persistent (SQLite or Redis).
    │
    └── pipeline/
        │                         Each file = one block in Excalidraw diagram.
        │                         Each raises PipelineError with block name on failure.
        │
        ├── preprocessor.py       ← Block: Pre-Processing
        │                           Input:  raw uploaded file (PDF/JSON/CSV)
        │                           Output: plain text string
        │                           Does:   PDF → pdfplumber, JSON → load, CSV → pandas
        │                           OCR:    flagged as TODO if PDF has no text layer
        │
        ├── heuristic.py          ← Block: Heuristic Pre-Filter
        │                           Input:  plain text
        │                           Output: possible_types[] (e.g. ["irrigation","soil"])
        │                           Does:   regex patterns per doc_type
        │                           Why:    cheaper to run regex than LLM blindly
        │
        ├── classifier.py         ← Block: LLM Classification Layer
        │                           Input:  plain text + possible_types[]
        │                           Output: {engine, crop, doc_type, confidence, reason}
        │                           Does:   LLM prompt with possible_types as hints
        │                           Rule:   confidence >= 0.9 → auto approve
        │                                   confidence < 0.9  → human confirmation flow
        │
        ├── extractor.py          ← Block: Structured Extraction
        │                           Input:  plain text + confirmed classification
        │                           Output: structured JSON dict with `<field>_source`
        │                                   companions for every extracted value
        │                           Does:   LLM extracts fields based on doc_type schema
        │                           Rule:   retry once with tighter prompt if first fails
        │                                   if second fails → PipelineError with block name
        │
        ├── raw_input_validator.py ← Block: Raw Input Validation
        │                           Input:  parsed JSON/CSV structured object
        │                                   (None for PDFs) + confirmed classification
        │                           Output: nothing on success; PipelineError on failure
        │                           Does:   runs structure/range/logical rules on the
        │                                   original input BEFORE the LLM touches it,
        │                                   so invalid data cannot be silently healed
        │
        ├── evidence_checker.py   ← Block: Evidence Checker
        │                           Input:  extracted JSON (with `*_source`) + raw_text
        │                           Output: list of dotted field paths that fail
        │                                   evidence rules (e.g. "stages[2].kc")
        │                           Three rules (all regex/string-only):
        │                             1. value non-null but `<field>_source` null → flag
        │                             2. `<field>_source` not a substring of raw_text
        │                                (case-folded + whitespace-collapsed) → flag
        │                             3. numeric values — a matching number must appear
        │                                in the source AND the closest known keyword
        │                                within ±50 chars must map to this field.
        │                                Numbers inside DAS/stage ranges are distractors.
        │                           Flag set non-empty → pending_evidence_review
        │                           Source fields are stripped after check runs.
        │
        ├── validator.py          ← Block: Validation
        │                           Input:  structured JSON
        │                           Output: validated JSON or detailed error
        │                           Three sequential checks:
        │                             1. Structure  → Pydantic model per doc_type
        │                             2. Range      → hardcoded rules (NDVI, Kc, etc.)
        │                             3. Logical    → continuity rules per doc_type
        │                           On fail: returns structured rejection_reason,
        │                                    client must re-upload. No editing in Phase 1.
        │
        ├── metadata.py           ← Block: Add Metadata
        │                           Input:  validated JSON + classification result
        │                           Output: full document dict ready for storage
        │                           Does:   builds doc_key ({crop}_{type})
        │                                   sets version (1 for new, N+1 for replacement)
        │                                   sets is_active = True
        │                                   LLM generates 1-2 line description
        │                                   sets source, engine, crop, doc_type
        │
        ├── text_gen.py           ← Block: Text Generation
        │                           Input:  validated JSON + metadata
        │                           Output: plain text string ready for embedding
        │                           Does:   template-based conversion (NO LLM here)
        │                                   uses markers [DOC_TYPE], [CHUNK_TYPE]
        │                                   no inference, no added meaning
        │                           Why no LLM: we want deterministic, stable text
        │
        └── embedder.py           ← Block: Embedding
                                    Input:  text string
                                    Output: vector (list of floats)
                                    Does:   sentence-transformers encode
                                    Rule:   1 doc = 1 embedding (Phase 1)
                                    Note:   abstracted like LLM provider for future swap
```

---

## API Design

### POST /upload
Client uploads a file and starts the pipeline.

**Request:** multipart/form-data with file

**Happy path (confidence ≥ 90%, no version conflict):**
```json
{ "status": "stored", "upload_id": "uuid", "doc_key": "maize_stage_definition" }
```

**Needs human classification confirmation:**
```json
{
  "status": "pending_classification",
  "upload_id": "uuid",
  "predicted": { "engine": "e1_stage", "crop": "maize", "type": "stage_definition" },
  "reason": "LLM explanation of why it thinks this",
  "confidence": 0.74,
  "options": ["approve", "reject"]
}
```

**Version conflict:**
```json
{
  "status": "pending_version",
  "upload_id": "uuid",
  "existing_version": 2,
  "message": "A document with key maize_stage_definition already exists (v2, active)",
  "options": ["replace", "reject"]
}
```

**Evidence review (LLM emitted values without supporting source text):**
```json
{
  "status": "pending_evidence_review",
  "upload_id": "uuid",
  "flagged_fields": ["stages[2].kc", "stages[2].root_depth_mm"],
  "message": "LLM-extracted values could not be traced back to the source document",
  "options": ["confirm", "reject"]
}
```
Evidence review takes routing priority over version conflict — if both would
apply, the client sees `pending_evidence_review` first.

---

### POST /confirm/classify/{upload_id}
**Request:**
```json
{ "decision": "approve" }
```
or
```json
{ "decision": "reject" }
```

On approve → resumes pipeline from Structured Extraction
On reject → deletes pending, returns `{ "status": "stopped" }`

---

### POST /confirm/version/{upload_id}
**Request:**
```json
{ "decision": "replace" }
```
or
```json
{ "decision": "reject" }
```

On replace → write-then-swap (store new inactive → embed → confirm → flip)
On reject → delete pending, return `{ "status": "rejected" }`

---

### GET /status/{upload_id}
Returns current state of any pending upload. Also checks TTL.

```json
{ "status": "pending_classification", "expires_in_minutes": 22 }
```

---

## Error Format — Every Block Must Follow This

```python
# Every error raised anywhere in pipeline/ must use this format
{
  "error": true,
  "block": "Structured Extraction",   # exact name of Excalidraw box
  "reason": "short human-readable reason",
  "detail": "technical detail if useful",
  "action_required": "what client should do next"
}
```

This means when you look at an error response, you can open the Excalidraw diagram and immediately point to the box that failed.

---

## Pending State — How WAIT Works

The pipeline is not truly async. WAIT means:

1. Pipeline runs up to the decision point
2. Intermediate state (classified doc, validated doc, etc.) is saved to in-memory dict with timestamp
3. API returns response to client with upload_id
4. Client calls confirm endpoint later
5. Confirm endpoint fetches saved state, resumes pipeline from that point

```python
# What gets saved in pending_classifications
{
  "upload_id": "uuid",
  "created_at": datetime,
  "raw_text": "...",           # from preprocessor
  "possible_types": [...],     # from heuristic
  "classification": {...},     # from classifier
  "original_filename": "..."
}

# What gets saved in pending_uploads
{
  "upload_id": "uuid",
  "created_at": datetime,
  "validated_doc": {...},      # full validated + metadata added doc
  "text_for_embedding": "...", # ready to embed
  "existing_doc_id": "...",   # the old version in ChromaDB
  "existing_version": 2
}
```

TTL check: on every confirm request, check `now - created_at > 30 minutes`. If expired, return error, delete record.

---

## Version Conflict — Write-Then-Swap Sequence

This is the rollback-safe approach (Option B: keep old active until new is confirmed):

```
1. Store new document in ChromaDB with is_active=False
2. Generate embedding for new document
3. Store embedding in ChromaDB (still is_active=False)
4. Confirm storage succeeded
5. Only now: set old doc is_active=False
6. Set new doc is_active=True
7. Increment version number
```

If steps 1-4 fail → delete the failed new doc. Old version was never touched.
Old version stays live throughout until step 5.

---

## ChromaDB Collections Strategy

```
maize_collection      → all maize documents
apple_collection      → all apple documents
sugarcane_collection  → all sugarcane documents
common_collection     → shared docs (pumps, general rules, etc.)
```

Every document stored has these metadata fields available for filtering:
```
doc_key, doc_id, engine, type, crop, version, is_active, source, priority
```

Every retrieval query MUST filter `is_active = true`. This is not optional.

---

## Validation Rules Reference

### Structure Check (Pydantic per doc_type)
- Is JSON parseable?
- Are required fields present and non-null?

### Range Check (hardcoded rules)
- NDVI: must be in [-1, 1]
- Kc (crop coefficient): must be in [0.1, 1.5] typically
- DAS (days after sowing): must be positive integers
- No negative values where they make no sense

### Logical Check (per doc_type)
- For stage definitions: stages must not overlap, must be continuous
- For schedules: no gaps between application windows
- Rules will grow as we understand each doc_type better

---

## LLM Prompt Strategy

### Classification prompt
- System: "You are an agricultural document classifier..."
- User: raw_text + possible_types from heuristic as hints
- Output: JSON with engine, crop, doc_type, confidence (0-1), reason

### Extraction prompt (first attempt)
- System: "You are an agricultural data extractor. Return only valid JSON."
- User: raw_text + target schema for the doc_type
- Output: structured JSON

### Extraction prompt (retry — tighter)
- System: same
- User: same + "Your previous attempt failed. Here is exactly the schema you must follow: ..."
- Output: structured JSON or error

### Description generation
- System: "Write a 1-2 sentence description of this agricultural document."
- User: structured JSON
- Output: plain text description (stored in metadata)

---

## Running the Server

```bash
conda run -n agri uvicorn app.main:app --reload --port 8000
```

- **API base:** `http://localhost:8000`
- **Swagger UI (interactive docs):** `http://localhost:8000/docs`
- **Dev frontend:** open `frontend/index.html` in your browser (file:// works, CORS is open)

The frontend is gitignored — it is a local dev tool only and is not part of the client deliverable.

---

## Order of Development

Build in this exact sequence. Each step is independently testable.

```
Step 1: schemas.py          → define all data shapes first
Step 2: config.py           → env loading
Step 3: llm/base.py         → abstract interface
Step 4: llm/groq_provider.py → Groq implementation, test with a simple prompt
Step 5: storage/vector_store.py → ChromaDB wrapper, test store + search
Step 6: storage/pending_store.py → in-memory dict with TTL
Step 7: pipeline/preprocessor.py → test with a sample PDF and CSV
Step 8: pipeline/heuristic.py    → test with sample texts
Step 9: pipeline/classifier.py   → test with Groq
Step 10: pipeline/extractor.py         → test with Groq (include *_source fields)
Step 11: pipeline/raw_input_validator.py → test with valid + invalid parsed input
Step 12: pipeline/evidence_checker.py  → test with testing/ fixtures (see below)
Step 13: pipeline/validator.py         → test with valid + invalid JSON
Step 14: pipeline/metadata.py          → test metadata building
Step 15: pipeline/text_gen.py          → test text output format
Step 16: pipeline/embedder.py          → test vector output
Step 17: api/routes/upload.py          → wire pipeline together
Step 18: api/routes/confirm.py         → wire confirm flows (classify + evidence + version)
Step 19: app/main.py                   → register routes, launch
Step 20: End-to-end test               → upload a real document, check ChromaDB
```

## Test Fixtures

The `testing/` folder at the project root contains adversarial fixtures
organized by category. Use them to regression-test the evidence checker,
validators, and classifier as you build:

- `testing/A_adversarial/` — vague language, implicit values, mixed signals
- `testing/B_edge_cases/` — NDVI/DAS boundaries, overlaps, gaps, missing stages
- `testing/C_structural_traps/` — empty strings, wrong types, malformed JSON
- `testing/D_classification_confusion/` — multi-crop, missing crop, multi-engine
- `testing/E_evidence_attacks/` — unsourced values, hallucinated refs, contradictions
- `testing/F_valid_and_conflict/` — clean happy-path docs + version-conflict case
- `testing/final_readiness/` — near-valid real-world cases, unit/format traps,
                                co-located number confusion (F06, F18)

`testing/TEST_REPORT.md` and `testing/FINAL_TEST_REPORT.md` map each fixture
to its expected pipeline path (VALID / INVALID / AMBIGUOUS / EVIDENCE_REVIEW
/ VERSION_CONFLICT) and document which system weakness it probes.

---

## What NOT To Do

- Do not hardcode any logic that belongs in a document (that's Part 2's domain)
- Do not write validation rules that are too specific to one crop — keep them general
- Do not use `is_active = false` as a delete — we keep history
- Do not put secrets in code — always use `.env`
- Do not skip the pending state pattern to "simplify" — the WAIT is a design requirement
- Do not let two docs with the same doc_key both have `is_active = true`
- Do not add retrieval logic here — that is Part 2

---

## Files That Go to Client

When this project is handed off:
- All `app/` code
- `requirements.txt`
- `.env.example` (not `.env`)
- `PROJECT_OVERVIEW.md`
- `decisions.md`
- `DEPLOYMENT_NOTES.md`
- `PLAN.md`

The client does NOT get our internal conversation notes.
