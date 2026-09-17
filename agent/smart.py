"""SMART on FHIR: EHR launch, standalone schedule launch, the shared callback and API sessions (ARCHITECTURE §2).
Failures raise ValidationFailed (the user, token or request is rejected: 401/403) or UpstreamUnavailable (OpenEMR or our
own configuration failed: 503, an error under §7). Both carry a fixed reason code: safe for logs and audit rows, never
token, user or patient data."""
import base64
import hashlib
import html
import re
import secrets
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Dict, FrozenSet, Literal, Optional, Tuple
from urllib.parse import urlencode

import httpx
from fastapi.responses import HTMLResponse

from config import Settings
from sessions import Session, SessionStore

SCOPES = ("openid", "fhirUser", "launch", "user/Patient.rs", "user/AllergyIntolerance.rs", "user/MedicationRequest.rs",
          "user/Condition.rs", "user/Observation.rs", "user/Encounter.rs", "user/Appointment.rs")
SCHEDULE_SCOPES = tuple(s for s in SCOPES if s != "launch")   # standalone: bound to the user only
API_SCOPES = SCOPES + ("launch/patient",)                     # grader token path: Bruno standalone launch with launch/patient
_ALLOWED = {"patient": frozenset(SCOPES), "schedule": frozenset(SCHEDULE_SCOPES), "api": frozenset(API_SCOPES)}

CALLBACK_PATH = "/smart/callback"   # the one registered redirect URI (§2 client registration); both flows return here
STATE_COOKIE = "__Host-copilot-state"
PAGE_PLACEHOLDER = "<!--COPILOT_SESSION_META-->"   # shared with static/panel.html, schedule.html (tests/test_ui_static.py)
OPENEMR_TIMEOUT = httpx.Timeout(3.0, connect=1.0)   # §3 per-call budget
STATE_TTL_S = 300
MAX_PENDING_STATES = 1000
LAUNCH_REPLAY_S = 3600
MAX_SEEN_LAUNCHES = 10_000
PATIENT_ID = re.compile(r"[0-9a-fA-F-]{36}")   # same shape as SessionCreateRequest.patient_id
FHIR_ID = re.compile(r"[A-Za-z0-9.-]{1,64}")

Kind = Literal["patient", "schedule", "api"]   # patient = EHR launch from the chart


class ValidationFailed(Exception):
    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


class UpstreamUnavailable(Exception):
    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


@dataclass(frozen=True)
class PendingAuth:
    verifier: str
    kind: Literal["patient", "schedule"]
    created_at: float


@dataclass(frozen=True)
class LaunchGrant:
    fhir_user: str              # relative reference, e.g. Practitioner/<uuid>
    patient_id: Optional[str]   # None for schedule sessions
    client_id: Optional[str]
    scopes: FrozenSet[str]
    expires_at: float           # epoch seconds


def _expire(d: Dict[str, Any], stamp: Callable[[Any], float], cutoff: float) -> None:
    """Drop entries stamped at or before cutoff (dicts keep insertion order, so the oldest come first)."""
    while d and stamp(next(iter(d.values()))) <= cutoff:
        del d[next(iter(d))]


class StateStore:
    """Pending authorizations (one-time state + PKCE verifier) and the launch replay set, both bounded."""

    def __init__(self, clock: Callable[[], float] = time.time):
        self.clock = clock
        self._pending: Dict[str, PendingAuth] = {}
        self._launches: Dict[str, float] = {}   # sha256(launch) -> first seen

    def begin(self, kind: Literal["patient", "schedule"], launch: Optional[str] = None) -> Tuple[str, str]:
        """Returns (state, S256 code_challenge). A launch reused within the hour is refused (§2; AUDIT ARCH-5: launch
        tokens never expire). Full stores refuse new launches instead of evicting live entries, so a flood can't drop
        callbacks in flight or make a used launch valid again."""
        now = self.clock()
        _expire(self._pending, lambda p: p.created_at, now - STATE_TTL_S)
        _expire(self._launches, lambda t: t, now - LAUNCH_REPLAY_S)
        key = hashlib.sha256(launch.encode()).hexdigest() if launch else None
        if key in self._launches:
            raise ValidationFailed("launch_replay")
        # ponytail: ~3 junk requests/s keeps a store full and blocks real launches; rate-limit /smart/launch and /schedule per IP
        if len(self._pending) >= MAX_PENDING_STATES or (key and len(self._launches) >= MAX_SEEN_LAUNCHES):
            raise UpstreamUnavailable("launch_capacity")
        if key:
            self._launches[key] = now
        state, verifier = secrets.token_urlsafe(32), secrets.token_urlsafe(64)
        self._pending[state] = PendingAuth(verifier, kind, now)
        return state, base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()

    def take(self, state: str) -> Optional[PendingAuth]:
        """One-time use: the state is consumed even when it turns out to be expired."""
        pending = self._pending.pop(state, None)
        return pending if pending and self.clock() - pending.created_at < STATE_TTL_S else None


def _client(settings: Settings) -> Tuple[str, str]:
    if not settings.client_id or not settings.client_secret:
        raise UpstreamUnavailable("client_not_configured")
    return settings.client_id, settings.client_secret


def _authorize_url(settings: Settings, states: StateStore, kind: Literal["patient", "schedule"],
                   launch: Optional[str] = None) -> Tuple[str, str]:
    client_id, _ = _client(settings)   # before begin(), so a misconfigured agent burns no launch
    state, challenge = states.begin(kind, launch)
    params = {"response_type": "code", "client_id": client_id, "redirect_uri": settings.agent_public_url + CALLBACK_PATH,
              "scope": " ".join(SCOPES if kind == "patient" else SCHEDULE_SCOPES), "state": state,
              "aud": settings.public_issuer, **({"launch": launch} if launch else {}),
              "code_challenge": challenge, "code_challenge_method": "S256"}
    return f"{settings.oauth_public_base}/authorize?{urlencode(params)}", state


def build_authorize_url(settings: Settings, states: StateStore, launch: str, iss: str, aud: str) -> str:
    """GET /smart/launch -> OpenEMR authorize. iss is checked before the launch is recorded, so a bad iss burns nothing."""
    if iss != settings.public_issuer or aud != iss:
        raise ValidationFailed("iss_mismatch")
    if not launch:
        raise ValidationFailed("launch_missing")
    return _authorize_url(settings, states, "patient", launch)[0]


def build_standalone_authorize_url(settings: Settings, states: StateStore) -> Tuple[str, str]:
    """GET /schedule -> (OpenEMR authorize URL, Set-Cookie value). UC5 standalone launch: no launch param or scope.
    The cookie binds the state to the top-level window that started it, so a callback link captured by someone else
    can't hand this browser their session (login CSRF). Lax still rides the top-level redirect back from OpenEMR;
    __Host- needs a secure context: https, or localhost in Chrome and Firefox."""
    url, state = _authorize_url(settings, states, "schedule")
    return url, f"{STATE_COOKIE}={state}; Max-Age={STATE_TTL_S}; Path=/; Secure; HttpOnly; SameSite=Lax"


async def _post_form(http: httpx.AsyncClient, url: str, name: str, data: Dict[str, str]) -> Dict[str, Any]:
    try:
        r = await http.post(url, data=data, headers={"Accept": "application/json"}, timeout=OPENEMR_TIMEOUT)
    except httpx.TimeoutException:
        raise UpstreamUnavailable(f"{name}_timeout") from None
    except httpx.TransportError:
        raise UpstreamUnavailable(f"{name}_unreachable") from None
    # Introspection answers 200 active=false for any bad token (RFC 7662); its 4xx only mean our client credentials
    # failed (TokenIntrospectionRestController.php:329-383), which is misconfiguration, not the user's fault.
    if r.status_code >= 500 or (r.status_code != 200 and name == "introspect"):
        raise UpstreamUnavailable(f"{name}_http_{r.status_code}")
    if r.status_code != 200:
        raise ValidationFailed(f"{name}_http_{r.status_code}")
    try:
        body = r.json()
    except ValueError:
        body = None
    if not isinstance(body, dict):
        raise UpstreamUnavailable(f"{name}_unparseable")
    return body


# Client credentials go in the form body: OpenEMR's introspection endpoint reads client_id/client_secret only from
# there and answers active=false without them (TokenIntrospectionRestController::validateInitialRequestParameters).
async def exchange_code(http: httpx.AsyncClient, settings: Settings, code: str, verifier: str) -> Dict[str, Any]:
    client_id, secret = _client(settings)
    return await _post_form(http, f"{settings.oauth_internal_base}/token", "token", {
        "grant_type": "authorization_code", "code": code, "redirect_uri": settings.agent_public_url + CALLBACK_PATH,
        "code_verifier": verifier, "client_id": client_id, "client_secret": secret})


async def introspect(http: httpx.AsyncClient, settings: Settings, token: str) -> Dict[str, Any]:
    client_id, secret = _client(settings)
    return await _post_form(http, f"{settings.oauth_internal_base}/introspect", "introspect", {
        "token": token, "token_type_hint": "access_token", "client_id": client_id, "client_secret": secret})


def _is_num(v: Any) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool)


def validate_launch_token(token_response: Dict[str, Any], introspection: Dict[str, Any], kind: Kind, now: float) -> LaunchGrant:
    """All callback checks of §2. kind='api' applies the same scope and fhirUser checks to a bare token (pass {})."""
    if introspection.get("active") is not True:
        raise ValidationFailed("inactive")
    scopes = frozenset(" ".join(str(x.get("scope") or "") for x in (token_response, introspection)).split())
    # OpenEMR appends a `nonce` pseudo-scope whenever its PHP session ever saw a nonce (ScopeRepository.php:173,
    # AuthorizationController.php:565 never clears it). It grants no data access.
    scopes -= {"nonce"}
    for s in sorted(scopes):
        if s.startswith("patient/"):   # patient/ scopes skip OpenEMR role ACL (AUDIT SEC-2)
            raise ValidationFailed("scope_patient")
        if s not in _ALLOWED[kind]:    # write scopes, offline_access, anything unrequested
            raise ValidationFailed("scope_not_allowed")
    if "refresh_token" in token_response:   # AUDIT SEC-4
        raise ValidationFailed("refresh_token")

    parts = str(introspection.get("fhirUser") or "").split("/")   # OpenEMR sends {fhir base}/Practitioner/<uuid>
    if len(parts) < 2 or not FHIR_ID.fullmatch(parts[-1]):
        raise ValidationFailed("fhir_user_invalid")
    if parts[-2] not in ("Practitioner", "Person"):
        raise ValidationFailed("fhir_user_not_clinician")

    patient = introspection.get("patient") or None
    if (token_response.get("patient") or None) not in (None, patient):
        raise ValidationFailed("patient_mismatch")
    if kind == "schedule":
        patient = None
    elif patient is not None and not (isinstance(patient, str) and PATIENT_ID.fullmatch(patient)):
        raise ValidationFailed("patient_invalid")
    elif kind == "patient" and patient is None:
        raise ValidationFailed("patient_missing")

    ends = [float(introspection["exp"])] if _is_num(introspection.get("exp")) else []
    if _is_num(token_response.get("expires_in")):
        ends.append(now + float(token_response["expires_in"]))
    if not ends:
        raise ValidationFailed("expiry_missing")
    if min(ends) <= now:
        raise ValidationFailed("token_expired")
    client_id = introspection.get("client_id")
    return LaunchGrant(fhir_user=f"{parts[-2]}/{parts[-1]}", patient_id=patient,
                       client_id=client_id if isinstance(client_id, str) else None, scopes=scopes, expires_at=min(ends))


async def complete_launch(http: httpx.AsyncClient, settings: Settings, states: StateStore, store: SessionStore,
                          code: str, state: str, cookie_state: Optional[str]) -> Tuple[str, Session]:
    """GET /smart/callback for both flows: state -> token -> introspect -> session. The kind (and so the page to serve:
    session.kind) comes from the pending state, never the request. cookie_state is the STATE_COOKIE value, or None."""
    pending = states.take(state)
    if pending is None:
        raise ValidationFailed("state_invalid")
    # Only the top-level schedule window can hold a first-party cookie. The EHR launch runs in OpenEMR's iframe, where
    # ours would be third-party (§1), so an EHR-launch callback link replayed in another browser is a stated limitation.
    if pending.kind == "schedule" and cookie_state != state:
        raise ValidationFailed("state_not_bound")
    tokens = await exchange_code(http, settings, code, pending.verifier)
    access_token = tokens.get("access_token")
    if not isinstance(access_token, str) or not access_token:
        raise UpstreamUnavailable("token_missing")
    grant = validate_launch_token(tokens, await introspect(http, settings, access_token), pending.kind, store.clock())
    return store.create(kind=pending.kind, source="launch" if pending.kind == "patient" else "schedule",
                        fhir_user=grant.fhir_user, client_id=grant.client_id, access_token=access_token,
                        token_expires_at=grant.expires_at, patient_id=grant.patient_id)


async def create_api_session(http: httpx.AsyncClient, settings: Settings, store: SessionStore, access_token: str,
                             patient_id: Optional[str], kind: Literal["patient", "schedule"] = "patient") -> Tuple[str, Session]:
    """POST /api/sessions (§2): patient from the token's launch context, else only an EVAL_PATIENT_IDS id (AUDIT SEC-1).
    kind='schedule' (UC5 evals, load tests) binds the user only, exactly like the /schedule window."""
    if not settings.allow_api_sessions:
        raise ValidationFailed("api_sessions_disabled")
    grant = validate_launch_token({}, await introspect(http, settings, access_token), "api", store.clock())
    if kind == "schedule":
        if patient_id is not None:
            raise ValidationFailed("patient_not_allowed")
        patient = None
    elif grant.patient_id is not None:
        if patient_id not in (None, grant.patient_id):
            raise ValidationFailed("patient_mismatch")
        patient = grant.patient_id
    elif patient_id in settings.eval_patient_ids:
        patient = patient_id
    else:
        raise ValidationFailed("patient_not_allowed")
    return store.create(kind=kind, source="api", fhir_user=grant.fhir_user, client_id=grant.client_id,
                        access_token=access_token, token_expires_at=grant.expires_at, patient_id=patient)


def panel_headers(settings: Settings) -> Dict[str, str]:
    """Headers for every panel response (§1 CSP, §2 callback no-store and no-referrer)."""
    return {"Cache-Control": "no-store", "Referrer-Policy": "no-referrer",
            "Content-Security-Policy": f"default-src 'self'; frame-ancestors {settings.openemr_public_origin}"}


def load_page(path: Path) -> str:
    """Read a panel template once at startup, failing there (not after a callback created a session) if it has no slot."""
    page = path.read_text()
    if page.count(PAGE_PLACEHOLDER) != 1:
        raise ValueError(f"{path.name} needs exactly one {PAGE_PLACEHOLDER}")
    return page


def panel_response(settings: Settings, page_html: str, handle: str) -> HTMLResponse:
    """Callback page: the handle rides in <meta name="copilot-session">, which the UI script reads into memory and removes."""
    if PAGE_PLACEHOLDER not in page_html:
        raise ValueError("panel page has no session placeholder")
    meta = f'<meta name="copilot-session" content="{html.escape(handle, quote=True)}">'
    return HTMLResponse(page_html.replace(PAGE_PLACEHOLDER, meta, 1), headers=panel_headers(settings))
