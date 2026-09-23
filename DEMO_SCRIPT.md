# Demo script — Week 2, 3–5 minutes

Not part of the submission; a shooting script for you. The PRD asks the video to show **document upload,
extraction, evidence retrieval, citations, eval results, and observability**. Every one of those has a beat
below, in the order that tells the best story.

**Read this once before recording.** The single most valuable shot is the last one — the gate going red — and it
is the one people run out of time for. If you are running long, cut the observability beat, not that.

---

## Pre-flight (do this before you hit record)

Five minutes, and it turns a re-shoot into a take.

| # | Check | How |
|---|---|---|
| 1 | The six scopes are on the Railway OpenEMR client | Admin → System → API Clients → the Co-Pilot client. The Scopes list there is READ-ONLY — it tells you what the client has, it cannot widen it. Need: `api:oemr`, `user/document.crs`, `user/allergy.cruds`, `user/medical_problem.cruds`, `user/medication.cruds`, `user/patient.crus`. Any missing → re-register (see README) and swap `SMART_CLIENT_ID`/`SECRET` on Railway |
| 2 | The demo user's group has the three ACL entries | **Administration → ACL** (NOT the Access Control box in Edit User — that only picks the group). Under *Patient Information*: **Documents** needs **write** or *add only* (`_rest_routes_standard.inc.php:497`); **Medical Records and History** and **Demographics** just need to be granted, no write qualifier. A stock Physicians group has all three |
| 3 | The app answers | `curl -s https://agent-production-e0ed.up.railway.app/ready` → `{"ready":true,…}` |
| 4 | **Do one silent dry run of the whole flow** | Launch → attach → approve. If the approve 403s, it is check 1 or 2 |
| 5 | Have the two documents ready on your desktop | Generate them: see *Documents* below |
| 6 | A terminal open in the repo, font size up | You will run two commands on camera |

**Generate the demo documents:**

```bash
cd ~/Desktop/gauntlet_workbench/openemr-ai-copilot
docker run --rm -i -v "$PWD":/repo -w /repo agentforge-agent-w2 python - <<'PY'
import sys; sys.path.insert(0, "/repo/evals/w2")
import fixtures
for name in ("intake_full", "lab_degraded"):
    open(f"/repo/{name}.pdf", "wb").write(fixtures.get(name))
    print("wrote", name + ".pdf")
PY
mv intake_full.pdf lab_degraded.pdf ~/Desktop/
```

`lab_degraded.pdf` is the one that produces the honest "could not be located" — that is deliberate, and it is
your best 20 seconds.

---

## The shots

### 0:00 — 0:25 · What this is

> *"This is a Clinical Co-Pilot inside OpenEMR. Last week it answered questions from structured records and
> cited every sentence. This week it can read the documents that actually matter before a visit — a scanned lab
> and a front-desk intake form — without inventing anything."*

**On screen:** the OpenEMR chart, Co-Pilot panel open beside it.

---

### 0:25 — 1:15 · Attach a document

**Do:** drag `intake_full.pdf` into the panel, pick *Intake form*, click **Attach & read**.

> *"The document goes into the chart first, before anything is extracted — a faithful copy isn't a claim about
> the patient. Then a vision model reads it."*

**Wait for the boxes.** Point at them.

> *"The model returned values. It was never asked for a coordinate — the schema it's constrained to has no box
> field at all. The server found each value on the page itself, using the page's own word positions. So the box
> is evidence the value is really there, not the model marking its own work."*

---

### 1:15 — 1:55 · The beat that sells it

**Do:** attach `lab_degraded.pdf`. One value comes back **without** a box, reading *"extracted, could not be
located on the page."*

> *"This is the part I care about most. That row prints the result and a reference range that share a number, so
> the page genuinely can't say which one the model meant — and it says so, instead of drawing a confident box
> around the wrong ink. A wrong value with a box would look better evidenced than an honest gap. That's the
> failure this design exists to prevent."*

Pause a beat after that line. It is the strongest thing in the project.

---

### 1:55 — 2:40 · Approve → the chart

**Do:** in the review queue, click **Approve → chart** on the penicillin allergy.

> *"Nothing has touched the record until now. Extraction writes to a review queue; only a clinician's approval
> writes to the chart — and it writes under the clinician's own token, so OpenEMR's audit log records that they
> did it, not that a robot did."*

**Do:** switch to the terminal, read it back:

```bash
curl -s "$OPENEMR/apis/default/api/patient/$PUUID/allergy" \
  -H "Authorization: Bearer $TOKEN" | python3 -m json.tool | grep -A2 Penicillin
```

> *"There it is in the chart, carrying the document id, the page, and the exact field it came from. That's
> Requirement 1's round trip — document in, derived fact out, provenance intact."*

*(If a live curl feels risky on camera: open the allergy in OpenEMR's own UI instead and point at the comment
field. Same point, no terminal.)*

---

### 2:40 — 3:15 · Ask a question

**Do:** type *"Is it safe to start amoxicillin?"*

> *"The supervisor decides what happens next; two workers do the work. Every routing decision comes back on the
> response — not just into a trace — because a supervisor nobody can inspect is the failure the assignment
> warns about."*

**Do:** show the answer's flags and cited lines, then the guideline evidence under its own heading.

> *"Guideline evidence is always labelled as guideline. It never merges with this patient's record, because
> those are different kinds of claim."*

---

### 3:15 — 4:15 · The gate, and making it fail

This is the hard gate. **Do not skip it.**

```bash
python evals/w2/run_gate.py
```

> *"Fifty-five cases, all of them blocking. No network, no API key — they replay recorded model responses, which
> is what makes a five-percent threshold mean something instead of measuring sampling noise."*

**Then break something.** Either edit one line of the system prompt, or just:

```bash
ANTHROPIC_MODEL=claude-opus-5 python evals/w2/run_gate.py; echo "exit=$?"
```

> *"Recordings are keyed on a hash of the model-facing surface — the prompt, the model, the tools, the schema. I
> changed the model, so every case is a cache miss, and a cache miss is a hard failure, never a silent pass.
> That's the regression a grader introduces, and the build goes red on its own."*

**Show `exit=1` and the red output.** Then revert and show it green again.

*(30-second version if you are tight: `python evals/w2/run_gate.py --selftest` — one deliberately-broken case
whose only job is to prove the runner can fail. It passes only if that case fails.)*

---

### 4:15 — 4:45 · Observability, briefly

**Do:** Langfuse — one trace, expanded.

> *"Per encounter: the tool sequence, latency per step, tokens and cost, retrieval hits, extraction confidence.
> And no PHI — routing reasons are enum codes, not model prose, and page images never leave the browser."*

---

### 4:45 — 5:00 · Close

> *"Two document types, one supervisor and two workers, hybrid retrieval with a measured relevance floor, and a
> gate that blocks the build and can prove it goes red. Narrower than the original spec, and stronger for it."*

---

## If something goes wrong on camera

| Symptom | Almost certainly | Do |
|---|---|---|
| Approve returns 403 | `patients`/`docs` ACL, or the six scopes not registered | Pre-flight 1 and 2. Keep filming the queue and say the write is gated — it is true |
| Upload 400s `ingest_failed` | Document category missing in that OpenEMR | Use `intake_full.pdf`, which lands in *Patient Information* |
| No boxes at all | Tesseract path, or a scan with no text layer | The fixtures have a text layer, so this should not happen. Say values are shown unlocated — which is the honest behaviour, not a bug |
| Evidence section empty | Voyage rate limit (3/min on the free tier) | Say retrieval found nothing above the floor. Also true, and the floor is the point |
| Answer is slow | Cold start on Railway | Ask one throwaway question before recording |

## What NOT to claim

Worth being disciplined about, because the AI interview will probe exactly these:

- **Don't say the 50-case set was scored against real Claude output.** The recordings are fixtures with real
  surface keys; `W2_COST_AND_LATENCY.md` says so and prices the re-record at ~$1.50.
- **Don't quote an ingestion latency number.** There isn't a real one yet — `KEY_METRICS.md` #12 says *Pending*.
- **Don't say the supervisor is essential.** Say its divergence from the deterministic policy is *measured*, and
  that if it comes back at zero you would demote it to a rule. That is a stronger answer.
- **Don't say lab values reach the chart.** They can't — OpenEMR has no lab-result write route. Staged and cited
  against the document, stated as an upstream limitation.
