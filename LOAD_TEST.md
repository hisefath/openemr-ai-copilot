# LOAD_TEST.md — Load tests and performance baselines

**Status: scripted, not yet run.** Runs spend Anthropic credits and load the shared demo OpenEMR, so they run only when explicitly scheduled. Results tables below are filled in when they do.

## What we're testing

The assignment requires at least 10 and 50 concurrent users against the deployed agent, with p50/p95/p99 latency, error rate, and CPU, memory, latency and throughput baselines. The question behind it, from [USERS.md](USERS.md): **does a physician still get a verified answer in ≤ 10 s (p95) when colleagues are using the Co-Pilot at the same time?**

AUDIT.md predicts the answer depends on OpenEMR, not the agent: every FHIR call pays a heavy bootstrap and synchronous audit writes (PERF-1, PERF-2), MedicationRequest is expensive (PERF-3), and OpenEMR runs in a ~1 GB container (OPS-3). The test is designed to confirm or refute that.

## Scenarios

Defined in [`loadtest/locustfile.py`](loadtest/locustfile.py).

| Scenario | Share | Steps | Think time |
|---|---|---|---|
| Between rooms | 90 % | open session on a synthetic patient → brief → 1–2 follow-ups (allergies/interactions, what changed, trend, "safe to start amoxicillin?") | 20–60 s (walking to the next room) |
| Morning scan | 10 % | schedule scan over the physician's appointments | 2–5 min |

| Run | Users | Spawn rate | Duration |
|---|---|---|---|
| L10 | 10 | 2/s | 10 min |
| L50 | 50 | 5/s | 10 min |

## Setup

1. **Tokens:** one OAuth access token per virtual user, from synthetic demo users (see README, "Demo users"). Tokens last 1 hour, so each run stays under 50 minutes. Save as a JSON file outside the repo and point `COPILOT_TOKENS_FILE` at it.
2. **Agent:** `ALLOW_API_SESSIONS=true` and `EVAL_PATIENT_IDS` listing the synthetic patients used.
3. **Budget check:** estimate the run's LLM cost first: questions ≈ users × (duration ÷ average think time) × 2.5; cost ≈ questions × measured cost per answer (from Langfuse). Confirm it fits the remaining prepaid Anthropic balance.
4. **Run:** `locust -f loadtest/locustfile.py --host https://<agent> --users 10 --spawn-rate 2 --run-time 10m --headless --csv loadtest/results/l10`

## What gets recorded

- **From Locust:** p50/p95/p99 latency, requests/s, failures per endpoint. Fallback answers count as failures (same definition as ALERTS.md).
- **From Langfuse** (same time window): FHIR time vs Claude time per request, retries, queue depth on the OpenEMR semaphore, verification outcomes, tokens and cost.
- **From Railway metrics** for `openemr`, `MySQL` and `agent`: CPU and memory at idle (5 min before), during L10, during L50.

## Results

### Baselines (idle)

| Service | CPU | Memory |
|---|---|---|
| openemr | | |
| MySQL | | |
| agent | | |

### L10 — 10 concurrent users

| Endpoint | p50 | p95 | p99 | Error rate | Throughput |
|---|---|---|---|---|---|
| brief | | | | | |
| follow_up | | | | | |
| schedule_scan | | | | | |

| Service | CPU peak | Memory peak |
|---|---|---|
| openemr | | |
| MySQL | | |
| agent | | |

FHIR vs Claude time split: · LLM cost of the run: · Queue depth p95:

### L50 — 50 concurrent users

| Endpoint | p50 | p95 | p99 | Error rate | Throughput |
|---|---|---|---|---|---|
| brief | | | | | |
| follow_up | | | | | |
| schedule_scan | | | | | |

| Service | CPU peak | Memory peak |
|---|---|---|
| openemr | | |
| MySQL | | |
| agent | | |

FHIR vs Claude time split: · LLM cost of the run: · Queue depth p95:

## Interpretation

_To be written after the runs: where the bottleneck was, whether the 10 s p95 held, and what would change first (ARCHITECTURE §11)._
