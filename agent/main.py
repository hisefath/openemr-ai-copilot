"""Clinical Co-Pilot agent: HTTP wiring for ARCHITECTURE §2 (launch, sessions), §4.2 (question flow),
§4.3 (schedule scan), §7 (correlation, metrics, audit) and §8 (API contract)."""
import asyncio
import json
import logging
import time
from contextlib import asynccontextmanager
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional

import anthropic
import httpx
from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles

import audit
import fhir
import llm
import observability as obs
import render
import rules
import smart
import verify
from config import Settings
from deadline import Deadline
from schemas import (AuditEvent, AuditEventType, EncountersInput, ErrorBody, ErrorResponse, LabHistoryInput, LoadStatus,
                     MessageRequest, MessageResponse, Outcome, PatientContext, ScheduleScanResponse, SessionCreateRequest,
                     SessionCreateResponse, SessionStatus, ToolName)
from sessions import Session, SessionStore, Turn

log = logging.getLogger("agent")
STATIC = Path(__file__).parent / "static"
PREFETCH_BUDGET_S = 12.0      # launch prefetch runs in the background, not inside a question's deadline
FRESH_S = 120                 # §3 freshness: refetch anything older before answering
LLM_RESERVE_S = 4.0           # a question waits for a running prefetch only while this much deadline remains
AUDIT_TIMEOUT_S = 3.0
PATIENT_TOOLS = [ToolName.get_lab_history, ToolName.get_encounters]  # §3: one round, patient sessions only
TOOL_WINDOWS = {"get_lab_history": "labs", "get_encounters": "encounters"}


def _epoch_iso(ts: float) -> str:
    return datetime.fromtimestamp(ts, timezone.utc).isoformat(timespec="seconds")


@asynccontextmanager
async def lifespan(app: FastAPI):
    obs.configure_logging()
    settings = Settings.from_env()
    app.state.settings = settings
    app.state.http = httpx.AsyncClient(timeout=smart.OPENEMR_TIMEOUT)
    app.state.fhir = fhir.FhirClient(app.state.http, settings.fhir_base, settings.openemr_concurrency)
    app.state.llm = anthropic.AsyncAnthropic(max_retries=0)
    app.state.store = SessionStore()
    app.state.states = smart.StateStore()
    app.state.locks: Dict[str, asyncio.Lock] = {}
    if settings.audit_db_host:
        app.state.audit = audit.AuditWriter(settings)
    else:  # local development without the audit database: rows go to the log instead (never in production)
        log.warning("AUDIT_DB_HOST not set: audit rows are logged, not stored")
        app.state.audit = audit.FakeAuditWriter()
    app.state.pages = {"patient": smart.load_page(STATIC / "panel.html"), "schedule": smart.load_page(STATIC / "schedule.html")}
    yield
    await app.state.http.aclose()
    lf = obs.langfuse_client()
    if lf is not None:
        lf.flush()


app = FastAPI(title="Clinical Co-Pilot Agent", lifespan=lifespan)
app.mount("/static", StaticFiles(directory=STATIC), name="static")


# ---------------------------------------------------------------- correlation, errors


@app.middleware("http")
async def correlation(request: Request, call_next):
    cid = obs.new_correlation_id()  # server-generated (§7); a client id is only kept separately, if it's a UUID
    obs.correlation_id.set(cid)
    start = time.perf_counter()
    try:
        response = await call_next(request)
    except Exception as e:  # never let exception text reach the client or the access log
        log.error("unhandled", extra={"error": obs.error_code(e), "path": request.url.path})
        obs.count(obs.Metric.error, kind="http_5xx")
        response = _error(500, "internal_error", "Something went wrong. Try again.")
    response.headers["X-Correlation-ID"] = cid
    log.info("request", extra={"method": request.method, "path": request.url.path, "status": response.status_code,
                               "ms": round((time.perf_counter() - start) * 1000)})
    return response


def _error(status: int, code: str, message: str) -> JSONResponse:
    body = ErrorResponse(error=ErrorBody(code=code, message=message, correlation_id=obs.correlation_id.get()))
    return JSONResponse(body.model_dump(), status_code=status)


@app.exception_handler(HTTPException)
async def http_error(_: Request, exc: HTTPException):
    code, _, message = str(exc.detail).partition(":")
    return _error(exc.status_code, code or "error", message.strip() or code)


@app.exception_handler(RequestValidationError)
async def validation_error(_: Request, exc: RequestValidationError):
    return _error(400, "invalid_request", "The request is not valid.")  # never echo input (it may hold PHI or tokens)


# ---------------------------------------------------------------- health


@app.get("/health")
async def health():
    return {"status": "ok"}


@app.get("/ready")
async def ready(request: Request):
    s: Settings = request.app.state.settings
    checks: Dict[str, str] = {}

    async def check(name, coro):
        try:
            await asyncio.wait_for(coro, 5)
            checks[name] = "ok"
        except Exception as e:
            checks[name] = f"fail: {obs.error_code(e)}"

    async def openemr():
        (await request.app.state.http.get(f"{s.fhir_base}/metadata")).raise_for_status()

    async def anthropic_api():
        await request.app.state.llm.models.retrieve(s.anthropic_model)

    async def langfuse():
        lf = obs.langfuse_client()
        if lf is None:
            raise RuntimeError("not_configured")
        if not await asyncio.to_thread(lf.auth_check):
            raise RuntimeError("auth_failed")

    await asyncio.gather(check("openemr_fhir", openemr()), check("anthropic", anthropic_api()), check("langfuse", langfuse()))
    ok = all(v == "ok" for v in checks.values())
    return JSONResponse({"ready": ok, "checks": checks}, status_code=200 if ok else 503)


# ---------------------------------------------------------------- audit helpers (§7, FM-15)


def _event(kind: AuditEventType, session: Optional[Session] = None, **fields) -> AuditEvent:
    return AuditEvent(event=kind, ts_ms=int(time.time() * 1000), correlation_id=obs.correlation_id.get(),
                      session_ref=session.session_ref if session else None,
                      fhir_user=session.fhir_user if session else None,
                      client_id=session.client_id if session else None,
                      source=session.source if session else None, **fields)


async def _audit(request: Request, events: List[AuditEvent]) -> None:
    """All rows or a 503: an unaudited disclosure is never returned (FM-15)."""
    if not events:
        return
    try:
        await asyncio.gather(*(request.app.state.audit.write(e, AUDIT_TIMEOUT_S) for e in events))
    except audit.AuditUnavailable:
        obs.count(obs.Metric.error, kind="audit_unavailable")
        raise HTTPException(503, "audit_unavailable: The Co-Pilot can't record access right now, so it can't answer.")


def _fhir_recorder(client: fhir.FhirClient, session: Session, sink: List[AuditEvent]):
    """FHIR call events -> metrics, a log line (no ids, no query strings) and a pending audit row."""
    def on_event(e: dict) -> None:
        status = e["status"] if isinstance(e["status"], LoadStatus) else LoadStatus(e["status"])
        obs.count_fhir(e["resource"], status)
        if e.get("retried"):
            obs.count(obs.Metric.retry, kind="fhir")
        obs.set_queue_depth(client.queue_depth)
        log.info("fhir", extra={k: e.get(k) for k in ("method", "resource", "path", "http_status", "count", "ms", "queue_ms", "retried")}
                 | {"status": status.value})
        sink.append(_event(AuditEventType.fhir_read, session, patient_id=e.get("audit_patient_id"), fhir_path=e["path"],
                           http_status=e.get("http_status"), record_count=e.get("count"), outcome=status.value))
    return on_event


# ---------------------------------------------------------------- sessions (§2)


def _session(request: Request, authorization: Optional[str], kind: str) -> Session:
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(401, "session_missing: Relaunch the Co-Pilot from the chart.")
    session = request.app.state.store.get(authorization[len("Bearer "):].strip())
    if session is None:
        raise HTTPException(401, "session_expired: Session expired: relaunch from the chart.")
    if session.kind != kind:
        raise HTTPException(403, "wrong_session_kind: This session can't do that.")
    return session


def _start_prefetch(request: Request, session: Session) -> None:
    """Launch-time prefetch in the background, reusing a (user, patient) result younger than 120 s (§2 re-launch)."""
    store: SessionStore = request.app.state.store
    cached = store.get_prefetch(session.fhir_user, session.patient_id)
    if cached is not None:
        session.context, session.context_fetched_at = cached
        return
    cid = obs.correlation_id.get()

    async def run():
        obs.correlation_id.set(cid)
        rows: List[AuditEvent] = []
        with obs.span("prefetch", session_ref=session.session_ref, patient=obs.hmac_id(request.app.state.settings, session.patient_id)):
            ctx = await fhir.prefetch(request.app.state.fhir, session.access_token, session.patient_id, date.today(),
                                      Deadline(PREFETCH_BUDGET_S), _fhir_recorder(request.app.state.fhir, session, rows), cid)
        await _audit(request, rows)  # raises -> context stays unset; questions then fail closed
        session.context, session.context_fetched_at = ctx, time.time()
        store.put_prefetch(session.fhir_user, session.patient_id, (ctx, session.context_fetched_at))

    session.prefetch_task = asyncio.create_task(run())


@app.get("/smart/launch")
async def smart_launch(request: Request, launch: str = "", iss: str = "", aud: str = ""):
    try:
        url = smart.build_authorize_url(request.app.state.settings, request.app.state.states, launch, iss, aud or iss)
    except smart.ValidationFailed as e:
        log.warning("launch rejected", extra={"reason": e.reason})
        raise HTTPException(400, f"{e.reason}: This launch can't be completed. Relaunch from the patient chart.")
    except smart.UpstreamUnavailable as e:
        raise HTTPException(503, f"{e.reason}: The Co-Pilot is busy. Try again in a moment.")
    return RedirectResponse(url, status_code=302, headers={"Cache-Control": "no-store", "Referrer-Policy": "no-referrer"})


@app.get("/schedule")
async def schedule_launch(request: Request):
    try:
        url, cookie = smart.build_standalone_authorize_url(request.app.state.settings, request.app.state.states)
    except smart.UpstreamUnavailable as e:
        raise HTTPException(503, f"{e.reason}: The Co-Pilot is busy. Try again in a moment.")
    return RedirectResponse(url, status_code=302, headers={"Set-Cookie": cookie, "Cache-Control": "no-store",
                                                           "Referrer-Policy": "no-referrer"})


@app.get(smart.CALLBACK_PATH)
async def smart_callback(request: Request, code: str = "", state: str = ""):
    s: Settings = request.app.state.settings
    try:
        handle, session = await smart.complete_launch(request.app.state.http, s, request.app.state.states,
                                                      request.app.state.store, code, state,
                                                      request.cookies.get(smart.STATE_COOKIE))
    except smart.ValidationFailed as e:
        log.warning("callback rejected", extra={"reason": e.reason})
        raise HTTPException(403, f"{e.reason}: This launch could not be verified. Relaunch from the patient chart.")
    except smart.UpstreamUnavailable as e:
        obs.count(obs.Metric.error, kind="oauth_unavailable")
        raise HTTPException(503, f"{e.reason}: OpenEMR sign-in is unavailable right now.")
    await _audit(request, [_event(AuditEventType.launch, session, patient_id=session.patient_id),
                           _event(AuditEventType.session_create, session, patient_id=session.patient_id)])
    if session.kind == "patient":
        _start_prefetch(request, session)
    response = smart.panel_response(s, request.app.state.pages[session.kind], handle)
    if session.kind == "schedule":
        response.delete_cookie(smart.STATE_COOKIE, path="/", secure=True, httponly=True, samesite="lax")
    return response


@app.post("/api/sessions", response_model=SessionCreateResponse)
async def create_session(request: Request, body: SessionCreateRequest):
    try:
        handle, session = await smart.create_api_session(request.app.state.http, request.app.state.settings,
                                                         request.app.state.store, body.access_token, body.patient_id,
                                                         body.kind)
    except smart.ValidationFailed as e:
        log.warning("api session rejected", extra={"reason": e.reason})
        raise HTTPException(403, f"{e.reason}: Session not allowed.")
    except smart.UpstreamUnavailable as e:
        raise HTTPException(503, f"{e.reason}: OpenEMR is unavailable right now.")
    await _audit(request, [_event(AuditEventType.session_create, session, patient_id=session.patient_id)])
    if session.kind == "patient":
        _start_prefetch(request, session)
    return SessionCreateResponse(session_handle=handle, expires_at=_epoch_iso(session.expires_at))


@app.get("/api/session", response_model=SessionStatus)
async def session_status(request: Request, authorization: Optional[str] = Header(None)):
    session = request.app.state.store.get((authorization or "")[len("Bearer "):].strip()) if authorization else None
    if session is None:
        raise HTTPException(401, "session_expired: Session expired: relaunch from the chart.")
    if session.kind == "schedule":
        return SessionStatus(kind="schedule")
    ctx: Optional[PatientContext] = session.context
    if ctx is None:
        failed = session.prefetch_task is not None and session.prefetch_task.done()
        status = LoadStatus.error if failed else LoadStatus.pending
        return SessionStatus(kind="patient", load_statuses={k: status for k in fhir.ORDER})
    return SessionStatus(kind="patient", patient_banner=render.patient_banner(ctx, date.today()),
                         data_as_of=ctx.fetched_at,
                         load_statuses={k: getattr(ctx, k).status for k in fhir.ORDER},
                         flags=rules.check(ctx.allergies.records, ctx.medications.records, ctx.labs.records))


# ---------------------------------------------------------------- questions (§4.2)


async def _context(request: Request, session: Session, deadline: Deadline, sink: List[AuditEvent]) -> PatientContext:
    """Wait for the launch prefetch while enough deadline remains, then refresh anything older than 120 s (§3)."""
    task = session.prefetch_task
    if session.context is None and task is not None and not task.done():
        try:
            await asyncio.wait_for(asyncio.shield(task), max(0.0, deadline.remaining() - LLM_RESERVE_S))
        except asyncio.TimeoutError:
            pass
    if session.context is None:
        if task is not None and task.done() and task.exception() is not None:
            if isinstance(task.exception(), HTTPException):
                raise task.exception()
        rows: List[AuditEvent] = []
        ctx = await fhir.prefetch(request.app.state.fhir, session.access_token, session.patient_id, date.today(),
                                  Deadline(max(0.5, deadline.remaining() - LLM_RESERVE_S)), _fhir_recorder(request.app.state.fhir, session, sink),
                                  obs.correlation_id.get())
        session.context, session.context_fetched_at = ctx, time.time()
        return ctx
    if session.context_fetched_at and time.time() - session.context_fetched_at > FRESH_S and deadline.has(LLM_RESERVE_S + 1):
        ctx = await fhir.refresh(session.context, fhir.ORDER, request.app.state.fhir, session.access_token, date.today(),
                                 Deadline(deadline.remaining() - LLM_RESERVE_S), _fhir_recorder(request.app.state.fhir, session, sink),
                                 obs.correlation_id.get())
        session.context, session.context_fetched_at = ctx, time.time()
    return session.context


@app.post("/api/session/messages", response_model=MessageResponse)
async def ask(request: Request, body: MessageRequest, authorization: Optional[str] = Header(None)):
    s: Settings = request.app.state.settings
    session = _session(request, authorization, "patient")
    lock = request.app.state.locks.setdefault(session.session_ref, asyncio.Lock())
    if lock.locked():
        raise HTTPException(409, "busy: Another question is still being answered.")
    async with lock:
        return await _answer(request, s, session, body)


async def _answer(request: Request, s: Settings, session: Session, body: MessageRequest) -> MessageResponse:
    deadline = Deadline(s.question_deadline_s)
    cid = obs.correlation_id.get()
    rows: List[AuditEvent] = []
    today, now = date.today(), datetime.now(timezone.utc)
    with obs.span("message", session_ref=session.session_ref, patient=obs.hmac_id(s, session.patient_id),
                  client_request_id=obs.parse_client_request_id(body.client_request_id)) as trace:
        ctx = await _context(request, session, deadline, rows)
        flags = rules.check(ctx.allergies.records, ctx.medications.records, ctx.labs.records)

        async def run_tool(name: ToolName, args, tool_deadline: Deadline):
            """Tools always read the session's patient; results join the record index so verification accepts them."""
            nonlocal ctx
            if name is ToolName.get_lab_history:
                load = await fhir.get_lab_history(request.app.state.fhir, session.access_token, session.patient_id,
                                                  args, tool_deadline, _fhir_recorder(request.app.state.fhir, session, rows), cid)
            else:
                load = await fhir.get_encounters(request.app.state.fhir, session.access_token, session.patient_id,
                                                 args, tool_deadline, _fhir_recorder(request.app.state.fhir, session, rows), cid)
            if load.status not in (LoadStatus.ok, LoadStatus.empty):
                return "", False
            field = TOOL_WINDOWS[name.value]
            known = {sid for r in getattr(ctx, field).records for sid in r.source_ids}
            new = [r for r in load.records if not set(r.source_ids) & known]
            merged = getattr(ctx, field).model_copy(update={"records": getattr(ctx, field).records + new})
            ctx = ctx.model_copy(update={field: merged})
            return json.dumps([r.model_dump(exclude_none=True) for r in load.records], separators=(",", ":")), True

        model_context = render.build_model_context(ctx, flags, list(session.history), ctx.fetched_at, today, fenced=False)
        plan, meta = await llm.plan_answer(request.app.state.llm, s, llm.SYSTEM_PROMPT, model_context, body.question,
                                           PATIENT_TOOLS, deadline, run_tool)
        answer = verify.verify_and_render(plan, ctx, body.question, body.selected_source_id, flags, today, now)

        if plan is None:
            obs.count(obs.Metric.error, kind=f"llm_{meta.reason or 'unknown'}")
        if answer.verifier_error:
            obs.count(obs.Metric.error, kind="verifier_exception")
        obs.count(obs.Metric.verification, outcome=answer.outcome.value)
        trace.update(metadata={"outcome": answer.outcome.value, "withheld": answer.withheld_count, "flags": len(answer.flags),
                               "high_flags": sum(f.severity == "high" for f in answer.flags), "llm_reason": meta.reason,
                               "llm_calls": meta.calls, "llm_cost_usd": round(meta.cost_usd, 6), "tools_run": meta.tools_run,
                               "intent": plan.intent.value if plan else None, "anthropic_request_ids": meta.request_ids,
                               "deadline_left_s": round(deadline.remaining(), 2)})

        rows.append(_event(AuditEventType.llm_call, session, patient_id=session.patient_id,
                           outcome="ok" if plan else "no_plan", detail=meta.reason, http_status=meta.http_status))
        rows.append(_event(AuditEventType.question, session, patient_id=session.patient_id,
                           intent=plan.intent.value if plan else None, outcome=answer.outcome.value))
        if answer.outcome is Outcome.refused:
            rows.append(_event(AuditEventType.refusal, session, patient_id=session.patient_id,
                               detail=plan.scope_violation.value if plan else None))
        rows += [_event(AuditEventType.denied, session, patient_id=session.patient_id, detail=f"source_id:{sid}"[:200])
                 for sid in answer.denied_source_ids]
        rows += [_event(AuditEventType.denied, session, patient_id=session.patient_id, detail=f"tool:{t}")
                 for t in meta.tools_denied]
        await _audit(request, rows)

    session.history.append(Turn(body.question, [line for sec in answer.sections for line in sec.lines]))
    return answer.response(cid)


# ---------------------------------------------------------------- schedule scan (§4.3)


@app.post("/api/schedule/scan", response_model=ScheduleScanResponse)
async def schedule_scan(request: Request, authorization: Optional[str] = Header(None)):
    s: Settings = request.app.state.settings
    session = _session(request, authorization, "schedule")
    rows: List[AuditEvent] = []
    with obs.span("schedule_scan", session_ref=session.session_ref) as trace:
        try:
            result = await fhir.scan_schedule(request.app.state.fhir, session.access_token, session.fhir_user, date.today(),
                                              Deadline(60.0), _fhir_recorder(request.app.state.fhir, session, rows), obs.correlation_id.get())
        except fhir.FhirUnavailable as e:
            await _audit(request, rows)
            if e.status is LoadStatus.expired:
                raise HTTPException(401, "session_expired: Session expired: sign in again.")
            if e.status is LoadStatus.forbidden:
                raise HTTPException(403, "forbidden: Your account can't view today's schedule.")
            obs.count(obs.Metric.error, kind="schedule_unavailable")
            raise HTTPException(503, "schedule_unavailable: Today's schedule couldn't be loaded.")
        trace.update(metadata=result.counts.model_dump())
    rows.append(_event(AuditEventType.question, session, intent="schedule_scan",
                       outcome=f"checked={result.counts.checked},failed={result.counts.failed},flagged={result.counts.flagged}"))
    await _audit(request, rows)
    return result
