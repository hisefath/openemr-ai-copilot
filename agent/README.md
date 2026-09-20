# Clinical Co-Pilot agent

The AI half of the project: a read-only SMART on FHIR assistant that answers a physician's questions about the
patient already open in OpenEMR, with every sentence traced back to a record it actually read.

It is a **self-contained Python distribution**. It imports nothing from the OpenEMR fork it lives in — it talks to
OpenEMR over FHIR like any other SMART app — so it installs, tests and deploys on its own:

```bash
pip install ./agent                       # or: pip install -e './agent[dev]'
uvicorn copilot.main:app --port 8000
```

```bash
docker build -t copilot ./agent && docker run -p 8000:8000 --env-file agent/.env copilot
```

Configuration is environment-only; see [`.env.example`](.env.example). Full setup, including the OpenEMR side, is in
the [repository README](../README.md).

## Layout

```
agent/
  pyproject.toml     the distribution: name, pins, console script, pytest config
  requirements.txt   the same runtime pins, duplicated so Docker installs them in a cacheable layer
  Dockerfile         production image        Dockerfile.dev  test image (source mounted)
  copilot/           everything importable
  tests/             offline test suite (no network, no LLM, no database)
```

### Inside `copilot/`

One module per boundary the agent crosses or per decision it has to defend. Flat, not nested: fifteen modules is
small enough that a subpackage tree would add lookup cost without removing any coupling.

| Module | Responsibility | Why it is on its own |
|---|---|---|
| `main.py` | FastAPI app: routes, request lifecycle, deadline, orchestration | The only module that knows about HTTP |
| `config.py` | Settings parsed from the environment once, at import | Config errors surface at boot, not mid-question |
| `smart.py` | SMART on FHIR launch, OAuth2 + PKCE, token exchange | Auth is a security boundary; it is reviewed and tested alone |
| `sessions.py` | Session store: handle hashing, idle TTL, eviction, history | Holds the only server-side state; swapping it for Redis is a one-file change (ARCHITECTURE §11) |
| `fhir.py` | The FHIR client: which resources, what concurrency, what errors | The one place that talks to OpenEMR — and the one place tool failures are counted |
| `normalize.py` | Raw FHIR → typed records (codes, units, dates, statuses) | Pure functions over real OpenEMR payloads; the most heavily fixture-tested code here |
| `rules.py` | The deterministic clinical rules (interaction, allergy, renal dosing) | Clinical logic must never be the model's job — it is code, and reviewable as code |
| `llm.py` | Claude call: prompt, structured `AnswerPlan`, retries, cost/usage | Isolating it means the rest of the agent is testable with no API key |
| `verify.py` | Checks every planned line against the fetched records | The safety gate; a bug here is the worst bug in the system |
| `render.py` | Server-side rendering of verified lines into the sentences shown | The model never emits display text — this module does |
| `schemas.py` | Pydantic models for every boundary (API, LLM, audit) | One shared vocabulary, so a shape change breaks at the type level |
| `audit.py` | Append-only audit rows in MySQL | A compliance surface, kept apart from application logging |
| `observability.py` | Langfuse tracing, structured logs, PHI scrubbing | Log redaction has to hold everywhere, so it lives in one filter |
| `deadline.py` | The 9-second question budget, shared by every await | Small enough to read in one sitting, important enough to not inline |
| `alerts.py` | The ALERTS.md evaluator, run on a 5-minute cron | Ships in the same image but is a separate process (`python -m copilot.alerts`) |

### Tests

`tests/` is the **offline** tier: no network, no Anthropic key, no database. OpenEMR is mocked at the HTTP layer
with *real* FHIR payloads captured from the seeded demo patients (`tests/fixtures/`), and Claude is a fake that
returns answer plans. CI runs it on every push; it finishes in seconds and gates the deploy.

The **live** tier is [`evals/`](../evals) at the repository root — real Claude, real OpenEMR, real tokens, run by
hand because it costs money and needs a running stack. It lives outside `agent/` precisely because it is not part
of the shippable unit. [`evals/README.md`](../evals/README.md) explains the case taxonomy and the scoring.

See [TESTING.md](../TESTING.md) for what each tier guards against and when to add to which.
