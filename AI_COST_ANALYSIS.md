# AI_COST_ANALYSIS.md — What the Clinical Co-Pilot costs to build and run

All LLM numbers below are **measured** from the agent's own per-call logs (tokens, cache usage, cost) against real Claude Haiku 4.5 on synthetic patients, not estimated from token prices. Prices: Claude Haiku 4.5 at $1 / MTok input, $5 / MTok output, $1.25 / MTok cache write, $0.10 / MTok cache read (Anthropic list prices, 2026-09).

## 1. Actual development spend

| Item | Spend | Source |
|---|---|---|
| Anthropic API — live verification, 4 full eval runs, Bruno runs, alert fault injection, debugging, warm-ups | **≈ $0.75** | Agent logs (`llm_cost_usd` per answer, warm-up lines) |
| Anthropic API — load tests (L10 ≈ $0.43, L50 $2.11, smoke runs) | **≈ $2.60** | Agent logs over each run's window |
| **Anthropic API total** | **≈ $3.35** | Confirm against the Anthropic console, Usage |
| Coding assistant (Claude Code) | Covered by a Claude Max subscription | Not API-billed |
| Langfuse | $0 | Free tier |
| Railway (OpenEMR, MySQL, agent, alerts cron) | ≈ $5–10 | Hobby plan, usage-based |

The API spend is small because development ran **local-first**: OpenEMR, MySQL and the agent run on a laptop, and every expensive loop (fixtures, rules, verification, 225 offline test cases) uses recorded OpenEMR output and a fake Claude. Real Claude is used only for live verification, evals and load tests — and the load tests are 78 % of the bill, which is the honest shape of it: **measuring the thing cost four times what building it did.**

## 2. Measured cost per question

From 63 live answers (65 Claude calls) on seeded synthetic patients:

| | Mean | p50 | p95 |
|---|---|---|---|
| Cost per answer | $0.0031 | $0.0014 | $0.0101 |
| Cached context read (tokens) | 5,235 | 6,198 | 11,910 |
| Uncached input (tokens) | 159 | 19 | 1,025 |
| Output (tokens) | 87 | 65 | 218 |
| Claude calls per answer | 1 in 61 of 63 answers | | |
| Latency | | 1.97 s | 5.18 s |

| Situation | Cost |
|---|---|
| **First question about a patient** (writes the ~6–12K-token chart context to the prompt cache) | **$0.0095** |
| **Follow-up in the same visit** (reads the cache) | **$0.0012** |
| Schedule scan (UC5) | **$0** (deterministic: no LLM call) |
| Startup warm-up (per deploy) | $0.005 |

Three design choices, each made after measuring live, set these numbers:
1. **Claude selects records; the server writes the text.** Output is ~65 tokens of record references, not paragraphs.
2. **Short record references** (`M3`) instead of FHIR UUIDs cut output tokens ~3× and brief latency from ~6 s to ~2 s.
3. **Prompt caching of the per-patient chart block** makes follow-ups ~8× cheaper than the first question (and faster).

## 3. Usage model

A "user" is a clinician (the primary care physician in [USERS.md](USERS.md)).

| Assumption | Value | Why |
|---|---|---|
| Visits per clinician per clinic day | 20 | USERS.md target user |
| Clinic days per month | 21 | |
| Visits where the Co-Pilot is opened | 60 % | Not every visit needs it (new problems, quick rechecks) |
| Questions per opened visit | 1.8 | First question + 0.8 follow-ups (evals and live runs averaged 1.5–2 per patient) |
| Morning schedule scans | 1 per clinician per day | $0 LLM |

**LLM cost per clinician per month** = 420 visits × 60 % × ($0.0095 + 0.8 × $0.0012) = **≈ $2.64**.

## 4. Cost at scale

Scaling is not tokens × users. Past a few hundred clinicians the LLM becomes the smaller part of the bill: OpenEMR capacity, compliance, observability that can legally hold PHI-adjacent data, and operations dominate.

| | **100 users** | **1,000 users** | **10,000 users** | **100,000 users** |
|---|---|---|---|---|
| Questions / month | 45 K | 454 K | 4.5 M | 45 M |
| Peak questions / s (8–9 AM) | ~0.5 | ~5 | ~50 | ~500 |
| **LLM** (list price) | **$264** | **$2.6 K** | **$26 K** | **$264 K** |
| LLM after the tier's optimizations | $264 | $2.4 K | ~$18 K | ~$150 K |
| OpenEMR + database | $150 | $2 K | $25 K | $200 K |
| Agent compute, sessions | $30 | $400 | $4 K | $35 K |
| Observability (Langfuse, logs) | $50 (self-host) | $400 | $3 K | $20 K |
| Audit storage (6-year retention) | ~$5 | $50 | $500 | $5 K |
| Compliance & security tooling | — | $1 K | $8 K | $40 K |
| **Infra + LLM / month** | **≈ $500** | **≈ $6 K** | **≈ $60 K** | **≈ $450 K** |
| **Per user / month** | **≈ $5.00** | **≈ $6.00** | **≈ $6.00** | **≈ $4.50** |

People (on-call SRE, security, clinical safety review) are excluded from the table but become the largest line item from ~10K users.

### What changes at each tier

**100 users — one clinic group (today's architecture, hardened).**
- Single agent replica, in-memory sessions (ARCHITECTURE §10), one OpenEMR container sized up from the current 1 GB (AUDIT OPS-3: install took 480 s at 1 GB vs 32 s locally).
- Langfuse self-hosted or on a plan with a BAA, since traces carry pseudonymous ids (AUDIT COMP-3).
- Anthropic usage tier with enough rate limit for ~1 question/s bursts.
- Bottleneck to watch: OpenEMR's per-request cost (PERF-1, PERF-2), not Claude.

**1,000 users — a health system's primary care network.**
- **Sessions move to Redis**; 3+ agent replicas behind the load balancer.
- **OpenEMR scales horizontally** (multiple PHP containers, managed MySQL with a read replica for FHIR reads); `api_log_option=1` and reduced SELECT auditing with compliance sign-off (PERF-2).
- **Overnight pre-warm**: the schedule scan's patient list drives prefetch before clinic opens, so the first question hits a warm OpenEMR.
- Anthropic: higher rate-limit tier; the cache TTL raised to 1 hour for patients on today's schedule (fewer cache writes: ~8 % LLM saving).

**10,000 users — multiple health systems.**
- **Multi-tenant deployment** on a cloud with a BAA (AWS/GCP), Kubernetes with autoscaling, per-tenant OpenEMR and database isolation.
- Claude through **Amazon Bedrock or Google Vertex AI** for the cloud BAA, data residency and **provisioned throughput** at predictable latency during the 8–9 AM peak.
- **Batch API (50 % off) for pre-visit briefs** generated overnight for scheduled patients; the interactive path handles follow-ups and walk-ins (~30 % LLM saving).
- Dedicated append-only audit store (the MySQL table becomes a write-once log pipeline) and SIEM integration.
- Observability self-hosted at scale; alerting through PagerDuty with the ALERTS.md runbooks.

**100,000 users — national scale.**
- **Enterprise LLM agreement** (committed-use discount) plus batch briefs and pre-computed context summaries per patient refreshed on OpenEMR change events. This requires fixing OpenEMR's incomplete change events (AUDIT ARCH-10) or CDC from the database.
- Multi-region active-active, per-region data residency.
- Evaluate a fine-tuned or distilled selection model for the high-volume brief intent, keeping Claude for safety checks and ambiguous questions. Only if evals show equal selection quality.
- A 24/7 SRE and clinical-safety operations team; people cost exceeds infrastructure.

## 5. Sensitivity

| Change | LLM cost per clinician / month | Why it matters |
|---|---|---|
| Baseline | $2.64 | |
| Every visit opens the Co-Pilot, 3 questions each | $5.00 | Heavier adoption roughly doubles it; still under the infra cost |
| No prompt caching | $3.30 | Every question ~$0.0073 (no write premium, no cheap follow-ups): caching saves ~20 % at 1.8 questions per visit, more in longer conversations, and makes follow-ups faster |
| Claude Sonnet 5 instead of Haiku 4.5 | ~$5.30 | 2× token prices; only if evals show better selection |
| Model writes paragraphs instead of selecting records | ~$3.80 | ~500 output tokens per answer: the dollar cost is small; the real costs are 3–6 s more latency and losing deterministic verification |
| Full FHIR UUIDs instead of short refs | ~$3.70 | More input and output tokens; the real cost was latency (briefs timed out at the 9 s deadline) |

## 6. Cost controls in place

- **Hard spend ceiling:** prepaid Anthropic credits with auto-reload off.
- **Per-question deadline** (9 s), `max_tokens=1500`, at most one tool round, `max_retries=0`: no runaway loops.
- **Deterministic paths cost nothing:** safety rules, the schedule scan, trends and the non-AI fallback use no LLM.
- **Every answer logs its cost** (`llm_cost_usd`) with the correlation id; Langfuse aggregates cost per answer on the dashboard.
- **Load tests are budgeted before running** ([LOAD_TEST.md](LOAD_TEST.md)): expected cost = questions × measured cost per answer.
