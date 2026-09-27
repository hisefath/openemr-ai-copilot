"""Backend-for-frontend for the React patient dashboard (PATIENT_DASHBOARD_MIGRATION.md).

WHY A BFF AND NOT A PURE SPA. OpenEMR refuses `user/` scopes to public OAuth clients outright —
AuthorizationController.php:324-329 raises `invalid_client_metadata` for any public client whose scope string
contains `user/` or `system/`. A clinician-facing dashboard needs `user/` scopes, so it needs a CONFIDENTIAL
client, so it needs a server to hold the secret. The browser can never hold this token. That is not a
workaround; it is also what the current IETF best practice for browser-based apps recommends.

So the access token stays here, exactly where the Week 1 agent already keeps it, and the browser gets a
session handle it can do nothing else with. No new auth code was written for the dashboard: it reuses
smart.py's SMART-on-FHIR flow, sessions.py's store, and fhir.py's client — the same path the eval gate
already covers, and the same path whose missing write scopes CI caught.

These routes are READ-ONLY and additive. Nothing here writes to OpenEMR; the dashboard is a presentation
layer over the existing API, which is the whole point of the exercise.
"""
from __future__ import annotations

import logging
from datetime import date
from typing import Any, Callable, Dict, List, Optional

from fastapi import APIRouter, HTTPException, Request

from . import observability as obs
from .deadline import Deadline

log = logging.getLogger("agent")

router = APIRouter(prefix="/api/dashboard", tags=["dashboard"])

CARD_DEADLINE_S = 8.0   # a card is a page element, not the 90 s ingest path; fail it fast and let the UI retry

# One FHIR search per card. Kept as data rather than code so the route list and the UI's card list cannot
# drift apart — the dashboard fetches /cards/{key} and this dict is the only place a key is defined.
CARDS: Dict[str, Callable[[str], tuple]] = {
    "allergies":     lambda pid: ("AllergyIntolerance", {"patient": pid}),
    "problems":      lambda pid: ("Condition", {"patient": pid}),
    "medications":   lambda pid: ("MedicationRequest", {"patient": pid}),
    "prescriptions": lambda pid: ("MedicationRequest", {"patient": pid, "intent": "order"}),
    "careteam":      lambda pid: ("CareTeam", {"patient": pid}),
    "vitals":        lambda pid: ("Observation", {"patient": pid, "category": "vital-signs"}),
}


def _session(request: Request, authorization: Optional[str]):
    """Reuses main.py's session check verbatim — one auth path for the whole app, not a second one here."""
    resolver = request.app.state.session_resolver
    # kind="patient": the dashboard is a chart-context app, so a schedule-scan session must not open it.
    return resolver(request, authorization, "patient")


def _entries(bundle: Any) -> List[dict]:
    """A FHIR searchset Bundle -> its resources. A missing or malformed bundle is an empty card, never a 500:
    one unavailable resource must not take the whole dashboard down with it."""
    if not isinstance(bundle, dict):
        return []
    return [e["resource"] for e in bundle.get("entry", []) or [] if isinstance(e, dict) and "resource" in e]


@router.get("/patient")
async def patient(request: Request, authorization: Optional[str] = None):
    """The persistent identity bar: name, birth date, sex, MRN, active status."""
    session = _session(request, authorization or request.headers.get("authorization"))
    app = request.app.state
    deadline = Deadline(CARD_DEADLINE_S)
    fetch = await app.fhir.get(
        f"Patient/{session.patient_id}", None, token=session.access_token, deadline=deadline,
        correlation_id=obs.correlation_id.get(), on_event=lambda *_a, **_k: None,
        patient_id=session.patient_id)
    # fhir.py wraps a read as a one-entry searchset, so the Patient comes back through the same unwrap
    # as every card rather than needing a second code path.
    found = _entries(fetch.bundle)
    if fetch.status.value != "ok" or not found:
        raise HTTPException(503, f"patient_unavailable: {fetch.status.value}")
    return found[0]


@router.get("/cards/{key}")
async def card(request: Request, key: str, authorization: Optional[str] = None):
    """One card's FHIR search. Each card is its own request on purpose — see the migration doc: the PHP page
    rendered every panel in one blocking pass, so the slowest query set the whole page's latency and one
    failing query could empty the screen. Independent requests make a slow Care Team a slow Care Team."""
    if key not in CARDS:
        raise HTTPException(404, f"unknown_card: {key}")
    session = _session(request, authorization or request.headers.get("authorization"))
    app = request.app.state
    resource, params = CARDS[key](session.patient_id)
    fetch = await app.fhir.get(
        resource, params, token=session.access_token, deadline=Deadline(CARD_DEADLINE_S),
        correlation_id=obs.correlation_id.get(), on_event=lambda *_a, **_k: None,
        patient_id=session.patient_id)
    # A card reports its own status rather than raising: the dashboard shows five good cards and one that says
    # "unavailable", which is strictly more useful than an error page. Same principle as the unlocated value.
    return {"card": key, "status": fetch.status.value, "resources": _entries(fetch.bundle)}
