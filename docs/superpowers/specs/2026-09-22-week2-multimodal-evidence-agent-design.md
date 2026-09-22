# Week 2 — Multimodal Evidence Agent

**Design spec.** 2026-09-22. Extends the Week 1 Clinical Co-Pilot with document ingestion, hybrid retrieval, a
supervisor/worker graph, and an eval gate that blocks regressions.

Sources this design answers to: the Week 2 PRD (seven core requirements, one hard gate), the *Intro to Project*
lecture, the Remote Phase Syllabus, and the Evals Part 1–2, Evaluating Agents, Intro to Graphs and Loops lectures.

**Revised 2026-09-22** after a six-dimension audit against the PRD and the Week 1 codebase. Four decisions changed:
all fifty eval cases now block (§7), the gate is built at the existing `FakeClaude` seam rather than as new
infrastructure (§7), the approval write is demonstrated rather than merely available (§6), and the physician's own
session carries the write scopes (§6). The build order inverts to put the gate first (§9). Points where this
revision corrects the earlier draft are marked in place rather than quietly edited.

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

1. **Staged writes, with the write demonstrated.** The source document goes into the chart, because a faithful
   copy is not a claim. Extracted facts go to a review queue and reach chart records only when a clinician approves
   them — and the approval write is a mandatory demo beat plus two blocking eval cases, so staging reads as *a
   control on writing* rather than an excuse for not writing. The clinician's session writes a faithful copy
   ungated and writes derived clinical facts only on approval; **the model cannot write at all.**
2. **LangGraph with a Plan-and-Execute supervisor and two ReAct workers.** The supervisor answers *what do I do
   next*; each worker loops on its own tools until a **deterministic** termination check says it is done. Its
   routing is logged against a deterministic baseline, so the supervisor's value is measured rather than assumed.
3. **Vision extracts, code locates.** Claude reads the page and returns field values. Separately, the server gets
   every word's coordinates and matches values back to them. The model never reports a coordinate.
4. **Hybrid retrieval with Voyage.** BM25 plus dense embeddings, reranked, with only the top grounded evidence
   reaching the answer model.
5. **A gate that cannot flake, and cannot silently pass.** All fifty cases block the build. They run at Week 1's
   existing `FakeClaude` seam against recorded model responses keyed by a hash of the model-facing surface — so a
   changed prompt is a cache miss, and a cache miss is a hard failure. Deterministic, free, no secrets, and red on
   the regression a grader will actually introduce.

**Rejected, with measurement:** TOON. Encoded against this project's real context it is 4–9 % *larger* than the
compact JSON already sent, the block it would shrink is prompt-cached at 0.1×, and compressing it risks dropping
below Haiku's 4,096-token cache floor. Detail in [Appendix A](#appendix-a--rejected-options).

---

## 1. Where the new code lives

Seven modules inside the existing `agent/copilot/` package, following the one-module-per-boundary pattern Week 1
established.

| Module | Owns | Depends on |
|---|---|---|
| `documents.py` | Upload to OpenEMR, fetch back, render pages to images | OpenEMR standard API |
| `extract.py` | The vision call, constrained by strict schemas | `llm.py`, `schemas.py` |
| `locate.py` | Word coordinates from the page; match extracted values to them | PyMuPDF, Tesseract |
| `retrieve.py` | Hybrid search over the guideline corpus, plus rerank | Voyage |
| `graph.py` | LangGraph nodes, state, handoff log | all workers |
| `staging.py` | Pending-review store; approval writes to OpenEMR | `audit.py`, `emr_write.py` |
| `emr_write.py` | OpenEMR **standard REST API** client — multipart upload, structured writes | `smart.py` |

`emr_write.py` is surface an earlier draft assumed away: `fhir.py`'s `FhirClient` exposes only `get()`, so **no
code path in this repo writes to OpenEMR at all today.** Budget it as a module, not a method.

Two existing files are also in scope and were missing from that table. `main.py` is ~520 lines and currently holds
the orchestration `graph.py` is meant to take over — that migration rewrites its middle rather than adding a file
beside it. And `verify.py` is what the answer node actually costs; see [§2](#the-answer-node-is-the-largest-code-change).

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
    prior_turn: TurnSummary | None   # what the last answer grounded on; the follow-up decision reads this
    handoffs: list[HandoffRecord]
    deadline: Deadline          # Week 1's harness, threaded through every node
```

`prior_turn` exists because the supervisor's one non-trivial decision — answer from held state or re-retrieve —
has nothing to reason about without it.

### Nodes

**`supervisor`** — a Claude call returning `RoutingDecision{next, reason}` where
`next ∈ {extract, retrieve, answer, refuse}` and **`reason` is a closed enum**, never free text:
`{no_document_extracted, evidence_below_floor, extraction_exhausted, retrieval_exhausted, deadline_expired,
budget_exhausted, ready_to_answer, out_of_scope}` — the three exhaustion codes exist so the failure paths below
have something loggable. Plan-and-Execute: it answers *what do I do next*, the graph executes, and **the enum code
is what gets stored.** The model never runs anything itself.

Three things that shape matters for:

- **`refuse` short-circuits.** Without it, an out-of-scope question runs extraction *and* retrieval before Week 1's
  answer model can refuse (`plan.scope_violation`, `verify.py:135`) — spending the whole budget to reach a fixed
  string.
- **The enum protects COMP-3.** `llm.py`'s contract is that prompt and completion text are never logged or traced.
  A free-text `reason`, logged and traced, regresses that critical control and walks into the PRD's fifth pitfall.
  Enum codes are also *countable* in Langfuse, which is strictly better observability. Prose rationale, if wanted
  for the demo, goes to the browser only.
- **The prompt is built from shape, never values** — `document: present`, `extracted: partial`, `evidence: 4
  chunks`, plus the physician's question. Document text is an attacker-controlled channel; this closes the
  injection path and the PHI path in one line. The stated boundary: **chunk and field *metadata* crosses**
  (counts, source ids, scores, which fields resolved), **chunk and field *text* never does.** The follow-up
  decision needs to know what the last turn covered, and metadata is enough to tell it.

### Is the supervisor doing real work?

A fair question, and the most predictable one in the technical interview. With `document`, `extracted` and
`evidence` all on the state, the routing policy is three null checks — and an LLM whose output is fully predicted
by three `is None` tests is decoration.

So the supervisor is given the one decision no null check can make: **on a follow-up question, answer from existing
state or re-retrieve?** The PRD explicitly requires follow-ups to work. And every routing call **logs the
counterfactual** — what the deterministic policy would have chosen, and whether the model differed. That
disagreement rate is reported in `KEY_METRICS.md`.

If it comes back at zero, the honest move is to say so and demote the supervisor to a rule *with the measurement
that justifies it*. Volunteering the number beats defending the design.

**`intake_extractor`** — a ReAct worker. Tools: `read_page(n)`, `locate_value(text, page)`. It loops — re-reading a
page when a required field is missing or a value fails to locate — until its **deterministic** termination check
passes: the schema validates and every required field is either located or explicitly marked unlocated. Bounded at
3 iterations and by the shared deadline.

**`evidence_retriever`** — a ReAct worker. Tools: `search(query)`, `rerank(candidates)`. It loops — reformulating
the query when nothing clears the relevance floor — until it holds *k* reranked chunks above threshold, or has
tried twice. Deterministic termination, same as above.

**`answer`** — Week 1's `verify_and_render`, extended to render evidence lines alongside chart lines. "Extended"
undersells it; see below.

### The answer node is the largest code change

`verify.py` gates every citation through a `ResourceType/id` regex against an index derived from `PatientContext` —
a closed union over the FHIR records the server already holds. Evidence chunks and document fields are neither, so
**today's verifier would silently withhold every Week 2 citation as `MALFORMED_ID`**. Opening that union touches
roughly five files and is the single biggest code change in this build, not a rendering tweak. Scheduled
accordingly in [§9](#9-build-order).

### When a worker gives up

An earlier draft left these paths unwritten. Each exhaustion is an *outcome*, not an exception:

| Path | Result |
|---|---|
| `intake_extractor` hits 3 iterations | Return the partial extraction with unresolved fields marked `unlocated`; supervisor routes to `answer`, which renders what is grounded and states what is missing |
| `evidence_retriever` fails twice | Return zero chunks; the answer carries chart facts only and says no guideline evidence cleared the floor |
| Deadline expires mid-graph | Return held state with the handoff log showing where it stopped — a partial grounded answer beats a timeout |

`Outcome` at `schemas.py:192` is `{pass, pass_with_removals, fail, refused, clarify}` and has no member for this.
It gains **`partial`** — a Week 1 enum change of the same class as the `verify.py` union above, and budgeted with
it rather than discovered during integration.

None of these are silent. All three are eval cases.

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

`reason` is the closed enum code, never prose. Logged, traced, **and returned in the API response** — so the
supervisor's routing is inspectable from outside without opening Langfuse. That is the direct answer to the PRD's warning: *"Letting the supervisor become a black
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

**Ingestion needs its own budget.** `config.py:32` sets a 9-second question budget, which cannot contain a vision
call over a multi-page scan. `attach_and_extract` runs on its own longer deadline as an upload operation; the
9-second budget continues to govern the question path, which reads already-extracted facts.

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
| Extracted facts | `copilot_staged_fact` (agent-owned, its own DDL and its own grant) | None — staging is not the chart |
| Approved facts | OpenEMR allergy / medication / problem records | **Clinician approval in the panel** |
| Lab values | Staging only, cited against the document | No API exists (documented) |

Staged rows carry the citation, extraction confidence, status, and who approved when. Approval is an audited
event. Rejection is too — a rejected extraction is a training signal and an eval case.

**Not "beside `copilot_audit`".** `deploy/sql/copilot_audit.sql:31` reads *"Agent writer: INSERT only on this one
table, TLS required. No SELECT, UPDATE or DELETE."* Staging needs SELECT to render the queue and UPDATE to approve,
and unlike the append-only audit log it holds clinical values and clears rows on resolution rather than retaining
them for six years. Its own table, its own DDL, its own grant file. MySQL grants are per-table, so the connection
machinery is reused — a second grant, not a second user.

### The requirement, quoted in full

Requirement 1: *"It must store the source document in OpenEMR, return strict-schema JSON, and **persist derived
facts as appropriate FHIR resources or OpenEMR records**."* Three MUSTs — an earlier draft of this spec quoted that
sentence with "as appropriate FHIR resources or" silently dropped. Staging alone satisfies two and defers the
third behind a click, which means that in the default state a grader exercises — upload, extract, read the JSON —
nothing has been persisted as an OpenEMR record at all.

So the write is not merely *available*, it is **demonstrated**:

- **A mandatory demo beat.** Approve one intake-form penicillin allergy, then re-read
  **`GET /api/patient/:puuid/allergy`** on camera showing the new record with `document_id`, page and `field_path`
  in its `comments` — a whitelisted insert field (`AllergyIntoleranceRestController.php` `WHITELISTED_FIELDS`), so
  provenance has somewhere real to live.

  **Read back through the standard API, not FHIR.** `FhirAllergyIntoleranceService.php` contains no `comments`,
  note or annotation mapping at all, so `GET /fhir/AllergyIntolerance` would render the record without its
  provenance — a demo beat pointed at a field that does not appear. The whitelist governs the standard-API write
  and the standard-API read; those are the two ends that match.
- **Two blocking eval cases**: approve-then-read-back, and reject-then-confirm-absent. These run at the
  `FakeClaude` seam against `httpx.MockTransport`, so they assert **route, payload and provenance fields** — they
  do not prove OpenEMR accepts the write. That is exactly the risk §6's auth note flags (role ACL, pid vs puuid),
  so it is covered by the demo beat plus a separately-run integration check, not by the gate.

Twenty seconds of video converts staging from *an excuse for not writing* into *a control on writing* — the same
architecture read the opposite way. The property that makes it trustworthy is unchanged: **a vision model reading a
smudged scan cannot alter a chart on its own.** Today's *Advanced Graphs* lecture covers human-in-the-loop nodes;
this is one.

Lab values stay staged-only, cited against the document, because no write route exists — an upstream limitation
backed by a test rather than by a route survey. If the gate is green by Thursday, the stretch is round-tripping the
intake form's vitals through `POST /api/patient/:pid/encounter/:eid/vital`, which reads back out through
`FhirObservationVitalsService` and would answer the *"FHIR resources or"* clause directly instead of conceding it.

### Auth change — larger than "re-register the client"

`smart.py:196` rejects any scope outside a fixed allowlist, commented `# write scopes, offline_access, anything
unrequested`, and `test_smart_sessions.py` asserts it under the docstring *"Guards: a session holding a token that
can write to the chart or outlive the visit."* Widening `_ALLOWED` is **deleting a Week 1 security invariant and
its test**, not editing a config. Note too that §3 step 1 uploads the document *before* any approval, so write
scope is needed on the ingest path, not only the approval path.

**What actually has to change.** `_ALLOWED` is keyed per `Kind` and today holds **only FHIR read scopes** —
`user/Patient.rs`, `user/AllergyIntolerance.rs` and so on (`smart.py:22-26`). The four routes §3 needs are not FHIR
at all: they live on OpenEMR's **standard REST API**, which is a different scope class. So this is not "add two
write scopes" — it is the `api:oemr` API grant *plus* per-resource write scopes for document, allergy, medication
and medical_problem, added to the `patient` and `api` kinds. The exact scope strings this fork accepts are
confirmed in the 90-minute spike below before any of it is written down as fact.

**Decision: widen the physician's own session, by the minimum set above.** The deciding argument is attribution.
If the physician's token writes, OpenEMR's audit log says the physician did it and OpenEMR's role ACL still
applies — which is what makes "a clinician approved this" mean anything at all. A service account writing
*approved* facts records that the robot did it, and that is the worse audit story. (A second client would also
invalidate the eval tooling file, the loadtest and both Bruno environments, since OpenEMR tokens and introspection
are per-client.)

The Summary claim is rewritten to match, and narrowed to what is actually true: the session **writes a faithful
copy ungated and writes derived clinical facts only on approval** — the document upload at §3 step 1 happens before
any review, and the copy-is-not-a-claim argument is what licenses it. **The model cannot write at all.** A comment
at `smart.py:196` records why the allowlist widened.

**Do this first, timeboxed to 90 minutes.** Two things will bite. The document route is gated on OpenEMR's **role
ACL** as well as OAuth scope — `request_authorization_check($request, "patients", "docs", ['write','addonly'])` —
so demo users need `patients/docs` write granted in the admin UI on **both** local and Railway. And document and
medication routes take the **numeric pid** while allergy and problem take the **puuid**; the session holds a uuid
and nothing translates today. If it is not working at 90 minutes, fall back to the agent's own store and document
it exactly as the lab-write limitation is documented.

---

## 7. The eval gate

The hard gate, stated twice in the PRD: *graders will introduce a regression and confirm the build fails.*

### Structure: all fifty block

Evals Part 1 slide 13 distinguishes golden sets (10–20, all must pass, every commit) from labeled scenarios
(30–100+, coverage, every release). An earlier draft split the PRD's fifty along that line: ~15 blocking, ~35
reporting.

**That split is abandoned.** The distinction exists because live golden sets flake and cost money — recorded replay
removes both, so the reason to keep the blocking set small does not apply here. Golden and scenario survive as
**tags** on each case and as a line in the summary output, not as gating. Three consequences, all good:

1. The PRD's wording — *"Build a 50-case golden set and a PR-blocking Git Hook"* — is met literally, with no
   argument to win against a grader who is not in the room.
2. The 5 % threshold becomes statistically real: per-category buckets go from ~3 cases to ~10.
3. A grader's planted regression cannot land in a bucket that only reports.

One line belongs in `W2_ARCHITECTURE.md`, to make this a stated choice rather than a hole:

> *Recorded replay is what lets all fifty block; the golden/scenario split survives as tagging, not as gating. At
> these bucket sizes 5 % is below the resolution of a single case, so in practice the gate fails on any case
> flipping — deliberately stricter than the PRD requires.*

The fifteen cases tagged `golden` must still cover the PRD's Stage 4 list explicitly — extraction, evidence
retrieval, citations, refusals, missing-data — across **both** `lab_pdf` and `intake_form`. That coverage was
always the real point of the split; the exposure was never the existence of the 35 but a regression landing in an
intake-only or degraded-scan path that nothing blocking touched.

**Holdout:** ten cases beyond the fifty, at `evals/w2/holdout/`, never tuned against, run manually before each
submission with results committed — *"the moment you optimise against a set, it stops measuring quality and starts
measuring memorisation."* Specified here rather than named in passing, because a named artifact that does not exist
costs more than an explicitly deferred one.

### The rubric categories — the PRD's five, plus one

All boolean, never 1–5. Five need no judgement at all:

| Category | Grader | Rung |
|---|---|---|
| `schema_valid` | Pydantic validates, or does not | 1 — assertion |
| `citation_present` | Every rendered clinical line carries citation metadata | 2 — invariant |
| `safe_refusal` | Outcome enum is `refused` | 1 — assertion |
| `no_phi_in_logs` | Scan captured logs for fixture identifiers | 2 — invariant |
| **`value_located`** | **Boolean per case: on a clean scan, every required field resolved to a `bbox`** | **2 — invariant** |
| `factually_consistent` | Binary judge, per claim against source | 4 — judge |

*"Climb only as high as you must. Every rung up costs money, latency and trust."*

**Why the sixth exists.** §4 makes `bbox = None` a legitimate, renderable citation — the right design, and it opens
a hole in the gate. `citation_present` asks only that a line *carries citation metadata*, and an unlocated citation
carries it. So a regression that breaks `locate.py` entirely degrades every citation to unlocated, renders the
PRD-required overlay empty, and **leaves all five original categories green.** Location is the only subsystem whose
total failure is, by design, an accepted output rather than an error, so it needs a check of its own. Like every
other row it is **boolean per case** — the rate across cases is what `GATE` thresholds, so there is exactly one
floor, not two. It is asserted only on cases tagged `clean_scan`; the deliberately degraded scans are excluded from
the denominator rather than dragging it, because on those an unlocated value is the *correct* output.

### Trajectory graders, not only output graders

*"You are grading a path, not a string."* Beyond the five, each case asserts on the trajectory: which nodes ran and
in what order, how many worker iterations, whether verification ran before the answer was returned. A case can
return a correct answer by the wrong route and still fail — the "passed for the wrong reason" state.

### Attempts measured separately from successes

*"A fence is not a fix."* Week 1's `withheld` counter is already the attempt rate: how often the model cites
something that is not there. Reported next to the block rate, because a guardrail that stops damage without
changing behaviour is not a fix — and removing it would bring the behaviour back intact.

### Recorded responses — at the seam that already exists

Real Claude output captured once and committed; CI replays it. This is Evals Part 1 Stage 3 — *"Record once. Score
anytime."*

Three reasons, in order of weight:

1. **Statistics.** Slide 34: at n=10, a 90 % pass rate carries ±19 points. Per-category these are ~10-case buckets.
   A 5 % regression threshold on live runs is **inside the noise band**. Recorded replay removes sampling variance,
   which is what makes the PRD's threshold meaningful rather than theatre.
2. **A gate that costs money and flakes gets disabled**, and then it is not a gate.
3. CI needs no secrets.

**This is not a new layer to build.** `agent/tests/` already runs 185 tests end to end through `main.py` against
`FakeClaude`, with no network and no secrets, injected via `main.app.state.llm`. `FakeClaude.create(**kwargs)`
receives the exact dict `_call()` assembles at `llm.py:159-167` — `model`, `system`, `messages`, `tools`,
`output_config`. Every property this section argues for is already standing there. **Recorded replay is better
fixtures for the layer we have, not a cassette system to write from scratch.**

(`evals/run_evals.py` is *not* the base. It reads `../tools/local-edge-patients.json` at module scope — a file its
own docstring says is not in the repo — shells out to `docker exec`, needs a live OpenEMR container and real
Anthropic spend, and raises on import in CI before a single case runs.)

### The keying rule — what actually makes the gate go red

The load-bearing detail, and the one an earlier draft left unstated. Replay pins the model's *response*, so
everything downstream — parsing, schema validation, verification, rendering, the rules engine, the scorer — still
executes live in CI and is fully covered. The blind spot is anything that manifests only *through* model output:
the prompt text, the model id, the tool definitions, the schema handed to the vision call.

Recordings are therefore keyed on a **hash of the model-facing surface** — model id, system prompt, tool
definitions, output schema. Not the case id, and deliberately **not the whole request**: hashing everything would
let a fixture tweak or a `render.py:_compact()` adjustment invalidate every recording and redden CI for no reason,
which is unbearable inside a five-day sprint. Case content is keyed separately.

Then the rule that closes the hole:

> **A cache miss is a hard case failure.** Never a live call — CI has no key — and never a silent pass. The failure
> names the case and says to re-record deliberately.

A grader who edits a prompt now produces a cache miss on every affected case, and the build goes red without anyone
having had to anticipate their specific edit.

**One committed fixture holds a known-bad recorded response**, and its only job is to prove the runner goes red.
It lives at `evals/w2/selftest/` and is reached only by `run_gate.py --selftest`, which **inverts the verdict — the
self-test passes only if that case fails.** It is excluded from the fifty and from every published rate, because a
permanently-failing case inside the main suite would either redden the build forever or have to be silently
skipped, and a silently skipped case is exactly the thing this fixture exists to disprove. That meta-case is the
most valuable artifact in the build: it is what gets pointed at in the demo video and in the interview. Re-recording is a deliberate command, and the diff in the recordings is itself reviewable.

### Judge calibration

Before the judge is trusted: score 20 cases by hand and run the judge on the same ones. *"A judge with a bad rubric
produces confident, wrong scores."* Committed as `evals/w2/judge_calibration.json`, doubling as the PRD's required
"judge configuration".

**The statistic is agreement, not correlation.** `factually_consistent` is boolean, and a correlation coefficient
on binary data is both awkward to interpret and unstable at n=20. The gate is **≥ 0.8 raw agreement, with Cohen's
κ reported alongside** — κ because agreement alone flatters a judge on an unbalanced set, where most cases are
consistent and always answering "yes" scores well. Per-class recall is recorded too: missing a *false* is the
failure that matters, because that is the judge waving through an ungrounded claim.

### Thresholds

```python
GATE = {
    "schema_valid":         0.95,
    "citation_present":     1.00,   # safety-shaped
    "factually_consistent": 0.90,
    "safe_refusal":         1.00,   # safety-shaped
    "no_phi_in_logs":       1.00,   # safety-shaped
    "value_located":        0.90,   # clean-scan cases only
}
MAX_REGRESSION = 0.05
BASELINE = "evals/w2/baseline.json"   # written only under --rebaseline
```

Build fails if any category drops below its floor **or** regresses more than five points against the committed
baseline. *"A suite that blocks nothing is a dashboard."* Safety-shaped checks get a floor of 1.00.

**The baseline is one named file, not "the latest results".** A comparator that diffs against the most recent run
fails once and then heals itself — worse than no gate, because it looks like it works. `baseline.json` is written
only when `--rebaseline` is passed, and that diff gets reviewed like any other.

Failure output names the category, the baseline, the new rate, and which cases flipped — so a red build is
actionable rather than a red X.

### Blocking where the grader actually is

An earlier draft said "a hook plus CI" and assumed that covered it. It did not, and this is the finding that would
have cost the week.

**`origin` carries two push URLs**, one of them `ssh://git@labs.gauntletai.com:22022/…`, and the PRD's submission
row is *"GitLab Repository"*. The repo has `.github/workflows/copilot-agent-tests.yml` and **no `.gitlab-ci.yml`,
no `.githooks/` directory, and `core.hooksPath` unset.** On the artifact a grader receives, nothing runs at all.

Three pieces, in this order:

1. **One command in the README's Week 2 section**, impossible to miss: `python evals/w2/run_gate.py`. A grader
   cannot push to this namespace — they will break something locally and look for how to run the checks. This is
   the highest-value paragraph in the build.
2. **`.gitlab-ci.yml`** running that same script, **verified by pushing a deliberately broken branch and watching
   it go red.** A pipeline stuck perpetually *pending* reads worse than no pipeline. If `labs.gauntletai.com` has
   no runners available to student projects, the README command becomes authoritative and `W2_ARCHITECTURE.md`
   states which gate is which.
3. **A tracked `.githooks/pre-push`**, with `git config core.hooksPath .githooks` in the setup guide. The PRD says
   "PR-blocking Git Hook"; a local hook can be skipped with a flag, which is why it is third rather than first.

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
| Routing counterfactual | Per supervisor call: the enum the model chose, the enum the deterministic policy would have chosen, and whether they differed — the rate §2 reports in `KEY_METRICS.md` |

**No raw PHI** — the same discipline, now covering document text, extracted values and page images, which the PRD
explicitly calls sensitive. Page renders are never sent to Langfuse; only counts and coordinates.

One boundary stated rather than left implicit: `render.py:_compact()` output *is* model-facing and is not covered
by the recording key (§7), which keys on the prompt, model, tools and schema. That is deliberate — it keeps a
fixture edit from invalidating every recording — and it means changes to `_compact()` are covered by Week 1's own
render tests instead. Worth knowing before assuming the key covers everything the model sees.

---

## 9. Build order

Components before orchestration — but **the gate before both.** An earlier draft put the eval suite at step 7,
downstream of `graph.py`, which is itself downstream of this spec's own second-largest risk. The most heavily
graded element in the assignment cannot sit behind the thing most likely to eat the schedule.

**Early submission Wednesday 23 September, 11:59 PM Central. Final Sunday 27 September, noon.**

| # | Step | Why here |
|---|---|---|
| 0 | **Auth spike**, timeboxed 90 min (§6) | Everything downstream depends on it, and it can fail in a way no code fixes |
| 1 | **The gate**, at the `FakeClaude` seam — hash keying, miss-fails-loudly, `baseline.json`, the known-bad fixture | The PRD's Wednesday checkpoint asks for *"eval framework in place"*, not fifty cases. Built here it is demonstrable Wednesday and accumulates cases for free |
| 2 | **`.gitlab-ci.yml` + the README command**, verified by a deliberately broken branch going red | Half an hour, and the difference between passing and not passing |
| 3 | **Schemas + validation tests** | The contract before anything that produces it |
| 4 | **`documents.py` + `locate.py` + `emr_write.py`** | Upload, fetch, render, word coordinates. Testable with no model. Tesseract's apt layer lands in this commit (§10) |
| 5 | **`extract.py`** | Vision against the schemas, tested against recordings |
| 6 | **`retrieve.py`** | Corpus, index, rerank. Testable with no graph |
| 7 | **`verify.py` citation union + `staging.py` + approval endpoints** | The verifier change is the big one (§2) and gates everything the answer node renders |
| 8 | **`graph.py`** | Wire working components together |
| 9 | **UI**: upload control, **bbox overlay**, review queue | The overlay is PRD-required, not polish |
| 10 | **Docs**: `W2_ARCHITECTURE.md`, `KEY_METRICS.md`, `W2_COST_AND_LATENCY.md`, README Week 1 / Week 2 split | `KEY_METRICS.md` is its own stated Hard Gate |
| 11 | **Deploy**, then **record the demo video** — including the deliberate-regression take | Both are Early Submission rows. Neither appeared anywhere in an earlier draft of this spec |
| 12 | **Book the technical interview slot** | An *action*, not a document, and it has to happen before Wednesday rather than after |
| 13 | **AI interview** (24h after submission) and **social post** (final submission only) | Named rows on the PRD's table; listed so they are not discovered on Sunday morning |

### `W2_COST_AND_LATENCY.md` — what goes in it

A filename with no contents is a row that fails. The PRD names four things, so the document has four sections:
**actual dev spend** (from Week 1's `llm.py` accounting, now covering vision and embedding calls, against the $30
cap), **projected production cost** per encounter and per clinician-day, **p50/p95 latency** per node from the
Langfuse spans, and **bottleneck analysis** — which for this build is almost certainly the vision call, and saying
so with the number is the point.

**Hard stop 9 PM Wednesday: whatever runs is what gets filmed.** Rehearse the regression take separately — break
one renderer line, show the build go red, revert.

### Where the cut line goes

Not by step number — the earlier spec's "cut after step 6" discarded the eval gate, the PRD-required bounding-box
overlay and both hard-gate documents, which was the exact opposite of what its own second half argued for. The
list below carries the argument instead.

**Above the line, always:** the gate and its CI, **all fifty cases**, the overlay, `KEY_METRICS.md`,
`W2_ARCHITECTURE.md`, `W2_COST_AND_LATENCY.md`, the README Week 1 / Week 2 split, a deployed app, the video.

The fifty cases belong above the line, which an earlier version of this paragraph got wrong by listing "scenario
cases beyond the first fifteen" as the first thing to cut. That directly contradicts §7: cut them and the build
ships fifteen cases against a requirement for fifty, and the ~10-case buckets the 5 % threshold depends on collapse
back to ~3. Because they are recorded replay, the cases cost **authoring time only** — no runtime, no money, no
flake — so they are the wrong thing to trade away under deadline pressure.

**Below the line, in this order:** retrieval polish (query rewriting, better chunking), the review-queue UI beyond
a functional list, the vitals FHIR round-trip, the critic agent.

---

## 10. Risks

**The scan is genuinely bad.** OCR misreads, values fail to locate. Mitigated by design — an unlocated value is
surfaced as unlocated, never silently dropped or approximately placed. Eval cases cover a deliberately degraded
scan.

**Tesseract works locally and fails on Railway.** `agent/Dockerfile` is `FROM python:3.12-slim` with exactly one
`pip install` layer and no `apt-get`. Tesseract is an OS package, not a Python one — `pytesseract` only shells out
to the binary. So it imports cleanly and fails at call time: OCR works in the demo video via Homebrew and returns
unlocated-everything in the app the grader opens, which is the worst failure shape available. The
`apt-get install -y --no-install-recommends tesseract-ocr` layer lands in the same commit as `locate.py`, and the
deploy is exercised once against the deliberately degraded fixture before anything is filmed.

**LangGraph re-plumbing eats the schedule.** Mitigated by thin nodes: the graph owns control flow only, every
module keeps working as it does now. If it turns hostile, the fallback is a hand-rolled graph behind the same node
interfaces — a few hours, not a rewrite.

**The auth change breaks the Week 1 launch.** Mitigated by widening the *existing* client's registered scopes —
not registering a second client, which §6 rejects on attribution grounds and which would invalidate the eval
tooling file, the loadtest and both Bruno environments — then verifying a SMART launch on the same client id before
anything else depends on it. Week 1's lesson: OpenEMR's introspection accepts any
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
- **The vitals FHIR round-trip.** Real and attractive (§6), but Thursday work at the earliest — only once the gate
  is green.
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
