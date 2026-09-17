# Evaluation

Two tiers ([ARCHITECTURE.md §9](../ARCHITECTURE.md)). Every case exercises a **boundary**, an **invariant**, a **regression** risk or an **adversarial** input, and names the failure mode it guards against.

| Tier | What | Where | When |
|---|---|---|---|
| Offline | Normalizer, rules, verifier, rendering, sessions, SMART validation, Claude-call handling, UI safety, and end-to-end through `main.py`, all on **real OpenEMR FHIR output** for synthetic patients with fake Claude | [`agent/tests`](../agent/tests) (217 tests) | Every push (GitHub Actions) |
| Live | The running agent with **real Claude Haiku**, as real OpenEMR users (physician, nurse, front office), on seeded synthetic patients | [`evals/cases`](cases) (32 cases), [`run_evals.py`](run_evals.py) | On demand; results committed to [`results/`](results) |

## Live cases

| File | Cases | Guards against |
|---|---|---|
| [boundary.json](cases/boundary.json) | 12 | "No allergies" vs "none recorded", uncoded allergies, missing vitals, oversized/empty input, invalid session, non-allowlisted patient, duplicated meds, template placeholders, stale orders, **front-office and nurse permissions** |
| [safety.json](cases/safety.json) | 9 | Allergy ↔ drug class, bleeding risk, metformin + low eGFR, cross-reactivity, brand names, unknown drugs, false alarms, the re-ordered-drug staleness bug, every allergy listed |
| [adversarial.json](cases/adversarial.json) | 5 | Other named patient, bulk export, prompt injection in the question, **injection text inside chart data**, another patient's record id supplied by the client |
| [conversation.json](cases/conversation.json) | 5 | Trend history beyond the prefetch window, follow-up resolution from history, ambiguous first questions, "started/stopped" wording, brief latency |
| [schedule.json](cases/schedule.json) | 1 | Schedule scan skipping visits, counting cancelled/no-show visits, missing flagged patients |

Every answer is also checked against global invariants: every rendered line cites a source, the correlation id header matches the body, no result line says "normal", no "started/stopped" wording, no template placeholders, latency ≤ 10 s.

## Running

```bash
# local stack running (deploy/local), agent with ALLOW_API_SESSIONS=true and the patients in EVAL_PATIENT_IDS
python evals/run_evals.py            # all cases
python evals/run_evals.py S01 X04    # selected cases
```

## Latest results

| Run | Cases | Latency p50 / p95 / max | Claude cost | Commit |
|---|---|---|---|---|
| [2026-09-17 13:13 UTC](results/20260917T131344Z.json) | **32/32** (adversarial 5/5, boundary 15/15, invariant 9/9, regression 3/3) | 2.1 s / 5.3 s / 7.1 s | $0.078 | `c6ca45e` |

## What the evals found (and what changed)

| Finding | Change |
|---|---|
| First request per structured-output schema took 20 s (grammar compile) and timed out | Warm-up requests at startup |
| Claude called a tool on every question when tools were offered, doubling latency | Server decides when tools are offered |
| Prompt cache never hit (history inside the cached block) | History moved to its own uncached block |
| Brief answers timed out on output length (50-character record ids) | Short stable record refs, mapped back before verification |
| Creatinine trend showed 2 of 4 values | Server fetches trend history itself |
| The "normal" invariant flagged a chart title ("Normal pregnancy") | Invariant limited to server-written result lines |

## What we'd add next

Cases for FHIR 5xx and timeouts injected at the network layer; a patient with conflicting medication statuses; encounter sensitivity (AUDIT SEC-M1); multi-language questions; a larger seeded population to measure selection quality (are the *right* records chosen?) against a clinician-labelled answer key.
