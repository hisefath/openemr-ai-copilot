# ALERTS.md — Alerts and on-call responses

Three alerts sit on top of the Langfuse dashboard ([KEY_METRICS.md](KEY_METRICS.md), [ARCHITECTURE.md §7](ARCHITECTURE.md)). Each one answers: *is a physician between rooms getting a slow, broken, or incomplete answer right now?*

## How alerts are evaluated

- **Evaluator:** `agent/alerts.py`, run every 5 minutes by a Railway cron service. It reads the last 15 minutes of traces from the Langfuse public API.
- **Minimum volume:** an alert only evaluates when there are **≥ 20 requests** in the window. Below that, one slow request would page someone at 3 AM for nothing.
- **Notification:** a POST to `ALERT_WEBHOOK_URL` (Slack or Discord incoming webhook). If unset, the alert is written to the agent logs as a JSON line with `"alert": true`, which Railway log search can find.
- **De-duplication:** an alert that is already firing re-notifies at most every 30 minutes, and sends one "resolved" message when it clears.
- **No PHI:** alert payloads contain only metric values, thresholds, window, and a link to the Langfuse dashboard filtered by time.

Definitions used below (same as ARCHITECTURE §7):
- **Request** = one `POST /api/session/messages` or `POST /api/schedule/scan`.
- **Error** = HTTP 5xx, or a fallback answer caused by the LLM (timeout, 429, refusal, truncation), an unparseable plan, or a verifier exception. A 4xx caused by the caller (bad input, expired session) is not an error.
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
1. In Langfuse, group the window's errors by `error_kind` span metadata (`llm_timeout`, `llm_429`, `llm_refusal`, `plan_unparseable`, `verifier_exception`, `audit_unavailable`, `http_5xx`).
2. `llm_*`: check the Anthropic console for key validity, credit balance (prepaid cap), and rate limits. **A drained prepaid balance looks exactly like this alert.**
3. `plan_unparseable` or `verifier_exception` right after a deploy: **roll back** the agent to the previous Railway deployment, then investigate with the eval suite.
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

Each alert is fired once on purpose to prove the pipeline works end to end, and the result is recorded here:

| Alert | Fault injected | Fired? | Notes |
|---|---|---|---|
| A1 | Temporarily set `QUESTION_DEADLINE_S=15` and add latency to the local OpenEMR container | _pending_ | |
| A2 | Invalid `ANTHROPIC_API_KEY` in a local run | _pending_ | |
| A3 | Stop the local OpenEMR container during a scripted run | _pending_ | |
