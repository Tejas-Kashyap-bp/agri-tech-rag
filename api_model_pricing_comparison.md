# API Model Pricing Comparison — Farm Advisory Pipeline

**Scope:** Hosted LLM APIs (Gemini, Grok, OpenAI, Anthropic) called from ECS workers.
Self-hosted GPU options are intentionally excluded here.

---

## 1. Architectural Context

The advisory pipeline runs the LLM **inside each ECS worker** as a synchronous HTTP call.
Workers pull farm jobs from SQS, build the prompt, call the model API, and persist the
advisory. This means:

- Every farm = **1 API call** (or a small chain — count once per call).
- Workers block on the API; throughput is gated by **provider rate limits + per-call latency**, not GPU capacity.
- Cost is purely **tokens × price** — no idle/boot/server overhead.
- Scaling is "free" — adding ECS workers just adds parallel API calls (until provider RPM/TPM caps hit).

### Token volume per farm

| Architecture | Input tokens | Output tokens | Total / farm |
|---|---|---|---|
| **Arch A** (lean prompt) | ~1,500 | ~500 | **~2,000** |
| **Arch B** (RAG + 15K context) | ~15,000 | ~5,000 | **~20,000** |

### Monthly token volume (6 batches/month, satellite cycle)

| Scale | Arch | Input tokens/mo | Output tokens/mo |
|---|---|---|---|
| 100K farms | A | 900 M | 300 M |
| 100K farms | B | 9.0 B | 3.0 B |
| 400K farms | A | 3.6 B | 1.2 B |
| 400K farms | B | 36 B | 12 B |

> Nightly new-farmer DAG adds <1% volume; ignored in totals.

---

## 2. Public List Prices (USD per 1M tokens)

| Tier | Model | Input | Output | Notes |
|---|---|---|---|---|
| Budget | **Gemini 2.0 Flash** | 0.10 | 0.40 | Cheapest viable option |
| Budget | **Grok 3 mini** | 0.30 | 0.50 | xAI's cheap tier |
| Budget | **Gemini 2.5 Flash** | 0.30 | 2.50 | Better reasoning than 2.0 |
| Mid | **Claude Haiku 4.5** | 1.00 | 5.00 | Fast, strong instruction following |
| Mid | **Gemini 2.5 Pro** | 1.25 | 10.00 | ≤200K context tier |
| Premium | **GPT-4o** | 2.50 | 10.00 | OpenAI flagship general |
| Premium | **Claude Sonnet 4.6** | 3.00 | 15.00 | Strong on structured output |
| Premium | **Grok 4** | 3.00 | 15.00 | xAI flagship |

> Prices are list/on-demand. **Batch APIs and prompt caching cut these by 50–90%** — see §5.

---

## 3. Architecture A — Monthly API Cost (2K tokens/farm)

| Model | 100K farms | 400K farms |
|---|---|---|
| Gemini 2.0 Flash | **$210** | **$840** |
| Grok 3 mini | $420 | $1,680 |
| Gemini 2.5 Flash | $1,020 | $4,080 |
| Claude Haiku 4.5 | $2,400 | $9,600 |
| Gemini 2.5 Pro | $4,125 | $16,500 |
| GPT-4o | $5,250 | $21,000 |
| Claude Sonnet 4.6 | $7,200 | $28,800 |
| Grok 4 | $7,200 | $28,800 |

**Verdict for Arch A:** Gemini 2.0 Flash at **$840/mo for 400K farms** is the floor.
Even premium models stay under $30K/mo — affordable across the board.

---

## 4. Architecture B — Monthly API Cost (20K tokens/farm)

| Model | 100K farms | 400K farms |
|---|---|---|
| Gemini 2.0 Flash | **$2,100** | **$8,400** |
| Grok 3 mini | $4,200 | $16,800 |
| Gemini 2.5 Flash | $10,200 | $40,800 |
| Claude Haiku 4.5 | $24,000 | $96,000 |
| Gemini 2.5 Pro | $41,250 | $165,000 |
| GPT-4o | $52,500 | $210,000 |
| Claude Sonnet 4.6 | $72,000 | $288,000 |
| Grok 4 | $72,000 | $288,000 |

**Verdict for Arch B:** Premium models are economically unviable at 400K
($165K–$288K/mo). Only Gemini 2.0 Flash and Grok 3 mini stay in reasonable
range. **If client insists on Arch B with a premium model, present these numbers
before approval.**

---

## 5. Cost Reductions Available

These are **stackable** with the prices above:

### 5.1 Batch APIs (~50% off)
- **Gemini Batch API**: 50% off on async jobs with up to 24h SLA.
- **Anthropic Message Batches**: 50% off, 24h SLA.
- **OpenAI Batch API**: 50% off, 24h SLA.
- **Grok**: no public batch discount yet.

The 5-day satellite cycle has plenty of slack for a 24h batch window — **use batch APIs for the main DAG**. Use real-time only for the nightly new-farmer DAG.

### 5.2 Prompt / Context Caching (Arch B only)
RAG context that repeats across farms (crop knowledge base, regional rules,
few-shot examples) can be cached:

- **Anthropic prompt caching**: 90% off on cache hits, 25% premium on writes.
- **Gemini context caching**: ~75% off cached tokens.
- **OpenAI prompt caching**: 50% off automatic cache hits.

For Arch B, if 10K of the 15K input is reusable context → **input cost drops ~60%**.

### 5.3 Effective price after batch + cache

| Model | Arch B 400K, list | + batch (50%) | + caching | Effective |
|---|---|---|---|---|
| Gemini 2.5 Pro | $165,000 | $82,500 | ~$35,000 | **~$35K/mo** |
| Claude Sonnet 4.6 | $288,000 | $144,000 | ~$60,000 | **~$60K/mo** |
| Gemini 2.0 Flash | $8,400 | $4,200 | ~$2,500 | **~$2.5K/mo** |

---

## 6. Operational Considerations from ECS Workers

| Concern | Implication |
|---|---|
| **Rate limits** | Gemini, OpenAI, Anthropic gate by RPM and TPM. At 400K farms in 24h Arch B = ~5 RPS — manageable on Tier 3+ accounts. Arch A is trivially below limits. |
| **Latency per call** | Arch A: ~1–3s. Arch B: ~25–35s (decode of 5K output dominates). Set ECS task `visibility_timeout` ≥ 3× expected latency. |
| **Retries** | All providers occasionally 429/503. SQS retry handles this — keep the message un-deleted on exception. |
| **Egress** | Provider APIs are external; ensure VPC has NAT or VPC endpoint. Egress cost is negligible (~$0.09/GB) but not zero — Arch B 400K = ~720 GB out = ~$65/mo. |
| **Data residency** | If client data must stay in India: only Gemini (asia-south1) and Azure OpenAI (Central India) offer regional endpoints. Grok and Anthropic are US-only. |
| **Determinism** | All providers support `temperature=0` for reproducible advisories. |
| **Failure isolation** | Single-provider lock-in is risky. Recommend abstracting via a thin client (`llm_client.py`) so model can be swapped per-DAG without worker changes. |

---

## 7. Recommendation

| Question | Answer |
|---|---|
| If Arch A is approved | **Gemini 2.0 Flash via Batch API** — $400–$1,000/mo end-to-end at 400K farms. Cheapest by far, lowest ops burden. |
| If Arch B is approved | **Gemini 2.5 Flash + Batch + Context Caching** — ~$5K/mo at 100K, ~$20K/mo at 400K. Premium models not worth it unless quality testing shows Flash fails. |
| If client demands premium quality | **Claude Sonnet 4.6 with prompt caching** at Arch A only. Arch B with Sonnet is $60K+/mo even after discounts. |
| Always | Build a provider-agnostic LLM client. Pilot with 2 providers in parallel during the first batch cycle and compare advisory quality before locking in. |

---

## 8. Quick Reference — Cost per 1,000 Farms

| Model | Arch A (2K) | Arch B (20K) |
|---|---|---|
| Gemini 2.0 Flash | $0.35 | $3.50 |
| Grok 3 mini | $0.70 | $7.00 |
| Gemini 2.5 Flash | $1.70 | $17.00 |
| Claude Haiku 4.5 | $4.00 | $40.00 |
| Gemini 2.5 Pro | $6.88 | $68.75 |
| GPT-4o | $8.75 | $87.50 |
| Claude Sonnet 4.6 | $12.00 | $120.00 |
| Grok 4 | $12.00 | $120.00 |

> Multiply by total farm-runs/month (farms × 6 batches) to project cost at any scale.
