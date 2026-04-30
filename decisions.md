# AGRI-RAG — ARCHITECTURE DECISIONS RECORD

> Date: 2026-04-23
> Session: theory-data-save
> Phase: Phase 1 scoping

---

## CORE PRINCIPLES

> "Data is dynamic, versions are controlled, LLM uses latest truth."
> "We are not coding logic, we are storing knowledge and prompting logic."

**LLM Role:** LLM takes MOST decisions in this system. It is not a helper — it is the brain. All classification, summarization, description generation, and reasoning flows through LLM.

---

## COLLECTIONS STRATEGY

| Collection | When used |
|-----------|-----------|
| `{crop}_collection` | Documents specific to one crop (maize, apple, sugarcane, …) |
| `common_collection` | Documents NOT tied to any one crop — fertilizer compositions, micronutrient tables, pump specs, general agronomic rules, anything that applies across all crops |

**Routing rule:** The LLM classifier returns `"common"` as the crop value for shared documents. Any of `"common"`, `"none"`, `"all"`, `"all_crops"`, `"general"`, or empty string all map to `common_collection` in the vector store.

**Part 2 note:** When retrieving, queries must search BOTH the crop-specific collection AND `common_collection` to get the full picture.

---

## DOCUMENT STRUCTURE

Every document stored in the vector DB must follow this schema:

```json
{
  "doc_id": "uuid",
  "doc_key": "{crop}_{type}",
  "engine": "e1_stage | e2_irrigation | e3_nutrition | e4_crop_health | e5_yield | e6_financial",
  "type": "stage_definition | fertigation_schedule | ipm_schedule | condition_rule | treatment_mapping | guardrail | agronomic_knowledge | crop_parameters",
  "crop": "crop_name  (or 'common' for shared documents)",
  "version": 1,
  "is_active": true,
  "priority": "high | medium | low",
  "source": "client_upload | expert | icar",
  "description": "LLM-generated 1-2 line summary of document content",
  "body": {
    "raw_text": "original document or structured content"
  }
}
```

**Metadata** = all fields except `body`. Used for filtering, retrieval, and version control.

---

## DECISIONS

---

### 1. CHUNKING STRATEGY

**Decision: 1 doc = 1 chunk (Phase 1)**

- Documents are 1-pagers — no need to split
- Keeps retrieval simple and predictable
- Phase 2: revisit if documents grow or retrieval quality degrades

---

### 2. doc_key GENERATION

**Decision: Auto-generated as `{crop}_{type}`**

Examples:
- `maize_stage_definition`
- `maize_ipm_schedule`
- `maize_fertigation_schedule`

doc_key is the sole versioning anchor. Only one document per doc_key can have `is_active = true` at any time.

---

### 3. DESCRIPTION FIELD

**Decision: LLM-generated**

After structured extraction succeeds, LLM generates a 1-2 line description from the document content. The client does not write this manually.

---

### 4. LLM CLASSIFICATION — AUTO-APPROVE THRESHOLD

**Decision: Auto-approve at ≥ 90% confidence**

- Confidence ≥ 90% → skip human confirmation, auto-approve classification
- Confidence < 90% → route to human confirmation flow (Approve / Reject)

---

### 5. PENDING STATE EXPIRY (TTL)

**Decision: 30-minute TTL**

- Both pending states (pending_classification, pending_upload) expire after 30 minutes
- On expiry: delete pending record, no action taken
- Client must re-upload if expired

---

### 6. VALIDATION FAILURE RECOVERY (Phase 1)

**Decision: Client must re-upload**

- Validation fails → return structured `rejection_reason` to client:
  - Missing fields
  - Out-of-range values
  - Logical rule violations
- Client corrects document and re-uploads from scratch
- No in-place editing of pending uploads in Phase 1
- Phase 2: may allow patching/editing a pending upload

---

### 7. ROLLBACK ON STORAGE FAILURE (Version Replacement)

**Decision: Keep old active until confirmed (Option B)**

Steps when replacing an existing active version:
1. Store new document with `is_active = false`
2. Embed new document
3. Confirm successful storage and embedding
4. Only then: set old → `is_active = false`, new → `is_active = true`

Old version remains live and untouched until new version is 100% confirmed. If any step fails before the final flip, delete the failed new document. Old version never goes down.

---

### 8. STRUCTURED EXTRACTION FAILURE

**Decision: One retry with tighter prompt (Option B)**

- First extraction attempt fails → retry once with a more constrained template prompt
- Second attempt also fails → raise error

**Error format must specify the exact block and reason** so that anyone looking at the Excalidraw flow diagram can immediately identify which block is broken:

```json
{
  "error": true,
  "block": "Structured Extraction",
  "reason": "LLM failed to produce valid structured JSON after 2 attempts",
  "detail": "Response did not conform to expected schema on both attempts",
  "action_required": "Client must re-upload a cleaner or pre-structured document"
}
```

This applies to ALL error responses in the pipeline — every error must name the block it originated from.

---

### 9. RETRIEVAL STRATEGY

**Decision: Not finalized — deferred**

`is_active = true` will always be a mandatory filter. Full retrieval logic (crop, stage, region, etc.) to be defined in a separate session.

---

### 10. PENDING STATE PERSISTENCE

**Deferred to Phase 2**

- Recognized flaw: if server restarts during a WAIT state, pending records may be lost
- Phase 1 acceptable risk given 30-minute TTL keeps exposure window small
- Phase 2: persist pending states in a reliable store

---

### 11. EVIDENCE CHECKER (Post-Extraction Guardrail)

**Decision: LLM extraction must emit a `<field>_source` companion for every
extracted value. A post-extraction checker enforces three rules before the
document is allowed to proceed to Validation.**

This block exists because the LLM is strictly forbidden from inferring values
("no inference allowed — missing → null"). The Evidence Checker makes that
rule enforceable rather than merely requested.

**Three deterministic rules (regex/string-only, no NLP):**

1. **Source presence.** If `value` is non-null but `<field>_source` is null
   or missing → field flagged as unsupported.
2. **Source existence in raw text.** `<field>_source` must be a substring
   of the raw document text. Both sides are case-folded and whitespace-
   collapsed before comparison (handles Title-case quoting and PDF column
   padding). If not found → flagged. Rejects hallucinated page/table
   references.
3. **Numeric value ↔ source consistency with keyword proximity.** For
   numeric values: a matching number must appear in `<field>_source`, AND
   the *closest* known keyword within ±50 characters of that number must
   map to this field. Numbers inside `DAS N to M` / `stage N to M` style
   ranges are pre-tagged as distractors. Fields not in the keyword map
   (`kc`, `mad`, `root_depth_mm`, `ndvi_range`) keep a permissive fallback.

**Why closest-keyword-wins and not any-in-window:** co-located tables like
`"DAS 55 to 90, the Kc is 1.15"` put "Kc" within ±50 chars of "55", so
any-in-window would accept 55 as a Kc value. Closest-wins plus the range
pre-pass rejects it.

**Routing impact:** if any field is flagged, the upload becomes
`pending_evidence_review` — the human reviewer sees the dotted field paths
(e.g. `stages[2].kc`) and either confirms or rejects. Evidence issues take
routing priority over VERSION_CONFLICT.

**Out of scope (Phase 2):** unit equivalence (cm ↔ mm), percentage ↔
fraction normalization, range-containment semantics, Unicode lookalikes
(middle-dot, en-dash), OCR-induced digit confusion. These currently
produce false-flags that end up in evidence review — noisy but safe.

---

### 12. STRICT PREPROCESSING FAILURE HANDLING

**Decision: Any parse failure in Pre-Processing stops the pipeline
immediately with a structured error. No fallback parser. No silent defaults.**

Applies to JSON (`json.JSONDecodeError`), CSV (`UnicodeDecodeError` /
empty file), and PDF (pdfplumber failure / empty text). Each raises
`PipelineError(block="Pre-Processing", ...)` with `detail` carrying the
underlying parser message (including line/column for JSON).

Rationale: malformed input must not be silently "healed" into `{}` or
an empty dict — the LLM would then confidently extract from nothing.

---

## FULL PIPELINE (PHASE 1 AGREED)

```
Client uploads file (PDF / JSON / CSV)
        ↓
[Block: Pre-Processing]
Extract raw text, normalize format, OCR if needed
        ↓
[Block: Heuristic Pre-Filter]
Apply regex → produce possible_types[]
        ↓
[Block: LLM Classification Layer]
LLM determines: engine, crop, confidence score
        ↓
[if confidence < 90%] ──→ [Block: Human Confirmation]
                              Show predicted type + reason
                              Options: Approve / Reject
                              Create pending_classification
                              WAIT (30 min TTL)
                              Client responds →
                                Reject → Stop Pipeline
                                Approve → continue ↓
[if confidence ≥ 90%] ──→ Auto-approve, continue ↓
        ↓
[Block: Structured Extraction]
LLM converts document → structured JSON WITH `<field>_source` companions
  Attempt 1 fails → retry with tighter prompt
  Attempt 2 fails → raise error (block: Structured Extraction), stop
        ↓
[Block: Evidence Checker]
  Rule 1: every extracted value must carry a non-null `<field>_source`
  Rule 2: `<field>_source` must be a substring of raw_text
          (case-folded + whitespace-normalized)
  Rule 3: numeric values — a matching number must appear in the source,
          and the CLOSEST known keyword (±50 chars) must map to this field.
          DAS/stage range spans are pre-tagged as distractors.
  Any flagged fields → create pending_evidence_review, WAIT for human
  confirmation (takes routing priority over VERSION_CONFLICT below).
  Source fields are stripped AFTER this check, before Validation.
        ↓
[Block: Validation]
  Structure check: is JSON valid? required fields present?
  Range check: values within acceptable bounds?
  Logical check: continuity rules, no contradictions
  Fails → return rejection_reason (block: Validation), client re-uploads
        ↓
[Block: Check Existing Active Version]
Search vector DB by doc_key where is_active = true
        ↓
[NO match found]
  ↓
  [Block: Add Metadata]
  LLM generates description
  Set: version = 1, is_active = true
  ↓
  [Block: Text Generation]
  Convert JSON → semantically searchable text
  Use markers: [DOC_TYPE], [CHUNK_TYPE]
  No inference, no added meaning
  ↓
  [Block: Embedding]
  Convert chunk → vector (1 doc = 1 chunk)
  ↓
  [Block: Store in Vector DB]
  Store chunk + all metadata
  DONE

[YES match found — conflict]
  ↓
  [Block: Create Pending Upload]
  Create pending_upload record
  Return to client:
    upload_id, "similar document exists", options: [replace, reject]
  WAIT (30 min TTL)
  ↓
  Client confirms →
    REJECT: delete pending_upload, done
    REPLACE: continue ↓
  ↓
  [Block: Fetch Pending Upload]
  ↓
  [Block: Create New Version]
  Store new doc with is_active = false
  Embed new doc
  Confirm success
  Then: old → is_active = false, new → is_active = true, version++
  ↓
  [Block: Store in Vector DB]
  DONE
```

---

## WHAT IS DEFERRED TO PHASE 2

| Item | Reason deferred |
|------|----------------|
| Multi-chunk documents | Phase 1 docs are 1-pagers |
| Persistent pending state storage | 30-min TTL limits risk |
| In-place editing of pending uploads | Complexity not justified yet |
| Full retrieval strategy | Separate session |
| Human override of auto-approved classification | Not needed yet |

---

## ADVISORY FLOW — PRODUCTION READINESS NOTES (2026-04-28)

### E6 (financial assessment) is intentionally excluded from /advisory

The default multi-engine flow runs E1–E5 only. E6 lives outside this loop by design:

- **Trigger model is different.** E1–E5 produce a per-field daily/periodic advisory that always
  fires together (one farm, one DAS, one snapshot → five answers). E6 is on-demand: a farmer or
  agronomist asks "is this season financially viable" or "what's the break-even" — it is not
  computed every time the orchestrator runs.
- **Inputs are different.** E6 needs cost/price assumptions and a full season projection, not
  the current-day sensor snapshot the other engines consume. Bundling it into AdvisoryContext
  would force the caller to supply financial inputs they don't have for routine advisories.
- **Cost.** E6 is the most token-heavy engine (largest prompt, longest output). Running it on
  every /advisory call would 2–3× the LLM bill for value most calls don't use.

E6 knowledge is still ingested under `engine="e6_financial"` and is retrievable. A separate
endpoint will surface it when the financial-assessment UX lands.

### Advisory persistence — pending client decision

The /advisory response carries a `request_id`, full context echo, per-engine `source_docs`
(doc_key + version), and `parse_status`. All inputs needed for audit replay are present in the
response shape. Persisting these records (so an auditor can ask "what did we tell farmer X on
2026-04-12") is **deferred pending client approval** on:

- where to store (sqlite vs jsonl vs Chroma side-collection),
- retention period,
- whether to also persist the rendered prompt (for full reproducibility) or only the inputs.

Once a target is picked, the orchestrator gains a single `persist(record)` call after the loop.
No other code changes needed.

---

## ENGINE-SPECIFIC DECISIONS (2026-04-28)

### E5 (yield) — hybrid: LLM extracts inputs, code computes the number

The legacy system returned a single numeric yield (kg/acre, t/ha) from a deterministic formula.
The system philosophy bans hardcoded math, but a single yield number from an LLM is false
precision (the LLM is not actually multiplying anything). The compromise:

- **Knowledge stays in docs.** Crop-specific harvest index (HI) and biomass assumptions live in
  the `yield_parameters` document for that crop, ingested under `engine="e5_yield"`.
- **LLM extracts.** The engine prompt asks the LLM to read the retrieved doc and return the HI
  and biomass numbers it found, plus any qualitative adjustments (NDVI stress, weather risk).
- **Code multiplies.** A small helper inside the E5 engine path takes the extracted inputs and
  produces `expected_yield = biomass × HI` (with whatever crop-specific units the doc declares).
- **Output shape.** `summary` carries the farmer-facing one-liner. `details.expected_yield`
  carries the computed number. `details.inputs` carries `{harvest_index, biomass, source_doc}`
  so an auditor can verify the math.
- **Failure mode.** If the LLM cannot find HI or biomass in the docs, the engine returns
  `expected_yield: null` plus a `details.reasoning` explaining why — never a fabricated number.

This is the only engine that runs deterministic math. It is documented here so future readers
do not see the multiplication and assume the no-math rule has been abandoned.

### E2 (irrigation) — pump catalog lives in Supabase, not Chroma

The pump library and pump-selection rules are stored in Supabase tables (`pump_catalog`,
`pump_selection_rules`), not in the vector DB. Reasons:

- The catalog is structured tabular data with frequent updates; SQL is the right shape for it.
- Catalog rows reference inventory/pricing that other systems already own in Supabase.
- Storing it in Chroma would mean re-ingesting on every pump-spec update — expensive and noisy.

Implications for the E2 engine:

- E2's retrieval path hits **two** sources: Chroma (irrigation knowledge — Kc, MAD, root depth)
  and Supabase (pump catalog + selection rules).
- A new `app/storage/pump_repo.py` module will wrap the Supabase calls; the engine code stays
  unaware of the SQL.
- `source_docs` carries Chroma citations; pump-row citations get their own field
  (`details.pump_source` with `pump_id` + `updated_at`) so audit replay can find the exact
  pump record used.
- If Supabase is unreachable, E2 returns the irrigation advisory **without** pump selection and
  flags this in `details.reasoning` rather than failing the whole engine.

### E3 (fertilizer) — accuracy delta vs the legacy LP solver

The legacy system used a linear-programming solver to produce a least-cost fertilizer mix. The
LLM-driven engine produces a *plausible* mix from the retrieved nutrient/fertilizer documents,
not a cost-optimal one. This is an explicit accuracy trade-off, made for two reasons:

- The system spec forbids in-process solvers ("we are not coding logic").
- A least-cost solver requires real-time price data the system does not currently have.

The engine surfaces this honestly: each E3 advisory's `details.reasoning` includes a sentence
noting that the recommended mix is plausible but not guaranteed cost-optimal, and pointing
at the inputs (target nutrients × fertilizer compositions) the LLM used.

### Prompt versioning

Every `EngineSpec` carries a `prompt_version` string ("v1" by default). It is bumped manually
whenever the engine's focus / system prompt / template changes in a way that could shift
outputs, and it is echoed in the engine's response. This lets an audit replay tie a stored
advisory back to the exact prompt revision that produced it, without storing the full prompt
text in every record.
