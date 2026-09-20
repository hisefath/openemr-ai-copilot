# Evaluation

Two tiers ([ARCHITECTURE.md §9](../ARCHITECTURE.md)). Every case exercises a **boundary**, an **invariant**, a **regression** risk or an **adversarial** input, and names the failure mode it guards against.

| Tier | What | Where | When |
|---|---|---|---|
| Offline | Normalizer, rules, verifier, rendering, sessions, SMART validation, Claude-call handling, UI safety, and end-to-end through `main.py`, all on **real OpenEMR FHIR output** for synthetic patients with fake Claude | [`agent/tests`](../agent/tests) (185 test functions, 226 cases) | Every push (GitHub Actions) |
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

From the repository root, with the local stack up ([README](../README.md#run-it-locally)) and the agent started
with `ALLOW_API_SESSIONS=true` and the synthetic patients listed in `EVAL_PATIENT_IDS`:

```bash
python evals/run_evals.py            # all cases
python evals/run_evals.py S01 X04    # selected cases
```

**Two files it needs are not in this repository.** `run_evals.py` mints a token per role user, so it reads the
local SMART client id and the edge-case patient UUIDs your own setup produced — values that differ on every
machine and that include a client secret, which is why they are not committed:

| File | Holds | Produced by |
|---|---|---|
| `local-smart-client.json` | `client_id` of your local SMART registration | the registration step in the [README](../README.md#run-it-locally) |
| `local-edge-patients.json` | `{"patients": {"E1": "<uuid>", …}}` for the edge-case patients | `deploy/local/seed_edge_cases.php`, which prints them |

Both are looked for in `../tools/` next to the repository. Point somewhere else with `EVAL_TOOLS_DIR=/path`
(`run_evals.py`) or `WORKBENCH=/path` (`loadtest/run_local.sh`, which reads the same two files). Without them the
run stops immediately with a missing-file error rather than part way through.

Each run costs about $0.07 of Anthropic credit and writes `results/<timestamp>.json`.

## Latest results

| Run | Cases | Latency p50 / p95 / max | Claude cost | Commit |
|---|---|---|---|---|
| [2026-09-20 13:31 UTC](results/20260920T133102Z.json) | **32/32** (adversarial 5/5, boundary 15/15, invariant 9/9, regression 3/3) | 1.4 s / 2.6 s / 3.1 s | $0.071 | `20f1c23` |
| [2026-09-17 13:13 UTC](results/20260917T131344Z.json) | 32/32 | 2.1 s / 5.3 s / 7.1 s | $0.078 | `c6ca45e` |

The 2026-09-20 run is the one [KEY_METRICS.md](../KEY_METRICS.md) reports against, taken after the agent was
repackaged. `results/` also holds two runs from that morning kept on purpose: `20260920T132925Z.json` is the
31/32 that caught the schedule scan finding nothing, because `seed_demo.php` dates appointments to the day it
runs and the local demo was three days stale, and `20260920T133055Z.json` is the single-case re-run of U01
after re-seeding. A failed run that found a real trap is worth keeping.

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
