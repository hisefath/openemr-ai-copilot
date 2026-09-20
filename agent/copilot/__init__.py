"""Clinical Co-Pilot agent: a read-only SMART on FHIR assistant for OpenEMR.

The package is the whole shippable unit — HTTP API, SMART launch, FHIR client, LLM planning, deterministic
verification, rules engine, audit trail, observability and the alert evaluator. It talks to OpenEMR over FHIR and
to MySQL for the audit table; it imports nothing from the surrounding OpenEMR fork, so it deploys on its own
(`pip install ./agent`, or the image built from agent/Dockerfile).

Entry points:
    copilot.main:app        the FastAPI application (uvicorn copilot.main:app)
    copilot.alerts:main     the ALERTS.md evaluator, run on a 5-minute cron (python -m copilot.alerts)
"""

__version__ = "1.0.0"
