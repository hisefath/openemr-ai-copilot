"""SMART launch, callback validation, API sessions and the session store (ARCHITECTURE §1, §2).
OpenEMR's token and introspection endpoints are simulated with httpx.MockTransport; the clock is injected.
Each test names the failure mode it guards against."""
import asyncio
import base64
import hashlib
import json
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest

from copilot import smart
from copilot.config import Settings
from copilot.sessions import IDLE_TTL_S, SessionStore, handle_hash
from copilot.smart import StateStore, UpstreamUnavailable, ValidationFailed

FIX = Path(__file__).parent / "fixtures"
PID_A = json.loads((FIX / "patient_a.json").read_text())["patient_uuid"]
PID_B = json.loads((FIX / "patient_b.json").read_text())["patient_uuid"]
ISS = "https://emr.example/apis/default/fhir"
DOC = "Practitioner/9f1e2d3c-0000-4000-8000-000000000001"
NOW = 1_789_000_000.0
ALLOWLIST = ("openid fhirUser launch user/Patient.rs user/AllergyIntolerance.rs user/MedicationRequest.rs "
             "user/Condition.rs user/Observation.rs user/Encounter.rs user/Appointment.rs")


class Clock:
    def __init__(self):
        self.t = NOW

    def __call__(self):
        return self.t


def settings(**kw):
    return Settings(**{**dict(
        public_issuer=ISS, fhir_base="http://openemr/apis/default/fhir", oauth_public_base="https://emr.example/oauth2/default",
        oauth_internal_base="http://openemr/oauth2/default", openemr_public_origin="https://emr.example",
        client_id="copilot", client_secret="s3cret", agent_public_url="https://agent.example", hmac_key="k",
        audit_db_host=None, audit_db_user=None, audit_db_password=None, audit_db_ca=None), **kw})


def _drop_none(d):
    return {k: v for k, v in d.items() if v is not None}


def introspection(**kw):
    """Shape of OpenEMR 8.5.0 TokenIntrospectionRestController output for an EHR-launch token."""
    return _drop_none({"active": True, "status": "active", "scope": ALLOWLIST, "exp": int(NOW) + 3600, "sub": "u1",
                       "client_id": "copilot", "patient": PID_A, "fhirUser": f"{ISS}/{DOC}", **kw})


def token_response(**kw):
    return _drop_none({"token_type": "Bearer", "expires_in": 3600, "access_token": "at-1", "id_token": "jwt",
                       "scope": ALLOWLIST, "patient": PID_A, "need_patient_banner": False, **kw})


def openemr(tokens, intro, seen):
    def handler(req: httpx.Request):
        seen.append((req.url.path, parse_qs(req.content.decode())))
        if req.url.path == "/oauth2/default/token":
            return httpx.Response(200, json=tokens)
        if req.url.path == "/oauth2/default/introspect":
            return httpx.Response(200, json=intro)
        return httpx.Response(404)
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


def query(url):
    return {k: v[0] for k, v in parse_qs(urlsplit(url).query).items()}


def reason(tokens=None, intro=None, kind="patient"):
    with pytest.raises(ValidationFailed) as e:
        smart.validate_launch_token(token_response() if tokens is None else tokens, intro or introspection(), kind, NOW)
    return e.value.reason


def run_reason(coro, exc=ValidationFailed):
    with pytest.raises(exc) as e:
        asyncio.run(coro)
    return e.value.reason


def callback(tokens, intro, states, store, state, cookie=None, s=None, seen=None):
    return smart.complete_launch(openemr(tokens, intro, [] if seen is None else seen), s or settings(), states, store,
                                 "code-1", state, cookie)


def new_session(store, user=DOC, expires=NOW + 3600):
    return store.create(kind="patient", source="launch", fhir_user=user, client_id="copilot", access_token="at-1",
                        token_expires_at=expires, patient_id=PID_A)


# ---------------------------------------------------------------- launch and callback

def test_ehr_launch_happy_path_is_pkce_bound_and_takes_patient_from_introspection():
    """Guards: an authorize redirect missing launch/aud/S256, a code redeemable without our verifier, or a session whose
    patient comes from anywhere but OpenEMR's confirmed launch context (AUDIT SEC-1)."""
    clock = Clock()
    s, states, store = settings(), StateStore(clock), SessionStore(clock)
    url = smart.build_authorize_url(s, states, "launch-1", ISS, ISS)
    q = query(url)
    assert url.startswith("https://emr.example/oauth2/default/authorize?")
    assert q == {"response_type": "code", "client_id": "copilot", "redirect_uri": "https://agent.example/smart/callback",
                 "scope": " ".join(smart.PATIENT_SCOPES), "state": q["state"], "aud": ISS, "launch": "launch-1",
                 "code_challenge": q["code_challenge"], "code_challenge_method": "S256"}

    seen = []
    handle, session = asyncio.run(callback(token_response(), introspection(), states, store, q["state"], seen=seen))
    (token_path, token_form), (intro_path, intro_form) = seen
    verifier = token_form["code_verifier"][0]
    assert base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode() == q["code_challenge"]
    assert token_form["redirect_uri"] == ["https://agent.example/smart/callback"] and token_form["client_secret"] == ["s3cret"]
    assert intro_form["token"] == ["at-1"] and intro_form["client_id"] == ["copilot"] and intro_form["client_secret"] == ["s3cret"]
    assert (session.kind, session.source, session.patient_id, session.fhir_user) == ("patient", "launch", PID_A, DOC)
    assert session.token_expires_at == NOW + 3600 and store.get(handle) is session

    assert run_reason(callback(token_response(), introspection(), states, store, q["state"])) == "state_invalid"


def test_launch_with_foreign_iss_or_aud_is_refused_before_redirect():
    """Guards: sending the physician's authorization to an issuer we don't trust, or burning the launch on a bad request."""
    states = StateStore(Clock())
    evil = "https://evil.example/apis/default/fhir"
    for iss, aud in [(evil, evil), (ISS, evil), (ISS + "/", ISS + "/")]:
        with pytest.raises(ValidationFailed) as e:
            smart.build_authorize_url(settings(), states, "launch-1", iss, aud)
        assert e.value.reason == "iss_mismatch"
    assert smart.build_authorize_url(settings(), states, "launch-1", ISS, ISS)


def test_launch_value_replayed_within_an_hour_is_refused():
    """Guards: a captured launch URL reused to open a session (AUDIT ARCH-5: launch tokens have no expiry or user binding)."""
    clock = Clock()
    states = StateStore(clock)
    smart.build_authorize_url(settings(), states, "launch-1", ISS, ISS)
    clock.t += smart.LAUNCH_REPLAY_S - 1
    with pytest.raises(ValidationFailed) as e:
        smart.build_authorize_url(settings(), states, "launch-1", ISS, ISS)
    assert e.value.reason == "launch_replay"
    clock.t += 2
    assert smart.build_authorize_url(settings(), states, "launch-1", ISS, ISS)


def test_expired_or_forged_state_is_refused_without_calling_openemr():
    """Guards: login CSRF with a forged callback, or a code redeemed after the 5-minute state window."""
    clock = Clock()
    states, store = StateStore(clock), SessionStore(clock)
    state = query(smart.build_authorize_url(settings(), states, "launch-1", ISS, ISS))["state"]
    clock.t += smart.STATE_TTL_S + 1
    seen = []
    for st in (state, "forged"):
        assert run_reason(callback(token_response(), introspection(), states, store, st, seen=seen)) == "state_invalid"
    assert seen == []


def test_launch_flood_is_bounded_without_dropping_live_states_or_replay_hashes(monkeypatch):
    """Guards: unauthenticated /smart/launch or /schedule floods growing memory without bound, evicting a physician's
    state in flight, or flushing the replay set so a used launch value works again."""
    monkeypatch.setattr(smart, "MAX_PENDING_STATES", 3)
    monkeypatch.setattr(smart, "MAX_SEEN_LAUNCHES", 5)
    clock = Clock()
    states = StateStore(clock)
    real = query(smart.build_authorize_url(settings(), states, "real-launch", ISS, ISS))["state"]
    smart.build_authorize_url(settings(), states, "junk-0", ISS, ISS)
    smart.build_standalone_authorize_url(settings(), states)
    with pytest.raises(UpstreamUnavailable) as e:
        smart.build_authorize_url(settings(), states, "junk-1", ISS, ISS)
    assert e.value.reason == "launch_capacity"
    with pytest.raises(ValidationFailed) as e:
        smart.build_authorize_url(settings(), states, "real-launch", ISS, ISS)
    assert e.value.reason == "launch_replay"
    assert states.take(real) is not None

    clock.t += smart.STATE_TTL_S
    monkeypatch.setattr(smart, "MAX_PENDING_STATES", 10)   # now only the launch set is full
    for i in (1, 2, 3):
        smart.build_authorize_url(settings(), states, f"junk-{i}", ISS, ISS)
    with pytest.raises(UpstreamUnavailable):
        smart.build_authorize_url(settings(), states, "junk-4", ISS, ISS)
    with pytest.raises(ValidationFailed):
        smart.build_authorize_url(settings(), states, "real-launch", ISS, ISS)
    assert len(states._launches) == 5 and len(states._pending) == 3
    smart.build_standalone_authorize_url(settings(), states)   # no launch value: the replay set doesn't apply


def test_patient_scope_is_rejected():
    """Guards: patient/ scopes, which skip OpenEMR's role ACL so front-office staff could read clinical data (AUDIT SEC-2)."""
    assert reason(intro=introspection(scope=ALLOWLIST + " patient/Observation.rs")) == "scope_patient"
    assert reason(tokens=token_response(scope=ALLOWLIST + " patient/Observation.rs")) == "scope_patient"


def test_only_the_named_week_two_write_scopes_are_allowed():
    """Guards: the widening of this allowlist becoming general.

    Week 2 deliberately admits six standard-API scopes so a clinician can attach a document and approve a fact
    under their own identity — see smart.py for the attribution argument. Everything else a token might carry
    is still refused, and an UNNAMED write scope is refused even though write scopes now exist in principle."""
    assert reason(tokens=token_response(scope=ALLOWLIST + " user/MedicationRequest.cruds")) == "scope_not_allowed"
    assert reason(intro=introspection(scope=ALLOWLIST + " offline_access")) == "scope_not_allowed"
    assert reason(tokens=token_response(scope=ALLOWLIST + " user/procedure.cruds")) == "scope_not_allowed"


def test_the_week_two_write_scopes_are_accepted_on_a_clinician_session():
    """Guards: the reverse — a correctly-scoped clinician launch being rejected, which would make document
    attachment impossible in the deployed app."""
    granted = ALLOWLIST + " " + " ".join(smart.WRITE_SCOPES)
    grant = smart.validate_launch_token(token_response(scope=granted), introspection(scope=granted),
                                        "patient", NOW)
    assert set(smart.WRITE_SCOPES) <= set(grant.scopes)


def test_a_chart_launch_actually_asks_for_the_write_scopes():
    """Guards the gap that shipped: the allowlist was widened and the authorize REQUEST was not.

    OpenEMR grants the intersection of the requested scope string and the scopes the client is registered for
    (AuthorizationController.php:1701, "only authorize scopes specifically allowed by the client regardless of
    what is sent in the request"). So admitting a scope in _ALLOWED is necessary and useless on its own — a
    scope absent from this URL cannot come back in the token no matter how the client is registered, and every
    document attachment and approval write would 403 in the deployed app while every test still passed."""
    asked = set(query(smart.build_authorize_url(settings(), StateStore(Clock()), "launch-1", ISS, ISS))["scope"].split())
    assert set(smart.WRITE_SCOPES) <= asked
    assert asked <= set(smart.PATIENT_SCOPES)   # and nothing beyond what the allowlist will accept back


def test_a_schedule_session_still_cannot_write():
    """Guards: the widening leaking to the standalone schedule scan, which has no document to attach and no
    clinician in front of it to approve anything."""
    assert "api:oemr" not in smart.SCHEDULE_SCOPES
    assert not set(smart.WRITE_SCOPES) & set(smart.SCHEDULE_SCOPES)


def test_refresh_token_in_response_is_rejected():
    """Guards: a long-lived refresh token in agent memory, redeemable without the client secret (AUDIT SEC-4)."""
    assert reason(tokens=token_response(refresh_token="rt-1")) == "refresh_token"


def test_launch_patient_scope_is_rejected_on_ehr_launch_and_nonce_pseudo_scope_is_ignored():
    """Guards: an EHR launch holding the picker scope it never requested, or every launch failing because OpenEMR appends
    `nonce` to the granted scope once any authorize request in the PHP session carried one (ScopeRepository.php:173)."""
    assert reason(tokens=token_response(scope=ALLOWLIST + " launch/patient")) == "scope_not_allowed"
    grant = smart.validate_launch_token(token_response(scope=ALLOWLIST + " nonce"), introspection(scope=ALLOWLIST + " nonce"),
                                        "patient", NOW)
    assert "nonce" not in grant.scopes and grant.patient_id == PID_A


def test_rejected_callback_creates_no_session():
    """Guards: a refactor that creates the session (and stores the access token) before the callback checks run."""
    clock = Clock()
    states, store = StateStore(clock), SessionStore(clock)
    cases = [(token_response(refresh_token="rt-1"), introspection(), "refresh_token"),
             (token_response(scope=ALLOWLIST + " patient/Patient.rs"), introspection(), "scope_patient"),
             (token_response(), introspection(fhirUser=f"{ISS}/Patient/{PID_A}"), "fhir_user_not_clinician")]
    for tokens, intro, why in cases:
        state = query(smart.build_authorize_url(settings(), states, f"launch-{why}", ISS, ISS))["state"]
        assert run_reason(callback(tokens, intro, states, store, state)) == why
    state = query(smart.build_authorize_url(settings(), states, "launch-no-token", ISS, ISS))["state"]
    assert run_reason(callback(token_response(access_token=None), introspection(), states, store, state),
                      UpstreamUnavailable) == "token_missing"
    assert store._sessions == {}


def test_patient_fhir_user_is_rejected():
    """Guards: a patient-portal login opening the clinician co-pilot."""
    assert reason(intro=introspection(fhirUser=f"{ISS}/Patient/{PID_A}")) == "fhir_user_not_clinician"
    assert reason(intro=introspection(fhirUser=None)) == "fhir_user_invalid"
    person = smart.validate_launch_token(token_response(), introspection(fhirUser=f"{ISS}/Person/abc-1"), "patient", NOW)
    assert person.fhir_user == "Person/abc-1"


def test_missing_or_disagreeing_patient_is_rejected():
    """Guards: an EHR-launch session with no bound patient, or token response and introspection naming different patients."""
    assert reason(tokens=token_response(patient=None), intro=introspection(patient=None)) == "patient_missing"
    assert reason(tokens=token_response(patient=PID_B)) == "patient_mismatch"
    assert reason(tokens=token_response(patient=PID_A), intro=introspection(patient=None)) == "patient_mismatch"
    assert reason(tokens=token_response(patient="../Patient/x"), intro=introspection(patient="../Patient/x")) == "patient_invalid"


def test_inactive_or_expired_token_is_rejected():
    """Guards: a session created from a revoked or already expired token."""
    assert reason(intro=introspection(active=False)) == "inactive"
    assert reason(intro=introspection(active="true")) == "inactive"
    assert reason(intro=introspection(exp=int(NOW))) == "token_expired"
    assert reason(tokens=token_response(expires_in=None), intro=introspection(exp=None)) == "expiry_missing"


def test_token_and_introspection_failures_raise_reason_codes_not_bodies():
    """Guards: OpenEMR error bodies (or a 500 page) leaking into logs, a failed exchange treated as success, or an OpenEMR
    outage (PERF-1) reported as an auth rejection (401) instead of 503, which hides it from the §7 error-rate alert."""
    def post(fn, response, exc):
        def handler(req):
            assert req.extensions["timeout"] == {"connect": 1.0, "read": 3.0, "write": 3.0, "pool": 3.0}   # §3 budget
            if isinstance(response, Exception):
                raise response
            return response
        http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        call = smart.exchange_code(http, settings(), "c", "v") if fn == "token" else smart.introspect(http, settings(), "at-1")
        return run_reason(call, exc)

    assert post("token", httpx.Response(400, json={"error": "invalid_grant", "hint": "details"}), ValidationFailed) == "token_http_400"
    assert post("token", httpx.Response(503, text="busy"), UpstreamUnavailable) == "token_http_503"
    assert post("introspect", httpx.Response(401, json={"error": "invalid_request"}), UpstreamUnavailable) == "introspect_http_401"
    assert post("introspect", httpx.Response(200, text="<html>"), UpstreamUnavailable) == "introspect_unparseable"
    assert post("introspect", httpx.ReadTimeout("slow"), UpstreamUnavailable) == "introspect_timeout"
    assert post("token", httpx.ConnectError("refused"), UpstreamUnavailable) == "token_unreachable"


def test_unconfigured_client_is_an_outage_not_a_rejection_and_burns_no_launch():
    """Guards: a missing SMART_CLIENT_SECRET answered as 401 (the physician blamed), or the launch value recorded anyway."""
    states = StateStore(Clock())
    with pytest.raises(UpstreamUnavailable) as e:
        smart.build_authorize_url(settings(client_secret=None), states, "launch-1", ISS, ISS)
    assert e.value.reason == "client_not_configured" and states._launches == {} and states._pending == {}
    s = settings(client_id=None, allow_api_sessions=True)
    assert run_reason(smart.create_api_session(openemr({}, introspection(), []), s, SessionStore(Clock()), "at-1", None),
                      UpstreamUnavailable) == "client_not_configured"


# ---------------------------------------------------------------- schedule and API sessions

def test_schedule_standalone_flow_binds_user_only_via_the_registered_callback():
    """Guards: the schedule window requesting launch context, binding a patient (UC5), or using a redirect URI other than
    the one registered /smart/callback, which OpenEMR's exact-match check rejects (CustomAuthCodeGrant::validateRedirectUri)."""
    clock = Clock()
    s, states, store = settings(), StateStore(clock), SessionStore(clock)
    url, cookie = smart.build_standalone_authorize_url(s, states)
    q = query(url)
    assert "launch" not in q and q["redirect_uri"] == "https://agent.example/smart/callback"
    assert q["scope"] == ALLOWLIST.replace(" launch", "") and q["aud"] == ISS and q["code_challenge_method"] == "S256"
    assert cookie == f"__Host-copilot-state={q['state']}; Max-Age=300; Path=/; Secure; HttpOnly; SameSite=Lax"

    scopes, seen = " ".join(smart.SCHEDULE_SCOPES), []
    handle, session = asyncio.run(callback(token_response(scope=scopes, patient=None), introspection(scope=scopes, patient=None),
                                           states, store, q["state"], cookie=q["state"], seen=seen))
    assert (session.kind, session.source, session.patient_id, session.fhir_user) == ("schedule", "schedule", None, DOC)
    assert seen[0][1]["redirect_uri"] == ["https://agent.example/smart/callback"] and store.get(handle) is session
    assert reason(tokens=token_response(patient=None), intro=introspection(patient=None), kind="schedule") == "scope_not_allowed"


def test_schedule_callback_from_another_browser_is_refused():
    """Guards: login CSRF, where an attacker's /schedule callback link opened by a colleague hands them a session bound to the
    attacker's token, so every question is audited under the wrong fhirUser (COMP-7)."""
    clock = Clock()
    states, store = StateStore(clock), SessionStore(clock)
    seen = []
    for cookie in (None, "someone-elses-state"):
        _, set_cookie = smart.build_standalone_authorize_url(settings(), states)
        state = set_cookie.split(";")[0].split("=", 1)[1]
        assert run_reason(callback(token_response(), introspection(), states, store, state, cookie=cookie,
                                   seen=seen)) == "state_not_bound"
    assert seen == [] and store._sessions == {}


def test_api_sessions_disabled_rejects_without_introspecting():
    """Guards: the bare-token session path left open on a deployment with real PHI (ALLOW_API_SESSIONS=false)."""
    seen = []
    assert run_reason(smart.create_api_session(openemr({}, introspection(), seen), settings(), SessionStore(Clock()),
                                               "at-1", PID_A)) == "api_sessions_disabled"
    assert seen == []


def test_api_session_patient_comes_from_token_context_or_eval_allowlist_only():
    """Guards: POST /api/sessions opening any patient's chart by id (AUDIT SEC-1)."""
    s = settings(allow_api_sessions=True, eval_patient_ids=frozenset({PID_A}))
    store = SessionStore(Clock())
    no_ctx = introspection(scope=" ".join(smart.API_SCOPES), patient=None)

    def api(intro, patient_id):
        return smart.create_api_session(openemr({}, intro, []), s, store, "at-1", patient_id)

    assert run_reason(api(no_ctx, PID_B)) == "patient_not_allowed"
    assert run_reason(api(no_ctx, None)) == "patient_not_allowed"
    _, session = asyncio.run(api(no_ctx, PID_A))
    assert (session.kind, session.source, session.patient_id) == ("patient", "api", PID_A)
    _, session = asyncio.run(api(introspection(patient=PID_B), None))
    assert session.patient_id == PID_B
    assert run_reason(api(introspection(patient=PID_B), PID_A)) == "patient_mismatch"
    assert run_reason(api(introspection(scope=ALLOWLIST + " patient/Patient.rs", patient=None), PID_A)) == "scope_patient"


def test_api_schedule_session_binds_user_only():
    """Guards: UC5 evals and load tests unable to open a schedule session over the API, or one bound to a patient id."""
    s = settings(allow_api_sessions=True, eval_patient_ids=frozenset({PID_A}))
    store = SessionStore(Clock())
    _, session = asyncio.run(smart.create_api_session(openemr({}, introspection(), []), s, store, "at-1", None, "schedule"))
    assert (session.kind, session.source, session.patient_id, session.fhir_user) == ("schedule", "api", None, DOC)
    assert run_reason(smart.create_api_session(openemr({}, introspection(), []), s, store, "at-1", PID_A,
                                               "schedule")) == "patient_not_allowed"


# ---------------------------------------------------------------- session store

def test_session_store_keeps_only_the_handle_hash():
    """Guards: bearer handles or access tokens readable from memory or a repr, or sessions found by anything but the handle."""
    store = SessionStore(Clock())
    handle, session = new_session(store)
    assert list(store._sessions) == [handle_hash(handle)] and len(handle) >= 43
    assert handle not in repr(vars(store))
    assert not any(x in repr(session) for x in ("at-1", PID_A, DOC, "copilot")) and session.session_ref in repr(session)
    assert store.get(handle_hash(handle)) is None and store.get(session.session_ref) is None
    assert store.get(handle) is session


def test_idle_session_expires_after_15_minutes_and_use_extends_it():
    """Guards: an abandoned panel staying usable, or an active physician logged out mid-visit."""
    clock = Clock()
    store = SessionStore(clock)
    handle, session = new_session(store)
    clock.t += IDLE_TTL_S - 1
    assert store.get(handle) is session
    clock.t += IDLE_TTL_S - 1
    assert store.get(handle) is session
    clock.t += IDLE_TTL_S
    assert store.get(handle) is None and store._sessions == {}


def test_session_never_outlives_its_access_token():
    """Guards: a session answering from cached PHI after OpenEMR's token expired."""
    clock = Clock()
    store = SessionStore(clock)
    handle, session = new_session(store, expires=NOW + 600)
    assert session.expires_at == NOW + 600
    clock.t += 599
    assert store.get(handle) is session
    clock.t += 1
    assert store.get(handle) is None


def test_fourth_session_for_a_user_evicts_the_least_recently_used():
    """Guards: unbounded live sessions (and tokens) per user; one user's launches evicting another user's session; or the
    schedule window in active use evicted before patient panels that were already closed."""
    clock = Clock()
    store = SessionStore(clock)
    other, _ = new_session(store, user="Practitioner/other")
    handles = []
    for _ in range(3):
        clock.t += 1
        handles.append(new_session(store)[0])
    clock.t += 1
    assert store.get(handles[0])   # oldest created, just used
    handles.append(new_session(store)[0])
    assert store.get(handles[1]) is None
    assert all(store.get(h) for h in (handles[0], handles[2], handles[3])) and store.get(other)


def test_prefetch_is_reused_only_for_same_user_and_patient_within_120_s():
    """Guards: a re-launch refetching everything (AUDIT PERF-1), or reusing another user's, another patient's or stale data."""
    clock = Clock()
    store = SessionStore(clock)
    store.put_prefetch(DOC, PID_A, "ctx")
    clock.t += 119
    assert store.get_prefetch(DOC, PID_A) == "ctx"
    assert store.get_prefetch("Practitioner/other", PID_A) is None and store.get_prefetch(DOC, PID_B) is None
    clock.t += 1
    assert store.get_prefetch(DOC, PID_A) is None


def test_expired_sessions_and_prefetch_leave_memory_without_new_launches():
    """Guards: yesterday's access tokens and patient context sitting in process memory (and heap dumps) until the next
    launch happens to purge them."""
    clock = Clock()
    store = SessionStore(clock)
    _, session = new_session(store)
    session.context = "phi"
    store.put_prefetch(DOC, PID_A, "phi")
    clock.t += IDLE_TTL_S
    store.purge()
    assert store._sessions == {} and store._prefetch == {}

    new_session(store)
    store.put_prefetch(DOC, PID_A, "phi")
    clock.t += IDLE_TTL_S
    assert store.get_prefetch(DOC, PID_B) is None and store._sessions == {} and store._prefetch == {}


def test_callback_page_delivers_handle_in_meta_with_no_store_and_framing_lock():
    """Guards: the handle cached by a proxy, leaked via Referer, breaking out of its attribute, or the panel framed by
    a non-OpenEMR origin."""
    page = '<html><head><!--COPILOT_SESSION_META--><script src="/static/panel.js"></script></head></html>'
    r = smart.panel_response(settings(), page, 'abc"><x>')
    assert '<head><meta name="copilot-session" content="abc&quot;&gt;&lt;x&gt;"><script' in r.body.decode()
    assert r.headers["cache-control"] == "no-store" and r.headers["referrer-policy"] == "no-referrer"
    assert r.headers["content-security-policy"] == "default-src 'self'; frame-ancestors https://emr.example"


def test_page_without_session_placeholder_fails_at_startup(tmp_path):
    """Guards: a template edit (e.g. <head data-theme>) that only surfaces as a 500 after a callback already created a
    session; the UI and this module share one insertion point, <!--COPILOT_SESSION_META--> (test_ui_static.py)."""
    good, bad = tmp_path / "panel.html", tmp_path / "schedule.html"
    good.write_text("<html><head><!--COPILOT_SESSION_META--></head></html>")
    bad.write_text('<html><head data-theme="light"></head></html>')
    assert smart.load_page(good) == good.read_text()
    with pytest.raises(ValueError):
        smart.load_page(bad)
    with pytest.raises(ValueError):
        smart.panel_response(settings(), bad.read_text(), "h")
