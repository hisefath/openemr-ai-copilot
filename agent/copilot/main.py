"""Clinical Co-Pilot agent: HTTP wiring for ARCHITECTURE §2 (launch, sessions), §4.2 (question flow),
§4.3 (schedule scan), §7 (correlation, metrics, audit) and §8 (API contract)."""
import asyncio
import json
import logging
import os
import re
import time
from contextlib import asynccontextmanager
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

import anthropic
import httpx
from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles

from . import audit
from . import emr_write
from . import fhir
from . import llm
from . import locate
from . import observability as obs
from . import render
from . import rules
from . import smart
from . import staging
from . import verify
from . import w2_routes
from .config import Settings
from .deadline import Deadline
from .schemas import (AuditEvent, AuditEventType, EncountersInput, ErrorBody, ErrorResponse, LabHistoryInput, LoadStatus,
                      MessageRequest, MessageResponse, Outcome, PatientContext, ScheduleScanResponse, SessionCreateRequest,
                      SessionCreateResponse, SessionStatus, ToolName)
from .sessions import Session, SessionStore, Turn

log = logging.getLogger("agent")
STATIC = Path(__file__).parent / "static"
PREFETCH_BUDGET_S = 12.0      # launch prefetch runs in the background, not inside a question's deadline
FRESH_S = 120                 # §3 freshness: refetch anything older before answering
LLM_RESERVE_S = 4.0           # a question waits for a running prefetch only while this much deadline remains
AUDIT_TIMEOUT_S = 3.0
PATIENT_TOOLS = [ToolName.get_lab_history, ToolName.get_encounters]  # §3: one round, patient sessions only
# The server, not the model, decides whether a question can need older records (UC3 older visits, UC4 trends). Haiku
# called a tool on every question when offered, adding a second call that pushed answers past the deadline.
# Trends no longer need a tool: the server fetches a requested trend's history itself (_fetch_trend_history).
HISTORY_QUESTION = re.compile(r"\b(older|earlier|years? ago|months? ago|back in|in (?:19|20)\d{2}|since (?:19|20)\d{2}|"
                              r"(?:19|20)\d{2}|long ago|first (?:visit|diagnos\w*))\b", re.I)


def tools_for(question: str) -> List[ToolName]:
    return PATIENT_TOOLS if HISTORY_QUESTION.search(question) else []
TOOL_WINDOWS = {"get_lab_history": "labs", "get_encounters": "encounters"}


def _epoch_iso(ts: float) -> str:
    return datetime.fromtimestamp(ts, timezone.utc).isoformat(timespec="seconds")


async def _warm_up_structured_output(client: anthropic.AsyncAnthropic, settings: Settings) -> None:
    """The first request for a new output schema + tool set waits while Anthropic compiles the grammar (measured: 20 s
    cold vs 1.4 s warm, AUDIT OPS-8), which would push the first physician's question past the 9 s deadline into the
    fallback. One synthetic request with the production request shape at startup pays that cost instead. No PHI."""
    async def no_tool(*_):
        return "", False
    try:
        for tools in (PATIENT_TOOLS, []):  # both request shapes main.py sends compile their own grammar
            _, meta = await llm.plan_answer(client, settings, llm.SYSTEM_PROMPT, '{"resources":{}}', "Brief me",
                                            tools, Deadline(60.0), no_tool)
            log.info("structured output warm-up", extra={"tools": len(tools), "reason": meta.reason, "calls": meta.calls,
                                                          "cost_usd": round(meta.cost_usd, 5)})
    except Exception as e:  # never blocks startup
        log.warning("structured output warm-up failed", extra={"error": obs.error_code(e)})


@asynccontextmanager
async def lifespan(app: FastAPI):
    obs.configure_logging()
    settings = Settings.from_env()
    ca_pem = os.environ.get("AUDIT_DB_CA_PEM")
    if ca_pem and settings.audit_db_ca and not os.path.exists(settings.audit_db_ca):
        # Railway variables are strings; the MySQL CA (a public certificate) becomes the file PyMySQL verifies against.
        Path(settings.audit_db_ca).write_text(ca_pem if ca_pem.endswith("\n") else ca_pem + "\n")
    app.state.settings = settings
    app.state.http = httpx.AsyncClient(timeout=smart.OPENEMR_TIMEOUT)
    app.state.fhir = fhir.FhirClient(app.state.http, settings.fhir_base, settings.openemr_concurrency)
    app.state.llm = anthropic.AsyncAnthropic(max_retries=0)
    app.state.store = SessionStore()
    app.state.states = smart.StateStore()
    app.state.locks: Dict[str, asyncio.Lock] = {}
    # Week 2. The write client and the review queue are separate from the Week 1 read path on purpose: nothing
    # on the question path can reach either of them.
    app.state.emr_write = emr_write.EmrWriteClient(app.state.http, settings.fhir_base, settings.openemr_concurrency)
    app.state.staging = staging.MemoryStagingStore()
    app.state.pages_cache: Dict[str, Any] = {}      # document_id -> rendered pages, for the citation overlay
    app.state.session_docs: Dict[str, Any] = {}     # session_ref -> the document this session ingested
    app.state.session_resolver = _session            # w2_routes reuses this session check, never its own
    app.state.retriever = _build_retriever(settings)
    if settings.audit_db_host:
        app.state.audit = audit.AuditWriter(settings)
    else:  # local development without the audit database: rows go to the log instead (never in production)
        log.warning("AUDIT_DB_HOST not set: audit rows are logged, not stored")
        app.state.audit = audit.FakeAuditWriter()
    app.state.pages = {"patient": smart.load_page(STATIC / "panel.html"), "schedule": smart.load_page(STATIC / "schedule.html")}
    if os.environ.get("LLM_WARMUP", "true").lower() != "false":
        app.state.warmup = asyncio.create_task(_warm_up_structured_output(app.state.llm, settings))
    yield
    await app.state.http.aclose()
    lf = obs.langfuse_client()
    if lf is not None:
        lf.flush()


def _build_retriever(settings: Settings):
    """Corpus and vectors ship with the package, so this is free and offline. Voyage is used only if a key is
    set; without one the dense half and the reranker are absent and retrieval reports itself unavailable rather
    than quietly returning unranked chunks."""
    from . import retrieve

    try:
        chunks = retrieve.load_corpus()
        vectors = json.loads((Path(retrieve.CORPUS).parent / "vectors.json").read_text())["vectors"]
    except Exception as e:
        log.warning("corpus_unavailable", extra={"error": type(e).__name__})
        return None
    key = os.environ.get("VOYAGE_API_KEY")
    provider = retrieve.VoyageProvider(key) if key else None
    return retrieve.HybridRetriever(chunks, vectors, provider, provider)


app = FastAPI(title="Clinical Co-Pilot Agent", lifespan=lifespan)
app.include_router(w2_routes.router)
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
    # OCR is a CAPABILITY REPORT, not a readiness gate. Tesseract is an OS package that agent/Dockerfile installs;
    # a build that misses it — an auto-detected builder, a changed base image — starts perfectly and then silently
    # returns "could not be located" for every value on a scanned page, which reads as a design choice rather than
    # a broken deploy. Surfacing it here is what lets a deploy check catch that. It must NOT 503 the service:
    # a PDF with a text layer still extracts correctly without tesseract, so the app is genuinely ready.
    checks["ocr"] = "ok" if locate.tesseract_available() else "unavailable"
    ok = all(v == "ok" for k, v in checks.items() if k != "ocr")
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
        # Queue depth as a countable event, not only a span attribute: the dashboard can chart "calls that had to
        # wait" alongside the other counts, and a rising share is the saturation signal ARCHITECTURE §7 asks for.
        if (e.get("queue_ms") or 0) > 0:
            obs.count(obs.Metric.queue_wait, resource=e["resource"])
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
                if task.exception().status_code >= 500:  # this request fails too; A2 counts errors per request
                    obs.count(obs.Metric.error, kind=str(task.exception().detail).split(":", 1)[0])
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


TREND_YEARS = 5
TREND_FETCH_MIN_S = 2.5
MAX_TREND_FETCHES = 2


def _unalias(plan, aliases: Dict[str, str]):
    """Map the model's short refs back to FHIR source ids. An unknown ref stays as written, so verification withholds it
    and audits it as denied, exactly like an invented id."""
    if plan is None:
        return None
    back = {v: k for k, v in aliases.items()}
    items = [i.model_copy(update={"source_id": back.get(i.source_id, i.source_id)}) if getattr(i, "kind", None) == "record"
             else i for i in plan.items]
    clarify = plan.clarify.model_copy(update={"candidate_source_ids": [back.get(x, x) for x in plan.clarify.candidate_source_ids]}) \
        if plan.clarify else None
    return plan.model_copy(update={"items": items, "clarify": clarify})


def _history_text(session: Session, aliases: Dict[str, str]) -> str:
    """Prior turns as the physician saw them (questions + server-rendered lines with sources), never raw model output."""
    turns = [{"question": t.question, "shown": [{"text": ln.text, "source_ids": [aliases.get(x, x) for x in ln.source_ids]}
                                                for ln in t.lines]}
             for t in list(session.history)[-render.HISTORY_TURNS:]]
    return json.dumps(turns, separators=(",", ":")).replace("<", "\\u003c").replace(">", "\\u003e") if turns else ""


async def _fetch_trend_history(request: Request, session: Session, ctx: PatientContext, plan, deadline: Deadline,
                               rows: List[AuditEvent], today: date) -> PatientContext:
    """A requested trend gets the lab's full recent history from the server, not only the 18-month prefetch, and not
    only when the model remembers to call a tool (UC4). Same patient-locked, audited FHIR path; skipped when time is short."""
    labs = list(dict.fromkeys(i.lab for i in plan.items if getattr(i, "kind", None) == "trend"))[:MAX_TREND_FETCHES]
    if not labs or not deadline.has(TREND_FETCH_MIN_S):
        return ctx
    since = today.replace(year=today.year - TREND_YEARS).isoformat()
    budget = Deadline(deadline.remaining() - 2.0)
    loads = await asyncio.gather(*(fhir.get_lab_history(request.app.state.fhir, session.access_token, session.patient_id,
                                                        LabHistoryInput(lab=lab[:80], since=since), budget,
                                                        _fhir_recorder(request.app.state.fhir, session, rows),
                                                        obs.correlation_id.get()) for lab in labs))
    known = {sid for r in ctx.labs.records for sid in r.source_ids}
    new = [r for load in loads if load.status is LoadStatus.ok for r in load.records if not set(r.source_ids) & known]
    return ctx.model_copy(update={"labs": ctx.labs.model_copy(update={"records": ctx.labs.records + new})}) if new else ctx


@app.post("/api/session/messages", response_model=MessageResponse)
async def ask(request: Request, body: MessageRequest, authorization: Optional[str] = Header(None)):
    s: Settings = request.app.state.settings
    session = _session(request, authorization, "patient")
    lock = request.app.state.locks.setdefault(session.session_ref, asyncio.Lock())
    if lock.locked():
        raise HTTPException(409, "busy: Another question is still being answered.")
    async with lock:
        return await _answer(request, s, session, body)


async def _run_w2_graph(request: Request, session: Session, question: str, deadline: Deadline) -> dict:
    """Route this turn through the Week 2 graph and return what it gathered.

    The graph owns the Week 2 concerns — is there an unread document, does this question need guideline
    evidence, is it in scope — and hands back. The answer itself is still produced by Week 1's plan-and-verify
    path, which is the part that has been proven for a week; the graph does not get to render anything.

    Wrapped so it cannot take the answer down with it. A failure here means no evidence and no handoff record,
    which degrades the answer; an exception would have meant no answer at all, and the Week 1 path works
    perfectly well without any of this.
    """
    state = request.app.state
    held = (getattr(state, "session_docs", {}) or {}).get(session.session_ref) or {}
    try:
        from . import graph as graph_mod

        deps = graph_mod.Deps(state.llm, state.settings, getattr(state, "retriever", None))
        gs: graph_mod.GraphState = {
            "session_ref": session.session_ref, "patient_id": session.patient_id, "question": question,
            "document": held.get("document"), "pages": held.get("pages"), "extracted": held.get("extracted"),
            "evidence": [], "handoffs": [], "deadline": deadline,
            "correlation_id": obs.correlation_id.get(),
            "prior_turn": {"answered": True} if session.history else None,
        }
        decision = await graph_mod.supervisor(gs, deps)
        gs.update(decision)
        if decision.get("_route") == "retrieve":
            gs.update(await graph_mod.evidence_retriever(gs, deps))
        return {"evidence": gs.get("evidence") or [], "handoffs": gs.get("handoffs") or [],
                "extracted": held.get("extracted")}
    except Exception as e:
        log.warning("w2_graph_unavailable", extra={"error": obs.error_code(e)})
        return {"evidence": [], "handoffs": [], "extracted": held.get("extracted")}


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

        # History stays out of the cached chart block (it changes every turn, which would defeat the cache).
        aliases: Dict[str, str] = {}
        model_context = render.build_model_context(ctx, flags, [], ctx.fetched_at, today, fenced=False, aliases=aliases)
        plan, meta = await llm.plan_answer(request.app.state.llm, s, llm.SYSTEM_PROMPT, model_context, body.question,
                                           tools_for(body.question), deadline, run_tool,
                                           history=_history_text(session, aliases))
        plan = _unalias(plan, aliases)
        if plan is not None:
            ctx = await _fetch_trend_history(request, session, ctx, plan, deadline, rows, today)
        w2 = await _run_w2_graph(request, session, body.question, deadline)
        answer = verify.verify_and_render(plan, ctx, body.question, body.selected_source_id, flags, today, now,
                                          w2_index=render.evidence_index(w2["extracted"], w2["evidence"]))
        answer = answer.model_copy(update={"handoffs": w2["handoffs"], "evidence": w2["evidence"]})

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

        log.info("answer", extra={"outcome": answer.outcome.value, "withheld": answer.withheld_count,
                                  "flags": len(answer.flags), "intent": plan.intent.value if plan else None,
                                  "llm_reason": meta.reason, "llm_calls": meta.calls, "llm_cost_usd": round(meta.cost_usd, 6),
                                  "input_tokens": meta.tokens.get("input", 0), "output_tokens": meta.tokens.get("output", 0),
                                  "tools_run": meta.tools_run, "elapsed_s": round(s.question_deadline_s - deadline.remaining(), 2)})
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
