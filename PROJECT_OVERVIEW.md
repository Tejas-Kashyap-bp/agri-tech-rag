# AGRI-RAG — Project Overview

> Last updated: 2026-04-23
> Status: Part 1 in planning, Part 2 not started

---

## What Is This Project?

AGRI-RAG is an **AI-first agricultural advisory system**.

The idea is simple: instead of hardcoding rules ("if crop is maize and stage is S2, irrigate X liters"), we store ALL agronomic knowledge in a vector database and let an LLM reason over it. No logic in code. Logic lives in data.

**Core philosophy:**
> "We are not coding logic, we are storing knowledge and prompting logic."
> "Data is dynamic, versions are controlled, LLM uses latest truth."

---

## Who Is the Client?

A client (agronomist, farm operator, or agri company) uploads documents — stage definitions, irrigation schedules, IPM plans, etc. The system ingests them, processes them, and makes them queryable by an AI engine.

The system is designed to be handed off to the client. All code must be readable, documented, and migration-friendly (no hard lock-ins).

---

## The Two Parts of This Project

### Part 1 — Data Storage Pipeline (CURRENT)

> **Goal:** Take a raw uploaded document and store it cleanly in a vector database.

This covers everything from "client uploads a file" to "data is stored in ChromaDB and ready to be searched."

The pipeline:
```
Upload → Pre-Process → Heuristic Filter → LLM Classify →
[Human Confirm if needed] → Extract Structured JSON (with *_source fields) →
Evidence Checker → [Human Evidence Review if needed] →
Validate → Check Versions → Add Metadata →
Generate Text → Embed → Store in Vector DB
```

The Evidence Checker enforces the "no-inference" rule: every extracted value
must be traceable to a substring of the original document, and for numeric
values the closest keyword in that substring must match the field name.
Fields that fail this check route to human review rather than silently
landing in the vector store.

Full details in `decisions.md` and `PLAN.md`.

---

### Part 2 — Retrieval + Logic + RAG Pipeline (FUTURE)

> **Goal:** When a farmer asks a question or an engine needs data, retrieve the right knowledge and let the LLM reason over it.

This will cover:
- How retrieval works (what filters, what similarity threshold)
- How the LLM uses retrieved chunks to make decisions
- The full RAG pipeline (query → retrieve → prompt → response)
- How multiple engines (irrigation, nutrition, crop health, etc.) interact

**Part 2 has NOT been designed yet.** Do not make Part 1 decisions that assume Part 2 structure.

---

## Engines in This System

The system supports multiple advisory engines. Each document belongs to one:

| Engine ID | Engine Name     |
|-----------|----------------|
| e1_stage  | Stage Definition|
| e2_irrigation | Irrigation  |
| e3_nutrition  | Nutrition   |
| e4_crop_health| Crop Health |
| e5_yield      | Yield       |
| e6_financial  | Financial   |

---

## Document Types

Each engine can have documents of different types:

- `stage_definition`
- `fertigation_schedule`
- `ipm_schedule`
- `condition_rule`
- `treatment_mapping`
- `guardrail`
- `agronomic_knowledge`
- `crop_parameters`

---

## Key Design Decisions (Summary)

All decisions with full reasoning are in `decisions.md`. Quick reference:

| Decision | Choice |
|----------|--------|
| Chunking | 1 doc = 1 chunk (Phase 1) |
| doc_key format | `{crop}_{type}` (e.g. `maize_stage_definition`) |
| Versioning | Only one `is_active=true` per doc_key |
| Rollback strategy | Keep old active until new is confirmed, then flip |
| LLM auto-approve | ≥ 90% confidence → skip human confirmation |
| Pending TTL | 30 minutes |
| Validation failure | Client must re-upload (Phase 1) |
| Evidence enforcement | `<field>_source` required per value, verified by substring + keyword-proximity check |
| Preprocessing failures | Stop pipeline with structured error; no fallback parser |
| Vector DB | ChromaDB (Phase 1), migrate to Pinecone/other in production |
| LLM | Groq (Phase 1), abstracted so any LLM can replace it |

---

## What Is NOT in Scope for Part 1

- Retrieval strategy (how queries work)
- RAG pipeline
- Advisory response generation
- Multi-engine coordination
- Any frontend or dashboard
