# LOAD_TEST.md — Load tests and performance baselines

**Status: run on 2026-09-20 against the local stack** (same images, same verified-TLS topology and the same agent build as the Railway deployment). Runs spend Anthropic credits and load a shared OpenEMR, so they are scheduled, not automated. The deployed-target run and why it needs a browser login are in [Running it against the deployed agent](#running-it-against-the-deployed-agent).

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
| L10 | 10 | 2/s | 5 min |
| L50 | 50 | 5/s | 5 min |

Five minutes rather than ten: with 20–60 s think time a 5-minute run already puts 77 and 374 requests through the agent, which is past the point where p95 stops moving, and it halves the LLM spend (the runs below cost about $0.43 and $2.11 of Anthropic credit).

## Setup

1. **Tokens:** one OAuth access token per virtual user, from synthetic demo users (see README, "Demo users"). Tokens last 1 hour, so each run stays under 50 minutes. Save as a JSON file outside the repo and point `COPILOT_TOKENS_FILE` at it.
2. **Agent:** `ALLOW_API_SESSIONS=true` and `EVAL_PATIENT_IDS` listing the synthetic patients used.
3. **Budget check:** estimate the run's LLM cost first: questions ≈ users × (duration ÷ average think time) × 2.5; cost ≈ questions × measured cost per answer (from Langfuse). Confirm it fits the remaining prepaid Anthropic balance.
4. **Session cap:** the agent allows 3 live sessions per user (the product default, `MAX_SESSIONS_PER_USER`). Every virtual user here launches off the same demo account, so a run must raise it or the store evicts sessions and Locust records 401s that no real physician would ever see. `run_local.sh` raises it for the run only; the deployment is never changed.
5. **Run it:** `sh loadtest/run_local.sh <users> <spawn rate> <duration> <label>`, which rebuilds the agent with the raised cap, mints a token **immediately** before the run (tokens last an hour, and an expired one turns every session create into a 403 — an earlier run was lost to exactly that), aborts unless a session create returns 200, samples container CPU and memory alongside, and writes the Locust CSVs. Against a remote target: `locust -f loadtest/locustfile.py --host https://<agent> --users 10 --spawn-rate 2 --run-time 5m --headless --csv results/l10`.

## What gets recorded

- **From Locust:** p50/p95/p99 latency, requests/s, failures per endpoint. Fallback answers count as failures (same definition as ALERTS.md).
- **From Langfuse** (same time window): FHIR time vs Claude time per request, retries, queue depth on the OpenEMR semaphore, verification outcomes, tokens and cost.
- **From `docker stats`**, sampled every 10 s for `agent`, `openemr` and `mysql`: CPU and memory at idle (5 min before), during L10, during L50. Raw samples are in `<results>/<label>-stats.csv` (written outside the repository, since the same directory holds a token file).
- **From the agent's own structured logs** (one `answer` line per question, correlation id included): outcome, verification result, tokens and cost per answer.

## Results

Local stack on an 8-core Apple Silicon laptop, 5.8 GiB to Docker: MySQL 9.4 with `--require-secure-transport=ON`, OpenEMR 8.5.0, the agent built from this repository — the same three images and the same verified-TLS topology as the Railway deployment, without Railway's network hop. Claude Haiku 4.5 is live in the loop; nothing is stubbed.

### Baselines (idle)

Five minutes with the stack up and no traffic.

| Service | CPU | Memory |
|---|---|---|
| agent | 0.1 % (3.5 % peak on a health poll) | 76 MiB |
| openemr | 0.1 % | 247 MiB |
| mysql | 0.2 % (2.9 % peak) | 772 MiB |

Agent throughput at idle is zero by construction: it does no background work between questions except the 15-minute session sweep.

### L10 — 10 concurrent users

77 requests in 5 min · **0 failures** · 0.26 req/s · ~68 answers, ~$0.43 of Anthropic credit.

| Endpoint | p50 | p95 | p99 | Error rate | Throughput |
|---|---|---|---|---|---|
| brief | 2 500 ms | 3 300 ms | 3 900 ms | 0 % | 0.14 req/s |
| follow_up | 1 600 ms | 2 400 ms | 2 500 ms | 0 % | 0.08 req/s |
| POST /api/sessions | 52 ms | 58 ms | 58 ms | 0 % | 0.03 req/s |
| **Aggregated** | **2 000 ms** | **3 200 ms** | **3 900 ms** | **0 %** | 0.26 req/s |

| Service | CPU peak | Memory peak |
|---|---|---|
| agent | 6.4 % | 91 MiB |
| openemr | 32.0 % | 279 MiB |
| mysql | 56.6 % | 781 MiB |

`schedule_scan` did not run: the `MorningScan` user only starts when `SCHEDULE_TOKENS_FILE` is set, and a standalone schedule launch needs a token minted from a schedule (non-patient) context, which `run_local.sh` does not mint. The scan path is covered instead by eval case U01 (`evals/cases/schedule.json`) and by the schedule-scan tests in `test_fhir.py`.

### L50 — 50 concurrent users

374 requests in 5 min · **7 failures (1.9 %)** · 1.26 req/s · 333 answers, $2.11 of Anthropic credit.

| Endpoint | p50 | p95 | p99 | Error rate | Throughput |
|---|---|---|---|---|---|
| brief | 2 300 ms | 3 000 ms | 4 400 ms | 0.5 % | 0.64 req/s |
| follow_up | 1 500 ms | 2 500 ms | 3 100 ms | 4.3 % | 0.47 req/s |
| POST /api/sessions | 80 ms | 119 ms | 119 ms | 0 % | 0.15 req/s |
| **Aggregated** | **2 000 ms** | **3 000 ms** | **3 700 ms** | **1.9 %** | 1.26 req/s |

| Service | CPU peak | Memory peak |
|---|---|---|
| agent | 6.2 % | 106 MiB |
| openemr | 35.9 % | 293 MiB |
| mysql | 82.9 % | 793 MiB |

Verification outcomes over the same window: **322 pass, 4 refused, 7 fallback.** Tokens: 849 921 in, 55 097 out — $0.0063 per answered question, which matches the per-question cost measured in [AI_COST_ANALYSIS.md](AI_COST_ANALYSIS.md).

## Interpretation

**The p95 target held, with room to spare.** 3.2 s at 10 users and 3.0 s at 50, against a 10 s budget and a 9 s hard deadline. Going 5× on concurrency did not move p95 — it moved *throughput*, from 0.26 to 1.26 req/s, which is the shape you want: the system was latency-bound on a fixed per-question cost, not queueing.

**AUDIT.md's prediction was half right.** It predicted OpenEMR would be the bottleneck. OpenEMR is indeed where the CPU went — MySQL peaked at 83 % of a core and OpenEMR at 36 %, against the agent's 6 % — but neither saturated, so the FHIR tier never became the limit at this scale. The per-question wall clock is dominated by the Claude call, not by FHIR: a brief (one Claude call over a full prefetched chart) runs ~800 ms slower than a follow-up (one Claude call over a warm session), and the prefetch that precedes a brief overlaps the session create rather than the question.

**The 1.9 % error rate is a product outcome, not a capacity failure.** Every one of the 7 was a *fallback*: the model returned a plan with no renderable line (`output_tokens` as low as 18), so the server said it could not answer from the chart rather than inventing one. Five of the seven were "What changed since the last visit?" on synthetic patients with a single encounter — there is genuinely nothing to diff. Counting those as failures is deliberate (it is the same definition [ALERTS.md](ALERTS.md) alerts on) and it is the honest number, but it is the verifier working, not the system bending. No request timed out, no request 5xx'd, no session was lost.

**Memory is flat.** The agent grew 76 → 106 MiB across a 5× load step and released it afterwards. Sessions are bounded (idle TTL, per-user cap, 6-turn history), so there is no growth term proportional to traffic.

**What would give first, and what changes.** MySQL CPU is the steepest curve of the three and it is the one that is *already* 83 % of a core at 50 users — that is the next ceiling, and it is OpenEMR's synchronous audit writes and un-indexed FHIR queries (PERF-1, PERF-2, PERF-3), not anything in the agent. The first change is therefore not to scale the agent: it is to put a read replica or a cache in front of the FHIR reads. The agent's own first constraint is different and structural — sessions live in process memory, so a second replica cannot serve a session the first one opened. [ARCHITECTURE §11](ARCHITECTURE.md) moves the store to Redis, which is what unblocks horizontal scaling; nothing in these numbers says that is needed yet.

## Running it against the deployed agent

The numbers above are from the local stack. The assignment asks for the deployed one, so this section says exactly
what stopped that, because it is not laziness — it is two independent controls in this system, and the second was
only discovered by trying.

A run needs one OAuth access token that the deployed agent will accept. Two things stand in the way:

1. **`deploy/local/mint_token.php` refuses any non-localhost site.** It mints tokens without a browser login, which
   is what makes local load testing cheap, and it checks `site_addr_oath` and exits 2 unless it is `http://localhost`.
   That is deliberate: the shortcut must not be pointable at a public deployment.
2. **The agent only accepts tokens issued to its own OAuth client.** `copilot/smart.py:175-188` introspects every
   presented token using the agent's own `SMART_CLIENT_ID` / `SMART_CLIENT_SECRET`, and rejects it unless OpenEMR
   answers `active: true`. OpenEMR scopes introspection per client, so a token minted through a *different*
   registered client comes back inactive and the session create returns `403 inactive`.

The second one was verified, not assumed. On 2026-09-20 a second confidential client was registered against the
deployed OpenEMR with a `http://localhost` redirect, a real authorization-code + PKCE login was completed as the
demo physician, and the resulting token was introspected twice:

| Introspecting client | `active` |
|---|---|
| The client that issued the token | `true` |
| The Co-Pilot client, which is what the agent uses | **`false`** |

`POST /api/sessions` with that token returned `403 inactive`, exactly as designed. So the only way to load-test the
deployment is a browser login **as the Co-Pilot client itself**, whose sole registered redirect URI is the agent's
own `/smart/callback` — and that callback consumes the code and keeps the token server-side, which is the whole
point of it. Reaching the token would mean either registering a localhost redirect on the client the graders use, or
adding an endpoint that hands out access tokens. Neither is a change worth making to a system whose central claim is
that it does not leak.

**What that costs this document:** the published p50/p95/p99 and error rates are from a laptop stack with no Railway
network hop, so they read optimistically. Treat them as the agent's own behaviour under concurrency — which is what
they measure well — and not as a service-level figure for the deployment.

**To run it anyway**, on a deployment you are willing to reconfigure:

1. Add a `http://localhost:8765/callback` redirect URI to the Co-Pilot OAuth client (OpenEMR: Admin → System → API
   Clients), or register a replacement client carrying both that and the agent's callback and point the agent at it.
2. Complete an authorization-code + PKCE login as the demo physician, requesting `user/` scopes and **not**
   `launch/patient` — a launch context binds the token to one patient and every virtual user would hit that chart.
   The [Bruno collection](api-collection/) does this flow; so does any OAuth client.
3. Raise the server-side cap: `railway variables --service agent --set MAX_SESSIONS_PER_USER=150`. Every virtual
   user launches off one demo account, and the product default is 3.
4. `sh loadtest/run_deployed.sh <token-file> 10 2 5m deployed-l10`, then the same with `50 5 5m deployed-l50`.
5. Set the cap back to 3 and remove the extra redirect URI.

Expect a higher p95 than the local figures by roughly one network round trip per FHIR call plus Railway's ingress,
and MySQL to redline first — it is on a shared instance with less CPU than the laptop these numbers came from.
