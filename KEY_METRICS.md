# KEY_METRICS.md — How we know the Clinical Co-Pilot works

**The promise** ([USERS.md](USERS.md)): in the ~90 seconds between rooms, a primary care physician gets the context that matters for today's visit, **every fact is traceable to her patient's chart**, nothing dangerous is missed, and no one sees a patient they shouldn't.

Six numbers test that promise. Each one would change a decision if it moved. Everything else we measure (token counts, FHIR call timings, queue depth) is diagnostic: it explains *why* one of these six moved.

| # | Metric | Target | Use cases | Measured by |
|---|---|---|---|---|
| 1 | **Time to first verified answer** (question → rendered answer), p50 / p95 | ≤ 5 s / ≤ 10 s | UC1–UC4 | Langfuse trace duration per `POST /api/session/messages`; launch → first answer tracked separately |
| 2 | **Unsupported-statement rate**: share of model-selected items removed by verification | < 2 % in evals; watched in production | UC1–UC5 | Verification span: `withheld_count / (items selected)` |
| 3 | **Safety-rule recall on seeded cases**: seeded conflicts (allergy ↔ drug, bleeding risk, metformin + low eGFR, critical labs) that were flagged | 100 % | UC2, UC5 | Live eval tier, seeded synthetic patients |
| 4 | **Wrong-patient or unauthorized disclosures** | 0 | UC6 | Eval cases (other patient, bulk, injected instructions, role without access) + `denied` / `refusal` audit rows in production |
| 5 | **Honest-gap rate**: when data is missing, empty, forbidden or failed, the answer says so | 100 % | UC1–UC5 | Eval assertions on coverage lines; production: every non-`ok` load status has a coverage line |
| 6 | **Follow-up rate**: sessions with at least one follow-up question after the first answer | Baseline, then ↑ | UC4, overall usefulness | Count of questions per session (no question text stored) |

## Why each metric

### 1. Time to first verified answer
The physician decides in seconds whether to wait or walk into the room. An answer that arrives at 15 s might as well not exist: she's already talking to the patient. We measure p95, not the average, because one slow answer in a 20-patient day teaches her to stop opening the panel. It's measured **after verification**, because that's what she actually waits for.

*What it doesn't prove:* that the answer was useful. That's why it's paired with #2, #3 and #6.

### 2. Unsupported-statement rate
The core trust claim is that nothing reaches the physician without a source in *this* chart. The architecture makes an unsupported sentence structurally impossible (the server writes every sentence from a cited record), so the question becomes: how often does the model *try* to cite something that isn't there? A rising rate means the model is confused by the context (a prompt, model or data change) before it shows up as a worse answer. Removals are counted, and the physician sees "N statements withheld", so the product is transparent about it.

*What it doesn't prove:* that the model picked the *right* records. Selection quality is measured by the eval suite's expected-record assertions.

### 3. Safety-rule recall on seeded cases
A missed allergy conflict is the failure that harms a patient. Recall on known, seeded dangerous combinations must be 100 %: every one of them must produce a flag with its sources. We measure recall, not precision, as the headline, because a false alarm costs a few seconds, while a miss can cost the patient. False alarms are still tracked in the eval results, because a flag that fires on everything gets ignored.

*What it doesn't prove:* coverage beyond our explicit rule table. The rule set is intentionally small, and every safety answer lists what wasn't checked.

### 4. Wrong-patient or unauthorized disclosures
HIPAA exposure and trust in one number. The target is zero, and a single occurrence is an incident. OpenEMR's FHIR API doesn't bind `user/` tokens to a patient (AUDIT SEC-1), so this boundary is ours to enforce and must be measured, not assumed. Measured two ways: adversarial eval cases (typed names of other patients, injected "list all patients" text, a role without medication access) and production audit rows for every refusal and every denied record id.

*What it doesn't prove:* that OpenEMR's own role setup is right for the clinic. Care-team restrictions don't exist in OpenEMR (stated in ARCHITECTURE.md).

### 5. Honest-gap rate
OpenEMR's data regularly looks like "nothing" when it's really "unknown": empty allergy lists, uncoded substances, 403s on some endpoints, timeouts (AUDIT DQ-1, DQ-2, SEC-2). The dangerous answer is "no allergies" when the truth is "allergies unavailable". This metric requires that every answer states each gap in plain words. It's what makes an incomplete answer safe to act on.

### 6. Follow-up rate
The only usage signal we collect without storing what was asked. If physicians ask a second question, the first answer was trusted enough to continue the conversation, and the multi-turn capability (UC4) is earning its complexity. Measured as a baseline in the first weeks; the direction matters more than the absolute value.

*What it doesn't prove:* satisfaction. In a real deployment we'd add a one-tap "useful / not useful" on each answer.

## How these become the dashboard

The Langfuse dashboard shows #1, #2 and #6 continuously, next to the engineering minimums — total requests (questions + schedule scans), errors, p50/p95 latency, tool call counts, retries, queue waits and verification pass/fail. It is code, not clicks: [`deploy/langfuse_dashboard.py`](deploy/langfuse_dashboard.py) defines all 17 widgets and is safe to re-run.

Two of those minimums are **rates**, and Langfuse's widget API aggregates one measure per widget, so they stay as formulas over numbers that are both on the board: **error rate = Errors ÷ Requests**, **tool failure rate = tool_failure ÷ fhir_call**. Those are the same definitions the alerts in [ALERTS.md](ALERTS.md) evaluate in code, so the board and the pager cannot drift apart.

#3, #4 and #5 come from the eval suite on every run ([evals](evals/)) and from audit rows in production.

## Current values

Measured on the live eval run of 2026-09-20 ([results](evals/results/20260920T133102Z.json), commit `20f1c23`, local stack mirroring production, real Claude Haiku 4.5, synthetic patients), with #1 cross-checked against the 50-user load test in [LOAD_TEST.md](LOAD_TEST.md). No real clinicians have used the Co-Pilot, so production values don't exist yet.

| # | Metric | Target | Current | Status |
|---|---|---|---|---|
| 1 | Time to first verified answer | p50 ≤ 5 s / p95 ≤ 10 s | **p50 1.4 s / p95 2.6 s** (max 3.1 s, 28 timed answers); under 50 concurrent users, **p50 2.0 s / p95 3.0 s / p99 3.7 s** over 374 requests | Met in evals and under load |
| 2 | Unsupported-statement rate | < 2 % | **0 %**: 0 items withheld across 182 rendered lines in 32 cases | Met in evals |
| 3 | Safety-rule recall on seeded cases | 100 % | **100 %**: S01–S05 and S09 flagged every seeded conflict (penicillin ↔ amoxicillin, brand name Augmentin, cephalosporin cross-reactivity, clopidogrel + NSAID, metformin + eGFR 24 + K 6.4); S07 confirmed no false alarm | Met |
| 4 | Wrong-patient or unauthorized disclosures | 0 | **0**: all 8 UC6 cases passed (X01–X05 adversarial, B06, B07, B11) | Met |
| 5 | Honest-gap rate | 100 % | **100 %** of gap cases (B01 empty allergies, B02 uncoded allergy, B03 missing vitals, B12 missing permission, S06 unknown drug) | Met |
| 6 | Follow-up rate | Baseline | **Not yet measurable**: needs real clinicians. Multi-turn follow-ups work (C01, C02), and the load test drove 139 follow-ups over 45 sessions | Pending real use |

Where these can mislead: the evals run on 24 synthetic patients and a small, known rule table, so #2–#5 prove the guarantees hold on the cases we thought of, not on every chart. #1's load-test figure is from the local stack, which has no Railway network hop — expect the deployed p95 to be higher (LOAD_TEST.md says by how much and why). The alert tests and their results are in [ALERTS.md](ALERTS.md).
