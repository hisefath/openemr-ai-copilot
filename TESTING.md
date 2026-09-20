# Testing

What is tested, at which tier, and why each tier exists. Written because the repository is a **fork of OpenEMR**:
some test machinery here is OpenEMR's and some is this project's, and telling them apart is the first question
anyone reading the tree asks.

## What is this project's, and what came with the fork

| | This project | OpenEMR upstream |
|---|---|---|
| Tests | `agent/tests/`, `evals/`, `loadtest/` | `tests/` (PHPUnit), `docker/dockerhub/tests/` |
| CI | `.github/workflows/copilot-agent-tests.yml` | every other workflow in `.github/workflows/` |
| Docs | the root `*.md` files listed in [README](README.md#documents) | `CONTRIBUTING.md`, `API_README.md`, `FHIR_README.md`, `DOCKER_README.md`, `CODE_OF_CONDUCT.md`, `CHANGELOG.md`, `Documentation/` |

**The sanity test and the golden test are OpenEMR's, not this project's.** `docker/dockerhub/tests/sanity.sh` and
`docker/dockerhub/tests/golden-test.sh` guard OpenEMR's Docker Hub README rendering: Tier 1 (sanity) asserts the
rendered README still has the expected structure, Tier 2 (golden) diffs it against a checked-in
`docker/dockerhub/tests/golden.md` so any wording change is deliberate. They are upstream's, they are untouched, and
they have nothing to do with the agent. The agent's equivalent of a golden file is the fixture corpus below — real
OpenEMR FHIR payloads, checked in, diffed by assertion rather than by text.

The fork is not stripped down. OpenEMR's own suites are left exactly as they were, because the project's premise is
integrating into working infrastructure rather than rebuilding it (AUDIT.md) — and because a fork whose tests you
quietly deleted is a fork whose behaviour you can no longer vouch for.

## The four tiers

| Tier | Where | Runs | Cost | Gates |
|---|---|---|---|---|
| 1 · Offline | `agent/tests/` (184 test functions, 225 cases) | every push, CI | free, seconds | the deploy |
| 2 · Live evals | `evals/` (32 cases) | by hand before a submission | ~$0.20/run | the release |
| 3 · Load | `loadtest/` | by hand, before a submission | ~$0.50/run | the capacity claim |
| 4 · Alert proof | `evals/fault_injection.py` | once per alert definition change | ~$0.15/run | the monitoring claim |

### Tier 1 — offline (`agent/tests/`)

No network, no Anthropic key, no database. OpenEMR is mocked at the HTTP layer with **real FHIR payloads captured
from the seeded demo patients** (`agent/tests/fixtures/`), and Claude is a fake returning answer plans. That is
deliberate: the interesting bugs in this system are in how real OpenEMR output is normalized and verified, not in
whether `httpx` works, so the fixtures are real and the model is fake.

| File | Guards |
|---|---|
| `test_normalize.py` | The 8 data-quality traps in AUDIT.md — an uncoded allergy reading as "Unknown", a completed prescription still looking active, units that silently disagree |
| `test_rules.py` | Every clinical rule, including the ones that must *not* fire |
| `test_render_verify.py` | The safety gate: a model line with no matching record never reaches the screen |
| `test_fhir.py` | FHIR client behaviour: pagination, partial failure, the tool-failure counter |
| `test_smart_sessions.py` | SMART launch, PKCE, patient lock, handle hashing, idle TTL, eviction |
| `test_main.py` | End to end through the app: session → prefetch → question → plan → verify → audit |
| `test_obs_audit_llm.py` | PHI never reaching logs or traces; audit rows appended for every answer |
| `test_ui_static.py` | The panel's markup and CSP: no inline script, no external asset |
| `test_alerts.py` | Each of the three ALERTS.md thresholds, firing and not firing |

Every test names the failure mode it guards against in its docstring. A test that cannot name one does not belong.

### Tier 2 — live evals (`evals/`)

Real Claude, real OpenEMR, real OAuth tokens, synthetic patients. This is the tier that can catch what mocking
cannot: the model returning a plausible sentence the verifier lets through, a real token expiring mid-session, a
real FHIR bundle shaped differently from the fixture.

32 cases in four categories — **boundary** (15: what the agent must refuse), **invariant** (9: what must always
hold, e.g. every line cites a record), **regression** (3: a bug that shipped once), **adversarial** (5: prompt
injection through chart free text). CI asserts every case declares a `failure_mode_guarded`; a case that guards
nothing is a case nobody will maintain. See [evals/README.md](evals/README.md) for scoring and the results format.

### Tier 3 — load (`loadtest/`)

10 and 50 concurrent physicians against the agent, with the real LLM in the loop, measuring p50/p95/p99 and error
rate against the 9-second question deadline. Results and method: [LOAD_TEST.md](LOAD_TEST.md).

### Tier 4 — alert proof (`evals/fault_injection.py`)

An alert that has never fired is a hypothesis. This starts a second agent with one fault injected, drives enough
traffic to cross the threshold, and then runs the real evaluator over exactly that window. Each of the three alerts
in [ALERTS.md](ALERTS.md) has been fired this way on purpose.

## Running them

```bash
cd agent && pip install -r requirements.txt -r requirements-dev.txt && python -m pytest tests   # tier 1
python evals/run_evals.py                                                                      # tier 2 (needs the local stack + a key)
sh tools/loadtest/run_local.sh 10 2 5m l10                                                     # tier 3
python evals/fault_injection.py A1                                                             # tier 4
```

OpenEMR's own suites are unchanged and documented in [CONTRIBUTING.md](CONTRIBUTING.md); `openemr-cmd unit-test` and
friends still work exactly as upstream intends.
