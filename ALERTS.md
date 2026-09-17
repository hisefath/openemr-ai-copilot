# ALERTS.md — Alerts and on-call responses

Three alerts sit on top of the Langfuse dashboard ([KEY_METRICS.md](KEY_METRICS.md), [ARCHITECTURE.md §7](ARCHITECTURE.md)). Each one answers: *is a physician between rooms getting a slow, broken, or incomplete answer right now?*

## How alerts are evaluated

- **Evaluator:** [`agent/alerts.py`](agent/alerts.py), deployed as the Railway cron service `alerts` (same image as the agent, start command `python alerts.py`, schedule `*/5 * * * *`, never restarted). It reads Langfuse's public observations API (v2; the legacy traces API is closed to new Langfuse organizations).
- **Window:** the 15 minutes ending **10 minutes ago**. Langfuse Cloud took 1 to 8 minutes to make new spans queryable in our measurements, so a window ending "now" undercounts. The cost is detection delay: an incident pages 10 to 25 minutes after it starts. Faster paging needs metrics pushed to a real-time backend (OTel/Prometheus), not a trace store.
- **Minimum volume:** an alert only evaluates when there are **≥ 20 requests** in the window. Below that, one slow request would page someone at 3 AM for nothing.
- **Notification:** a POST to `ALERT_WEBHOOK_URL` (Slack or Discord incoming webhook) when set. Every run also prints one JSON line (`"alert": true|false`, values, thresholds, window), which Railway log search finds as `alert=true`. No webhook is configured for this project yet (there is no team channel), so Railway logs are where alerts show up.
- **Exit code = the monitor's own health:** 0 whenever the window was evaluated, firing or not; 1 only when the evaluator couldn't reach Langfuse or couldn't deliver a webhook. A "crashed" run in Railway therefore means alerting itself is broken. (The first version exited 1 on a firing alert, and Railway showed every run during the fault tests as crashed.)
- **No de-duplication yet:** a firing alert notifies on every 5-minute run until its window drops below threshold. Add a last-notified record when that gets noisy.
- **No PHI:** payloads contain only metric values, thresholds, request counts and the window.
- **Replay:** `python alerts.py <from> <to>` evaluates any past window (ISO timestamps), for postmortems and for the tests below.

Definitions used below (same as ARCHITECTURE §7):
- **Request** = one `POST /api/session/messages` or `POST /api/schedule/scan`.
- **Error** = HTTP 5xx, or a fallback answer caused by the LLM (timeout, 429, refusal, truncation), an unparseable plan, or a verifier exception. A 4xx caused by the caller (bad input, expired session) is not an error. The agent emits one `metric.error` Langfuse event per error, with a `kind`; the evaluator counts those events (capped at one per request).
- **Tool failure** = any FHIR call (prefetch or tool) that ended `error` or `timeout`. `forbidden` (403, role can't view) is reported separately and does not count, because it's working as designed.

---

## A1 — p95 latency above 10 seconds

| | |
|---|---|
| **Condition** | p95 of request duration > **10 s** over 15 min (≥ 20 requests) |
| **Severity** | High: physicians stop waiting after ~10 s and walk into the room without context |
| **What it usually means** | OpenEMR is slow (most likely: PHP workers saturated, MySQL contention, or a large patient history), or Claude is slow/rate-limited |

**On-call response:**
1. Open the Langfuse dashboard for the window. Compare the **FHIR spans** (prefetch per resource) with the **Claude span** in slow traces. Whichever dominates is the cause.
2. **If FHIR dominates:** check `/ready`, check the OpenEMR service's CPU and memory on Railway (it has a ~1 GB ceiling, AUDIT OPS-3), and check queue depth on the OpenEMR semaphore. Look for one resource type dominating (MedicationRequest is the known slow query, PERF-3).
   - *Mitigate:* lower `OPENEMR_CONCURRENCY` if OpenEMR is thrashing, or restart the OpenEMR service if memory is pinned. Shorter lab and vitals windows can be set without a deploy.
3. **If Claude dominates:** check [status.anthropic.com](https://status.anthropic.com) and Claude spans for 429s.
   - *Mitigate:* the per-question deadline already falls back to non-AI answers after 9 s; confirm fallbacks are appearing rather than hangs.
4. **Escalate** if p95 stays above 10 s for 30 minutes after mitigation: post in the team channel with the dashboard link and the dominant span.

## A2 — Error rate above 5 percent

| | |
|---|---|
| **Condition** | errors / requests > **5 %** over 15 min (≥ 20 requests) |
| **Severity** | High: physicians are getting fallback lists or error screens instead of answers |
| **What it usually means** | Claude API problems (auth, rate limit, outage), an unparseable-plan spike after a model or prompt change, or the audit database refusing writes (requests fail closed, FM-15) |

**On-call response:**
1. In Langfuse, group the window's `metric.error` events by `kind`: `llm_deadline`, `llm_timeout`, `llm_rate_limited`, `llm_api_error`, `llm_connection_error`, `llm_refusal`/`llm_max_tokens` (stop reasons), `llm_unparseable`, `verifier_exception`, `audit_unavailable`, `oauth_unavailable`, `schedule_unavailable`, `http_5xx`.
2. `llm_*`: check the Anthropic console for key validity, credit balance (prepaid cap), and rate limits. **A drained prepaid balance looks exactly like this alert.**
3. `llm_unparseable` or `verifier_exception` right after a deploy: **roll back** the agent to the previous Railway deployment, then investigate with the eval suite.
4. `audit_unavailable`: check MySQL health and the `copilot_audit` user's TLS connection. Answers are deliberately blocked while audit writes fail; don't bypass that.
5. **Escalate** if the cause is not identified in 15 minutes.

## A3 — Tool failure rate above 10 percent

| | |
|---|---|
| **Condition** | FHIR calls ending `error` or `timeout` / all FHIR calls > **10 %** over 15 min (≥ 20 requests) |
| **Severity** | Medium: answers still arrive, but with "Labs unavailable right now"-type gaps that make them less useful |
| **What it usually means** | OpenEMR is overloaded, restarting, or erroring on a specific resource; a token or configuration problem making calls fail; Railway private networking issues |

**On-call response:**
1. In Langfuse, group failed FHIR spans by resource type and status.
2. **One resource type failing** (e.g. only MedicationRequest timing out): an OpenEMR query problem. Check its PHP error log over `railway ssh` and the resource's latency trend.
3. **All resource types failing:** check `/ready` and whether the OpenEMR service restarted recently in Railway's deploy history. OpenEMR's start command waits for its configuration instead of reinstalling (AUDIT OPS-1), so a restart loop shows up as "waiting" in its logs.
4. **Many `expired` statuses** (not counted in this alert, but visible next to it): token lifetime or clock problems; check that launches are recent.
5. **Escalate** if the OpenEMR service is unhealthy for more than 10 minutes.

---

## Testing the alerts

Each alert was fired once on purpose with [`evals/fault_injection.py`](evals/fault_injection.py) on 2026-09-17. The script starts a second local agent (port 8001) with one fault injected, asks 22 questions over 4 sessions as the demo physician, then runs `alerts.py` over exactly that window once Langfuse has ingested every request. Synthetic data only.

| Alert | Fault injected | Fired? | Result (22 requests each) |
|---|---|---|---|
| A1 | Claude calls routed through a proxy that adds 9 s (question deadline raised to 15 s) | **Yes** | p95 **14.6 s**. A2 also fired (13.6 %): 3 answers hit the 15 s deadline and fell back. That is what a slow Claude really looks like: latency first, then timeouts. A3 stayed quiet. |
| A2 | Invalid `ANTHROPIC_API_KEY` | **Yes** | Error rate **100 %** (`llm_api_error`), answers in 0.1 s as non-AI fallbacks. A1 and A3 stayed quiet. |
| A3 | `OPENEMR_FHIR_BASE` pointed at a path OpenEMR doesn't serve (OAuth still works) | **Yes** | Tool failure rate **100 %**: every FHIR call returned an error. Answers still arrived in about 1 s as fallbacks that named each unavailable resource; A2 stayed quiet (0 %) because missing data isn't an LLM, verifier or server error, and A1 stayed quiet (p95 1.8 s). |

**Deployed evaluator:** the Railway cron's first run (13:57 UTC) read the window 13:32–13:47, which held these local test requests, and logged `alert=true` with A1 (p95 11.1 s) and A2 (87 %) firing. Local and deployed agents currently report to the same Langfuse project; in production, set `LANGFUSE_TRACING_ENVIRONMENT` per deployment and filter the evaluator by environment.
