# W2_COST_AND_LATENCY.md — what Week 2 costs and where the time goes

Prices: Claude Haiku 4.5 at **$1 / MTok input, $5 / MTok output**, cache read $0.10, cache write $1.25 (Anthropic
list, 2026-09). Voyage `voyage-3-lite` and `rerank-2-lite` on the free tier.

Week 1's measured figures are in [AI_COST_ANALYSIS.md](AI_COST_ANALYSIS.md) and stand unchanged. This document
covers what Week 2 adds, and is explicit about which numbers are **measured** and which are **derived** — a
derived number presented as measured is worse than no number.

---

## 1. Development spend

| Item | Spend | Basis |
|---|---|---|
| Week 1 total | **$3.35** | Measured, per-call logs |
| Week 2 — Voyage embeddings and reranks | **$0.00** | 15 corpus chunks + 8 queries + 8 reranks, inside the 200M-token free allowance |
| Week 2 — Anthropic | **~$0.00** | Every test and the entire eval gate use a fake at `app.state.llm`; no vision call has been billed |
| **Running total against the $30 cap** | **~$3.35** | |

That last row is the interesting one, and it is a consequence of the design rather than thrift. The eval gate
runs against **recorded** model responses at the seam `agent/tests/` already had, so the 383-test suite and every
gate run cost nothing. The bill starts when real scans are recorded for the 50-case set.

**What re-recording will cost.** Deliberate and rare, priced below at ~$0.03 per document — so recording fifty
cases is **about $1.50**, once.

---

## 2. Cost per document

Derived from Anthropic's image-token formula (`width × height ÷ 750`) at the render scale this agent uses.

| Quantity | Value | Basis |
|---|---|---|
| Render scale | 2.0 (≈144 dpi) | `locate.RENDER_SCALE` — enough for OCR and for a model to read a mediocre scan |
| Rendered page | 1224 × 1584 px | US Letter at that scale |
| **Image tokens per page** | **~2,585** | 1224 × 1584 ÷ 750 |
| Prompt + schema | ~600 | Instruction and the constrained output schema |
| Output | ~400–1,200 | A dense lab panel produces far more than an intake form |

| Document | Input tokens | Cost |
|---|---|---|
| 1-page intake form | ~3,200 | **~$0.005** |
| 3-page lab report | ~8,400 | **~$0.013** |
| 10-page maximum | ~26,500 | **~$0.033** |

So a document costs roughly **half a cent to three cents**, dominated entirely by image tokens. Retrieval adds
nothing measurable: embeddings and reranks are within Voyage's free allowance, and the corpus vectors are
computed once and committed.

**The page cap is a cost control, not just a latency one.** Without `MAX_PAGES = 10`, a 200-page upload is a
$0.66 single request that anyone with front-desk access can trigger. With it, the worst case is bounded and the
answer says the document was truncated.

### Projected production cost

Assuming the Week 1 user — a PCP with a 20-patient day — and one document for a third of them:

| Scale | Documents/month | Cost/month |
|---|---|---|
| 1 physician | ~140 | **~$1.40** |
| 10 physicians | ~1,400 | **~$14** |
| 100 physicians | ~14,000 | **~$140** |

Documents are cheap. The cost driver at scale is not ingestion — it is the per-question spend Week 1 already
measured, plus Voyage leaving the free tier (below).

---

## 3. Latency

### Measured

| Path | p50 | p95 | Source |
|---|---|---|---|
| Week 1 question → verified answer | **1.4 s** | **2.6 s** | 28 timed answers, unchanged |
| Week 1 under 50 concurrent users | **2.0 s** | **3.0 s** (p99 3.7 s) | 374 requests, LOAD_TEST.md |
| Full offline suite (383 tests) | — | **5.6 s** | Every run |
| Eval gate, 3 cases | — | **<1 s** | Recorded replay; no network |

### Not measured, and I am not going to pretend otherwise

**Ingestion end-to-end has no real number.** The flow test completes in under a second, but its vision call is a
fake — that figure says nothing about the real one. `KEY_METRICS.md` #12 is marked *Pending* for the same reason.

What can be said is the **shape** of the budget, and why ingestion is not on the question path at all:

| Step | Expected | Note |
|---|---|---|
| Upload to OpenEMR + list back | 0.2–0.6 s | Two round trips; the write returns no id |
| Render + word coordinates | 0.3–1.5 s | Per page; OCR is far slower than a text layer |
| **Vision call** | **3–15 s** | Scales with page count. **The bottleneck.** |
| Locate all values | <50 ms | Pure string work over already-extracted words |
| Stage | <10 ms | In-memory; a database write when the queue is persisted |

**Ingestion runs on a 90-second deadline, not the 9-second question budget.** `config.py:32` sets that budget for
questions, and a vision call over a multi-page scan cannot fit in it. Conflating the two would have meant either
a question path that can block for a minute or an ingest path that times out on every real document.

---

## 4. Bottleneck analysis

**The vision call, and it is not close.** It is 80–95 % of ingestion wall-clock and ~95 % of its cost. Three
consequences shaped the design:

1. **Page cap.** Vision is billed and timed per page, so the cap is both controls at once.
2. **Text layer preferred over OCR.** A digital PDF's own text layer is both cheaper and *more accurate* than
   OCR, so OCR is the fallback rather than the default.
3. **One call per document, not per field.** Pages are rendered and word boxes collected once and handed on,
   rather than re-derived per value.

**The second bottleneck is Voyage's rate limit, not its speed.** The free tier without a payment method allows
**3 requests/minute** — which is why recording the eight eval queries took several minutes of deliberate pacing.
Corpus vectors are committed so start-up and CI are unaffected, but live reranking on the deployed app would
throttle under any real load. The failure is safe (a reranker outage returns *no* evidence rather than unranked
chunks) but it is a real ceiling, and the fix is a payment method rather than a code change.

**What is not a bottleneck:** locating (<50 ms of pure string work), BM25 over 15 chunks, and brute-force cosine
over 15 × 512 floats. The retrieval design deliberately spends nothing where nothing needs spending.

---

## 5. Where these numbers can mislead

- Everything in §3's "expected" column is a range, not a measurement. Treat it as a budget to check against, not
  a result.
- The image-token figures assume US Letter at scale 2.0. A3 scans or a higher render scale move them
  proportionally.
- Week 1's load-test latency is from the local stack, which has no Railway network hop. The deployed p95 is
  higher; LOAD_TEST.md says by how much and why.
- The $0.00 Week 2 spend is honest but temporary. It reflects a gate built on recorded responses, and it will
  become ~$1.50 the first time the 50-case set is recorded against real scans.
