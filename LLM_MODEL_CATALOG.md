# LLM Model Catalog and Recommendation

**Project:** AGRI-RAG — AI-First Agricultural Advisory System
**Document type:** Model selection brief for client review
**Prepared for:** Client procurement and infrastructure team
**Status:** For client decision

---

## 1. Purpose of this document

The AGRI-RAG system depends on a Large Language Model (LLM) at multiple stages of the data ingestion and advisory pipeline. The LLM is the single most important runtime dependency in the architecture — it performs document classification, structured data extraction with audit-grade evidence quoting, and farmer-facing advisory generation.

This document presents the three open-source LLMs we have shortlisted for self-hosted deployment on client infrastructure, the requirements they were evaluated against, and our final recommendation. The objective is to give the client a clear, defensible basis for selecting one of these models for procurement and deployment.

---

## 2. System requirements that drive the LLM choice

The LLM must satisfy the following requirements derived from the AGRI-RAG architecture and the agreed first-wave deployment scope.

### 2.1 Functional requirements

| # | Requirement | Why it matters |
|---|-------------|----------------|
| F1 | **Strict structured (JSON) output** with zero compromise on schema validity. | The pipeline has multiple stages (classifier, structured extractor, advisory generator) where malformed output causes a hard failure of the upload or a degraded fallback. The extractor stage additionally requires every extracted value to carry a companion `{field}_source` field quoting the exact supporting text from the original document — an audit and compliance requirement. |
| F2 | **Regional (Indic) language support** at "good, not best" quality. Hindi plus other major Indic languages (Bengali, Marathi, Tamil, Telugu, Urdu, Gujarati, Punjabi). | Farmer-facing advisories in Phase 2 will need to be generated in regional languages. State-of-the-art Indic quality is not required, but reliable coverage across major languages is mandatory. |
| F3 | **Reliable numeric reasoning grounded in retrieved knowledge.** | Advisories include irrigation amounts, fertigation schedules, dosage thresholds, and stage-based parameters. The model must reason over numeric values present in retrieved documents without inventing or rounding figures. Heavy mathematical computation is not required; faithful numeric grounding is. |

### 2.2 Non-functional requirements

| # | Requirement | Why it matters |
|---|-------------|----------------|
| N1 | **Open-source / open-weights license, commercially usable** at a 100,000-client deployment scale. | The client is self-hosting, not consuming a paid API. License terms must permit commercial use without per-seat or per-MAU restrictions that would be triggered at this scale. |
| N2 | **Self-hostable on client-procured GPU servers.** | The deployment model is on-premise / private cloud. The model must run on commercially available GPU hardware (A100 / H100 class). |
| N3 | **Production-grade throughput** sufficient to serve a queued backend handling 100,000 first-wave clients. | The backend uses multiple queues and batched inference. The model and serving stack must support continuous batching and high concurrency. |
| N4 | **Provider-swappable architecture compatibility.** | The codebase already implements an `LLMProvider` abstract interface. Replacing the current Groq integration with a self-hosted model must require only a new provider class and a single wiring change — no pipeline code changes. |
| N5 | **Mature inference tooling** — must work with vLLM or SGLang for production serving, and must support grammar-constrained decoding (xgrammar / outlines / lm-format-enforcer) to enforce JSON schemas at the decoder level. | Native "JSON mode" alone is insufficient for production reliability. Grammar-constrained decoding makes invalid JSON mathematically impossible, which is the only acceptable guarantee for a pipeline where malformed output hard-fails ingestion. |

### 2.3 Out of scope

The following are explicitly not part of this LLM selection:

- **Embeddings** — handled by a separate local model (`all-MiniLM-L6-v2`); not affected by the LLM choice.
- **Vector database** — ChromaDB in Phase 1, with a documented migration path to Pinecone or equivalent in production.
- **Phase 2 retrieval strategy** — not finalized; the LLM choice is not optimized around assumed retrieval shape.
- **Frontend / UX** — separate concern.

---

## 3. Architecture context — where the LLM is used

The AGRI-RAG codebase calls the LLM at the following pipeline stages. This list defines the workload the chosen model must handle reliably.

| Pipeline stage | LLM responsibility | Failure mode if model is unreliable |
|----------------|--------------------|--------------------------------------|
| Heuristic + LLM Classifier | Identify engine / crop / document type with a confidence score, return as JSON. | Misclassification routes documents to the wrong knowledge collection. |
| Structured Extractor | Convert raw document to strict JSON, with `{field}_source` quoting per non-null field. Two failed parse attempts hard-fail the upload. | Hard upload failure; client must re-upload. Audit traceability is broken if `{field}_source` discipline degrades. |
| Advisory Generator (per engine) | Reason over retrieved knowledge documents and produce a JSON advisory with `summary` + `details` (including `reasoning`). Two failed attempts produce a degraded `parse_status: fallback` response. | Degraded farmer-facing output; advisory quality drops. |
| Metadata generation (titles, descriptions) | Free-form short text generation. | Lower stakes; cosmetic impact only. |

The provider interface (`app/llm/base.py`) exposes two methods: `complete()` for free-form text and `complete_json()` for schema-bound output. Any model under consideration must support both modes reliably under a self-hosted serving stack.

---

## 4. Shortlisted models

Three open-source models have been shortlisted after evaluation against the requirements above. All three are capable of running the full AGRI-RAG workload. They differ in license terms, hardware footprint, and language coverage.

---

### 4.1 Option A — Qwen3-32B-Instruct (Recommended)

| Attribute | Detail |
|-----------|--------|
| Vendor | Alibaba Cloud |
| Architecture | 32B parameter dense transformer |
| Release | April 2025 |
| License | **Apache 2.0** |
| Languages | 119 languages including all in-scope Indic languages |
| Hardware per replica | 1 × NVIDIA A100 80 GB (BF16) or 1 × A100 40 GB (AWQ INT4) |
| Inference stack | vLLM, SGLang, TensorRT-LLM (all supported) |
| Structured output | Native structured-output training; verified with grammar-constrained decoding (xgrammar) |

**Strengths**

- **Cleanest license of any shortlisted option.** Apache 2.0 across all Qwen3 sizes. No commercial-use clauses to negotiate, no MAU caps, no separate commercial license required for the client's deployment scale.
- **Lowest hardware footprint per replica.** Single A100 80 GB is sufficient. At 100,000-client scale this materially reduces capital and operating cost compared to 70B-class models that need 2× A100 80 GB per replica.
- **Broadest language coverage.** Trained on 119 languages — the widest of any shortlisted model — covering all regional languages in the project scope.
- **Capability parity with larger models.** Qwen3-32B benchmarks at or above Qwen2.5-72B on standard reasoning tasks despite being smaller; the smaller size is not a quality compromise.
- **Strong structured-output discipline.** Trained with explicit reinforcement on structured output and tool-calling. Works with vLLM + xgrammar grammar-constrained decoding out of the box, satisfying requirement F1.

**Considerations**

- **Shorter production track record than Qwen2.5.** Released April 2025; less time in production than the Qwen2.5 family. This is mitigated by significant production deployment by other organizations through the year, and by the project's two-call retry policy at the pipeline level.
- **Domain-specific evaluation still required.** As with any model swap, the client should plan for an A/B evaluation against the current Groq-hosted model on a representative sample of agricultural documents before committing to production rollout (see Section 6).

---

### 4.2 Option B — Qwen2.5-72B-Instruct

| Attribute | Detail |
|-----------|--------|
| Vendor | Alibaba Cloud |
| Architecture | 72B parameter dense transformer |
| Release | September 2024 |
| License | **Qwen License** (commercial use permitted; review recommended) |
| Languages | 29 languages including all in-scope Indic languages |
| Hardware per replica | 2 × NVIDIA A100 80 GB (BF16) or 1 × H100 80 GB (FP8) |
| Inference stack | vLLM, SGLang, TensorRT-LLM (all supported) |
| Structured output | Explicitly improved for JSON output in the 2.5 release; verified with xgrammar |

**Strengths**

- **Longer production track record.** Released September 2024; widely deployed and well-characterized in production environments.
- **Strong multilingual and JSON discipline.** Among the most reliable open models for JSON output under grammar-constrained decoding. Indic coverage is solid across all in-scope languages.
- **Strong numeric reasoning.** Performs well on the document-grounded numeric reasoning that the advisory generator requires.

**Considerations**

- **License requires review.** Qwen License is permissive for commercial use but is not Apache 2.0 / OSI-certified. The client's legal team should confirm acceptability. This is a process step, not a blocker.
- **Higher hardware cost.** Requires 2 × A100 80 GB per replica versus 1 × A100 80 GB for Option A. At 100,000-client scale this approximately doubles the GPU footprint for the LLM tier.
- **No meaningful capability advantage over Option A** for this workload. The case for Option B is primarily production maturity, not capability.

---

### 4.3 Option C — Llama-3.3-70B-Instruct

| Attribute | Detail |
|-----------|--------|
| Vendor | Meta |
| Architecture | 70B parameter dense transformer |
| Release | December 2024 |
| License | **Llama 3 Community License** (permits commercial use up to 700M monthly active users) |
| Languages | English-primary; Hindi workable; other Indic scripts limited |
| Hardware per replica | 2 × NVIDIA A100 80 GB (BF16) or 1 × H100 80 GB (FP8) |
| Inference stack | vLLM, SGLang, TensorRT-LLM, llama.cpp (most mature ecosystem) |
| Structured output | Strong instruction-following; works with xgrammar |

**Strengths**

- **Best-in-class instruction following.** Highest IFEval scores among open models. The "do not infer; quote source or set null" discipline required by the structured extractor is exactly the kind of instruction Llama 3.3 follows most reliably.
- **Lowest-friction migration path.** The current Groq-hosted integration uses `llama-3.3-70b-versatile` — the same model family. Migrating to a self-hosted Llama 3.3-70B is the most predictable transition with the smallest behavioural delta.
- **Most mature inference ecosystem.** Widest community support across vLLM, TensorRT-LLM, llama.cpp, and quantization toolchains.

**Considerations**

- **Indic language gap is the primary concern.** Llama 3.3 is English-optimized. Hindi is workable. Tamil, Telugu, Bengali, Gujarati, and other Indic scripts are not reliable enough at production quality for farmer-facing advisories without further fine-tuning. This directly conflicts with requirement F2.
- **License requires acknowledgement.** The Llama Community License caps free use at 700M monthly active users. The client's deployment is well within this cap, so commercial use is permitted, but the license is not OSI-certified open source.
- **Higher hardware cost than Option A.** Same 2 × A100 80 GB per replica footprint as Option B.

**When this option becomes preferable:** if the deployment scope is narrowed to English and Hindi only, Llama-3.3-70B becomes the strongest choice on the basis of instruction-following maturity. For the full multi-Indic scope agreed for AGRI-RAG, it is not recommended as the primary model.

---

## 5. Side-by-side comparison

| Criterion | Option A: Qwen3-32B | Option B: Qwen2.5-72B | Option C: Llama-3.3-70B |
|-----------|---------------------|------------------------|--------------------------|
| License | Apache 2.0 | Qwen License | Llama 3 Community |
| Commercial use at 100k clients | Permitted, no review needed | Permitted, legal review recommended | Permitted (under 700M MAU cap) |
| Hardware per replica | 1 × A100 80 GB | 2 × A100 80 GB | 2 × A100 80 GB |
| Indic language coverage | Broadest (119 languages) | Solid (29 languages) | Hindi only at production quality |
| JSON / structured output | Native, xgrammar-verified | Native, xgrammar-verified | Strong, xgrammar-verified |
| Numeric reasoning | Strong | Strong | Strong |
| Production maturity | Released Apr 2025 | Released Sep 2024 | Released Dec 2024 |
| Migration effort from current Groq setup | New provider class | New provider class | New provider class (smallest behavioural delta) |
| Best-fit role | **Recommended primary** | Conservative alternative | Lowest-risk migration if Indic scope narrows |

---

## 6. Recommendation

**Primary recommendation: Option A — Qwen3-32B-Instruct.**

This recommendation is based on the following factors, in order of weight:

1. **License clarity.** Apache 2.0 is the only license among the shortlisted options that requires no commercial review at the client's deployment scale.
2. **Hardware economics.** A single A100 80 GB per replica versus two for the alternatives is a material cost difference at 100,000-client scale.
3. **Language coverage.** 119-language training is the strongest match for the multi-Indic scope of the project.
4. **No capability compromise.** Qwen3-32B benchmarks at or above the larger Qwen2.5-72B, so the smaller footprint does not cost quality.
5. **Structured-output reliability.** Native structured-output training combined with grammar-constrained decoding at the serving layer satisfies the strict JSON requirement of the pipeline.

**Conservative alternative: Option B — Qwen2.5-72B-Instruct,** if the client prefers a longer production track record and accepts the higher hardware cost and license-review step.

**Validation step before final commit:** Whichever option is selected, we recommend a structured A/B evaluation against the currently used `llama-3.3-70b-versatile` (Groq-hosted) model on a frozen representative sample of the client's actual documents. Specifically:

- 50 real documents through the structured extractor — measure JSON parse success rate and `{field}_source` exact-quote match rate.
- 50 representative advisory queries through the advisory generator — measure `parse_status=ok` rate and reviewer-rated answer quality.

The model with the better measured performance on these two evaluations becomes the production choice. This makes the final decision data-driven and defensible to internal and external review.

---

## 7. Serving stack — non-negotiable for any option

The choice of model is only one part of the production reliability story. Whichever model is selected, the serving stack must include the following components. These are not optional for a production deployment at 100,000-client scale.

| Component | Purpose | Notes |
|-----------|---------|-------|
| **vLLM** (≥ 0.6.x) or **SGLang** | High-throughput inference server with continuous batching and paged attention. | Provides 5–10× throughput over naive serving. Required for queue-backed batched workloads. |
| **xgrammar** (or outlines / lm-format-enforcer) | Grammar-constrained JSON decoding. | Mathematically guarantees JSON validity by zeroing out invalid token probabilities at sampling time. This is the load-bearing component for requirement F1. |
| **Pydantic → JSON Schema → grammar pipeline** | Auto-generates xgrammar grammars from existing Pydantic models in the codebase. | Reuses the schema definitions already in `app/advisory/generator.py` and the extractor pipeline. |
| **Speculative decoding** (target model + small draft model) | 2–3× throughput improvement at production scale at no quality cost. | Optional in initial rollout, recommended once baseline is stable. |
| **Prefix caching** | Caches the long system prompts shared across all calls. | Reduces time-to-first-token meaningfully on the extractor and advisory paths. |

---

## 8. Hardware sizing guidance

The following are indicative per-replica hardware footprints. Replica count for the queued backend should be sized by the client's DevOps team based on observed queue depth, target p95 latency, and daily-active client fraction.

| Option | Quantization | GPU per replica | Approximate VRAM | Notes |
|--------|--------------|-----------------|------------------|-------|
| A: Qwen3-32B | BF16 | 1 × A100 80 GB | ~65 GB | Recommended baseline |
| A: Qwen3-32B | AWQ INT4 | 1 × A100 40 GB or 1 × L40S | ~18 GB | Permits 2+ replicas per A100 80 GB |
| B: Qwen2.5-72B | BF16 | 2 × A100 80 GB | ~145 GB | Tensor parallelism across two GPUs |
| B: Qwen2.5-72B | FP8 | 1 × H100 80 GB | ~75 GB | If H100 is available |
| C: Llama-3.3-70B | BF16 | 2 × A100 80 GB | ~140 GB | Same footprint as Option B |
| C: Llama-3.3-70B | FP8 | 1 × H100 80 GB | ~70 GB | If H100 is available |

**Procurement consideration for India deployment:** H100 lead times have been long (8–16 weeks); A100 80 GB is more available. This is a practical reason to prefer Option A's single-A100 footprint.

---

## 9. Migration plan from the current setup

Regardless of which option is selected, the engineering work to migrate is bounded and well-defined.

1. Implement a new provider class (e.g. `QwenVLLMProvider` or `LlamaVLLMProvider`) in `app/llm/`, conforming to the existing `LLMProvider` interface.
2. Implement grammar-constrained decoding by converting the existing Pydantic schemas to xgrammar grammars and passing them with each `complete_json` call.
3. Change the singleton wiring in `app/llm/groq_provider.py` (or rename the file) to instantiate the new provider.
4. Run the A/B evaluation described in Section 6 against the current Groq-hosted model.
5. Promote to production once evaluation results are satisfactory.

No changes are required to any pipeline block (`classifier.py`, `extractor.py`, `evidence_checker.py`, `validator.py`, `metadata.py`, `text_gen.py`) or to the advisory layer. The provider abstraction was designed for exactly this swap.

---

## 10. Decision request

We request that the client confirm one of the following:

- [ ] Proceed with **Option A (Qwen3-32B-Instruct)** as the primary model.
- [ ] Proceed with **Option B (Qwen2.5-72B-Instruct)** as the primary model, accepting the higher hardware cost and the license-review step.
- [ ] Proceed with **Option C (Llama-3.3-70B-Instruct)** — only recommended if the deployment scope is being narrowed to English and Hindi only.
- [ ] Request additional information or alternative options before deciding.

Once a selection is made, we will proceed with the A/B evaluation described in Section 6 and the migration steps in Section 9.

---

*Prepared by the AGRI-RAG engineering team.*
