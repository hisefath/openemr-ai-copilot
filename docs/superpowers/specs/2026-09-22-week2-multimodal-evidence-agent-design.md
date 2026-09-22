# Week 2 — Multimodal Evidence Agent

**Design spec.** 2026-09-22. Extends the Week 1 Clinical Co-Pilot with document ingestion, hybrid retrieval, a
supervisor/worker graph, and an eval gate that blocks regressions.

Sources this design answers to: the Week 2 PRD (seven core requirements, one hard gate), the *Intro to Project*
lecture, the Remote Phase Syllabus, and the Evals Part 1–2, Evaluating Agents, Intro to Graphs and Loops lectures.

---

## Summary

A physician opens a chart before a follow-up visit. The recent information that matters is not in the structured
record — it is in a scanned lab PDF and an intake form the front desk uploaded. The Co-Pilot must read both, pull
out the facts with citations that point at the exact place on the page, retrieve the guideline evidence that bears
on them, and answer — separating what the chart says from what the guideline says.

Week 1's central claim was that **the model selects and the server states**. Nothing the model writes reaches a
physician; the server renders every sentence from a cited record. Week 2 extends that claim across two new
boundaries. A vision model reads a smudged scan — so the server locates every extracted value on the page
independently, and says so when it cannot. A retriever returns guideline text — so evidence is labelled as
guideline evidence, never merged with the patient's own record.

Five decisions carry the design:

1. **Staged writes.** The source document goes into the chart, because a faithful copy is not a claim. Extracted
   facts go to a review queue and reach chart records only when a clinician approves them. Week 1's read-only
   promise survives where it matters.
2. **LangGraph with a Plan-and-Execute supervisor and two ReAct workers.** The supervisor answers *what do I do
   next*; each worker loops on its own tools until a **deterministic** termination check says it is done.
3. **Vision extracts, code locates.** Claude reads the page and returns field values. Separately, the server gets
   every word's coordinates and matches values back to them. The model never reports a coordinate.
4. **Hybrid retrieval with Voyage.** BM25 plus dense embeddings, reranked, with only the top grounded evidence
   reaching the answer model.
5. **A gate that cannot flake.** Fifty cases split into ~15 blocking golden cases and ~35 tagged scenarios, run
   against recorded model responses so CI is deterministic, free, and needs no secrets.

**Rejected, with measurement:** TOON. Encoded against this project's real context it is 4–9 % *larger* than the
compact JSON already sent, the block it would shrink is prompt-cached at 0.1×, and compressing it risks dropping
below Haiku's 4,096-token cache floor. Detail in [Appendix A](#appendix-a--rejected-options).

---

## 1. Where the new code lives

Six modules inside the existing `agent/copilot/` package, following the one-module-per-boundary pattern Week 1
established.

| Module | Owns | Depends on |
|---|---|---|
| `documents.py` | Upload to OpenEMR, fetch back, render pages to images | OpenEMR standard API |
| `extract.py` | The vision call, constrained by strict schemas | `llm.py`, `schemas.py` |
| `locate.py` | Word coordinates from the page; match extracted values to them | PyMuPDF, Tesseract |
| `retrieve.py` | Hybrid search over the guideline corpus, plus rerank | Voyage |
| `graph.py` | LangGraph nodes, state, handoff log | all workers |
| `staging.py` | Pending-review store; approval writes to OpenEMR | `audit.py`, OpenEMR |

Schemas extend `schemas.py` rather than forming a parallel vocabulary: `LabReport`, `IntakeForm`, `Citation`,
`EvidenceChunk`, `HandoffRecord`, `RoutingDecision`, `StagedFact`.

**Build order is components before orchestration**, per the *Intro to Project* lecture: *"Building components
incrementally before orchestration to prevent system development failures."* Ingestion and retrieval must work
standalone before the graph wires them together. See [§9](#9-build-order).

---

## 2. The graph

LangGraph, because the cohort was taught it the day before this assignment and the PRD names it first. Nodes stay
**thin** — they call the existing modules, so Week 1's deadline propagation, Langfuse spans and error handling keep
working rather than being re-expressed through someone else's abstractions.

### State

```python
class GraphState(TypedDict):
    session_ref: str            # never the handle
    patient_id: str             # fixed at launch, server-side; workers cannot change it
    question: str | None
    document: DocumentRef | None
    extracted: ExtractedDocument | None
    evidence: list[EvidenceChunk]
    handoffs: list[HandoffRecord]
    deadline: Deadline          # Week 1's harness, threaded through every node
```

### Nodes

**`supervisor`** — a Claude call returning `RoutingDecision{next, reason}` where `next ∈ {extract, retrieve,
answer}`. Plan-and-Execute: it answers *what do I do next*, the graph executes, and the reason is stored. The
model never runs anything itself.

**`intake_extractor`** — a ReAct worker. Tools: `read_page(n)`, `locate_value(text, page)`. It loops — re-reading a
page when a required field is missing or a value fails to locate — until its **deterministic** termination check
passes: the schema validates and every required field is either located or explicitly marked unlocated. Bounded at
3 iterations and by the shared deadline.

**`evidence_retriever`** — a ReAct worker. Tools: `search(query)`, `rerank(candidates)`. It loops — reformulating
the query when nothing clears the relevance floor — until it holds *k* reranked chunks above threshold, or has
tried twice. Deterministic termination, same as above.

**`answer`** — Week 1's `verify_and_render`, extended to render evidence lines alongside chart lines.

### Why termination is code, not a model

The Loops lecture prices this. A judged "am I done" check at ~2 seconds and a cent is invisible for one loop and
**$10 a run plus 33 minutes of deciding-not-working at a factory of fifty**. Our termination conditions are
checkable in code — schema validity, field coverage, a rerank score floor — so they are. This is the same division
Week 1 already makes: the model judges, code decides what we can afford.

### Handoffs

Every transition appends:

```python
HandoffRecord(from_node, to_node, reason, elapsed_ms, correlation_id, iteration)
```

Logged, traced, **and returned in the API response** — so the supervisor's routing is inspectable from outside
without opening Langfuse. That is the direct answer to the PRD's warning: *"Letting the supervisor become a black
box. Handoffs must be logged and explainable."*

---

## 3. Document ingestion

### The constraint that shapes this

OpenEMR's **FHIR API is read-only** for documents — `GET /fhir/DocumentReference`, `GET /fhir/Binary`, and a
`$docref` route that generates CCDs rather than accepting uploads. There is no FHIR Observation write.

The writes live in the **standard REST API**, which needs different scopes:

- `POST /api/patient/:pid/document` — multipart upload
- `POST /api/patient/:puuid/allergy`, `.../medication`, `.../medical_problem` — structured writes
- **No lab result write exists.** Procedures are GET-only.

So intake-form facts have a supported home. Lab values do not — an upstream limitation, stated plainly in
`W2_ARCHITECTURE.md` rather than worked around.

### `attach_and_extract(patient_id, file_path, doc_type)`

1. Upload the source to OpenEMR. This is the round-trip the PRD requires, and the document id becomes the citation
   anchor. Idempotent on content hash, so re-running never creates duplicates.
2. Render pages and collect word coordinates — PDF text layer where present, Tesseract OCR where not.
3. Call Claude with page images, constrained to `LabReport` or `IntakeForm`.
4. Locate every extracted value against the word coordinates.
5. Persist to staging with citations, confidence, and status `pending`.

Returns strict-schema JSON. Never writes a chart record.

### Schemas

`LabReport` carries the PRD's required fields — test name, value, unit, reference range, collection date, abnormal
flag, source citation. `IntakeForm` carries demographics, chief concern, current medications, allergies, family
history, source citation. Both are Pydantic, with validation tests, and both are the contract the vision call is
constrained to rather than a shape checked afterwards.

---

## 4. Citations and the overlay

Every clinical claim carries the PRD's shape:

```python
Citation(source_type, source_id, page_or_section, field_or_chunk_id, quote_or_value, bbox: BBox | None)
```

`bbox` is present when the value was located, `None` when it was not — and an unlocated citation renders as
*"extracted, could not be located on the page"* rather than pointing somewhere approximate. **That is the
mechanism that makes unsupported extractions visible**, which the PRD asks for directly.

The panel renders the page image with boxes overlaid; clicking a claim highlights its box.

### Why the model never reports coordinates

Vision models are unreliable at precise coordinates, and a drifting box is a citation that points at the wrong
thing while looking authoritative. Worse, it makes the model the source of its own proof — the exact pattern Week 1
exists to avoid. Values come from the model; positions come from the page.

---

## 5. Hybrid retrieval

A small guideline corpus relevant to the Week 1 user — chunked, with source metadata.

**Sparse:** BM25 over chunks (`rank_bm25`, pure Python, no service).
**Dense:** Voyage embeddings, brute-force cosine over a committed vector file. The corpus is small by design; a
vector database would be a service to run, secure and explain for no measurable gain.
**Rerank:** Voyage `rerank-2-lite` over the union of both candidate sets.
**Feed forward:** only chunks above the relevance floor, capped at *k*.

Key already validated end to end — embeddings and rerank both live, and the reranker correctly scored a relevant
snippet above an irrelevant one.

**Evidence is never merged with chart facts.** Rendered lines carry `source_type` and are grouped under separate
headings, because the PRD requires the answer to separate patient-record facts from guideline evidence.

---

## 6. The write policy

| Artifact | Destination | Gate |
|---|---|---|
| Source document | OpenEMR chart storage | None — a faithful copy is not a claim |
| Extracted facts | `copilot_staged_fact` (agent-owned, beside `copilot_audit`) | None — staging is not the chart |
| Approved facts | OpenEMR allergy / medication / problem records | **Clinician approval in the panel** |
| Lab values | Staging only, cited against the document | No API exists (documented) |

Staged rows carry the citation, extraction confidence, status, and who approved when. Approval is an audited
event. Rejection is too — a rejected extraction is a training signal and an eval case.

This satisfies "persist derived facts as appropriate OpenEMR records" while keeping the property that makes the
system trustworthy: **a vision model reading a smudged scan cannot alter a chart on its own.** Today's *Advanced
Graphs* lecture covers human-in-the-loop nodes; this is one.

### Auth change

Week 1's client holds read-only FHIR scopes. Document and record writes need OpenEMR's standard API scope with
write permission, which means re-registering the SMART client. A real change, called out here so it is not a
surprise mid-build.

---

## 7. The eval gate

The hard gate, stated twice in the PRD: *graders will introduce a regression and confirm the build fails.*

### Structure: 15 + 35, plus a holdout

Evals Part 1 slide 13 distinguishes two things the PRD's "50-case golden set" runs together:

| | Golden sets | Labeled scenarios |
|---|---|---|
| Size | 10–20 | 30–100+ |
| All must pass? | **Yes** | No |
| When | Every commit | Every release |

So: **~15 golden cases** that block the build, **~35 tagged scenarios** (category × difficulty) that report
coverage and show where cases are missing. Plus a **holdout never tuned against** — *"the moment you optimise
against a set, it stops measuring quality and starts measuring memorisation."*

### The five rubric categories

All boolean, never 1–5. Four need no judgement at all:

| Category | Grader | Rung |
|---|---|---|
| `schema_valid` | Pydantic validates, or does not | 1 — assertion |
| `citation_present` | Every rendered clinical line carries citation metadata | 2 — invariant |
| `safe_refusal` | Outcome enum is `refused` | 1 — assertion |
| `no_phi_in_logs` | Scan captured logs for fixture identifiers | 2 — invariant |
| `factually_consistent` | Binary judge, per claim against source | 4 — judge |

*"Climb only as high as you must. Every rung up costs money, latency and trust."*

### Trajectory graders, not only output graders

*"You are grading a path, not a string."* Beyond the five, each case asserts on the trajectory: which nodes ran and
in what order, how many worker iterations, whether verification ran before the answer was returned. A case can
return a correct answer by the wrong route and still fail — the "passed for the wrong reason" state.

### Attempts measured separately from successes

*"A fence is not a fix."* Week 1's `withheld` counter is already the attempt rate: how often the model cites
something that is not there. Reported next to the block rate, because a guardrail that stops damage without
changing behaviour is not a fix — and removing it would bring the behaviour back intact.

### Recorded responses

Real Claude output captured once and committed; CI replays it. This is Evals Part 1 Stage 3 — *"Record once. Score
anytime."*

Three reasons, in order of weight:

1. **Statistics.** Slide 34: at n=10, a 90 % pass rate carries ±19 points. Per-category, these are ~10-case
   buckets. A 5 % regression threshold on live runs is **inside the noise band**. Recorded replay removes sampling
   variance, which is what makes the PRD's threshold meaningful rather than theatre.
2. **A gate that costs money and flakes gets disabled**, and then it is not a gate.
3. CI needs no secrets.

Re-recording is a deliberate command. The diff in the recordings is itself reviewable.

### Judge calibration

Before the judge is trusted: score 20 cases by hand, run the judge on the same ones, require **correlation ≥ 0.8**.
*"A judge with a bad rubric produces confident, wrong scores."* Committed as `evals/w2/judge_calibration.json`, and
it doubles as the PRD's required "judge configuration".

### Thresholds

```python
GATE = {
    "schema_valid":         0.95,
    "citation_present":     1.00,   # safety-shaped
    "factually_consistent": 0.90,
    "safe_refusal":         1.00,   # safety-shaped
    "no_phi_in_logs":       1.00,   # safety-shaped
}
MAX_REGRESSION = 0.05
```

Build fails if any category drops below its floor **or** regresses more than five points against the committed
baseline. *"A suite that blocks nothing is a dashboard."* Safety-shaped checks get a floor of 1.00.

Failure output names the category, the baseline, the new rate, and which cases flipped — so a red build is
actionable rather than a red X.

### Blocking in two places

A tracked **`.githooks/pre-push`** (enabled with one config line) and a **CI job** running the same script. The
PRD says "PR-blocking Git Hook"; a local hook can be skipped with a flag, so CI covers the case a grader actually
exercises.

---

## 8. Observability

Requirement 7 asks for seven things per encounter. Extending Week 1's `answer` log line and trace:

| Required | Source |
|---|---|
| Tool sequence | The handoff records |
| Latency by step | Per-node timing already on each span |
| Token usage, cost estimate | Existing `llm.py` accounting, extended to vision and embedding calls |
| Retrieval hits | Candidate count, reranked count, score distribution |
| Extraction confidence | Per field, plus the located / unlocated ratio |
| Eval outcome | Case id and rubric results when running under the suite |

**No raw PHI** — the same discipline, now covering document text, extracted values and page images, which the PRD
explicitly calls sensitive. Page renders are never sent to Langfuse; only counts and coordinates.

---

## 9. Build order

Components first, orchestration second.

1. **Schemas + validation tests.** The contract before anything that produces it.
2. **`documents.py` + `locate.py`.** Upload, fetch, render, word coordinates. Testable with no model.
3. **`extract.py`.** Vision against the schemas. Testable with recorded responses.
4. **`retrieve.py`.** Corpus, index, rerank. Testable with no graph.
5. **`staging.py` + the approval endpoints.**
6. **`graph.py`.** Wire the working components together.
7. **The eval suite**, growing alongside from step 1 — not bolted on at the end.
8. **UI**: upload control, overlay, review queue.
9. **Docs**: `W2_ARCHITECTURE.md`, `KEY_METRICS.md`, `W2_COST_AND_LATENCY.md`, README Week 1 / Week 2 split.

Each step ships something testable. If the deadline bites, the cut line is after step 6 — a working core flow with
a real gate beats a broad one that cannot block a regression.

---

## 10. Risks

**The scan is genuinely bad.** OCR misreads, values fail to locate. Mitigated by design — an unlocated value is
surfaced as unlocated, never silently dropped or approximately placed. Eval cases cover a deliberately degraded
scan.

**LangGraph re-plumbing eats the schedule.** Mitigated by thin nodes: the graph owns control flow only, every
module keeps working as it does now. If it turns hostile, the fallback is a hand-rolled graph behind the same node
interfaces — a few hours, not a rewrite.

**The auth change breaks the Week 1 launch.** Mitigated by re-registering a client with the superset of scopes and
verifying a SMART launch before anything else depends on it. Week 1's lesson: OpenEMR's introspection accepts any
secret, so **the only real verification is an actual launch**.

**Duplicate records on re-ingest.** The PRD requires documents and derived observations to round-trip *"without
creating duplicate or untraceable records."* Content-hash idempotency on upload; staged facts keyed by
`(document_id, field_path)`. Thursday's *Converting Legacy Systems* class covers exactly this and the syllabus says
**"expect a curveball"** — worth attending before finalising.

**Week 3 attacks this.** The syllabus: *"turns the challenger into the attacker, building a multi-agent adversarial
evaluation platform against their own co-pilot."* Document text is an attacker-controlled channel into the prompt.
Treated as data throughout, and the adversarial eval cases include injection via chart and document content.

---

## 11. Not doing

Deliberately out of scope, per the PRD's closing line — *"the best submissions will feel narrower than the original
spec and stronger because of it."*

- **A third document type.** Two must work reliably first; the PRD names this as pitfall one.
- **ColQwen2 / multi-vector indexing.** Stretch, stated as such.
- **A vector database.** The corpus is small; brute-force cosine over a committed file is defensible and has no
  service to secure.
- **The critic agent.** Extension work per the PRD. It is Generator–Evaluator from the Loops lecture, and slots in
  as a fifth node if time allows.
- **TOON.** Measured and rejected — [Appendix A](#appendix-a--rejected-options).

---

## Appendix A — Rejected options

### TOON (Token-Oriented Object Notation)

Evaluated on 2026-09-22 by three independent investigators; all three rejected it, all three verdicts held under
adversarial review.

| Measurement | Result |
|---|---|
| Drop-in, `patient_a` | **+9.0 %** tokens (5,266 → 5,739) |
| Drop-in, `patient_b` | **+4.3 %** tokens (9,881 → 10,309) |
| Forced uniform (delete `_compact()`, null-pad) | −15 % ceiling |

Measured with Anthropic's token counting endpoint on `claude-haiku-4-5`, against the compact JSON the code actually
emits — not naive JSON.

**Why it loses.** TOON's tabular path needs uniform arrays of primitives. `render.py:_compact()` strips empty
fields per record, leaving the context 43–50 % uniform — inside the "savings shrink" band TOON's own documentation
flags. Non-uniform arrays fall back to a list form bulkier than compact JSON.

**Why even the best case is not worth it.** The chart block is prompt-cached at 0.1× input. A 15 % saving is under
half a cent per ten-turn session. Output tokens cost 50× a cache read and are JSON-schema-constrained, so TOON
cannot touch them. Compressing the block also risks dropping below Haiku 4.5's 4,096-token cache floor — losing
caching entirely, a net loss.

**Why it is wrong here specifically.** TOON rows are comma-positional with no key per value; one misalignment
shifts a lab value into the units column. Independent benchmarks show accuracy regression on nested data (43.1 %
vs JSON 50.3 %). Reaching the ceiling requires null-padding every absent field — converting "not recorded" into an
explicit null, which fights the normalization work that exists because "empty" and "unknown" must stay
distinguishable. The Python ecosystem is pre-1.0 and fragmented across six PyPI packages.

### Hand-rolled graph

Reconsidered after finding the cohort was taught LangGraph the day before the assignment, with a teaching repo.
A custom graph remains defensible on the lecture's own terms — *"nearly all agents in production are customized"* —
but alignment with what was taught is worth more than the dependency costs.
