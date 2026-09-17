"""Clinical Co-Pilot agent: FastAPI + Claude tool loop + citation verification + Langfuse."""
import asyncio
import contextvars
import json
import logging
import os
import time
import uuid
from contextlib import asynccontextmanager
from typing import Dict, List, Optional

import httpx
from anthropic import AsyncAnthropic
from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import JSONResponse
from langfuse import get_client, observe
from pydantic import BaseModel, Field

from fhir import FHIR_BASE, PATIENT_ID, TOOL_SPECS, TOOLS, PatientInput
from verify import Briefing, VerifiedClaim, verify

MODEL = os.environ.get("ANTHROPIC_MODEL", "claude-haiku-4-5")
MAX_TOOL_ROUNDS = 5
SYSTEM = (
    "You are a clinical co-pilot giving a physician a fast briefing on ONE patient. "
    "Use the tools to read the chart; never state a clinical fact you did not read from a tool result. "
    "Every claim must cite the source_id of the record it came from and copy the exact supporting value "
    "into quoted_value. If the chart does not contain something, say so rather than guessing. "
    "No diagnosis or prescribing advice."
)

# --- correlation IDs: one per request, on every log line, tool call and LLM call ---
correlation_id = contextvars.ContextVar("correlation_id", default="-")


class _CidFilter(logging.Filter):
    def filter(self, record):
        record.cid = correlation_id.get()
        return True


_h = logging.StreamHandler()
_h.addFilter(_CidFilter())
_h.setFormatter(logging.Formatter("%(asctime)s %(levelname)s cid=%(cid)s %(message)s"))
log = logging.getLogger("agent")
log.addHandler(_h)
log.setLevel(logging.INFO)


@asynccontextmanager
async def lifespan(app: FastAPI):
    app.state.http = httpx.AsyncClient(timeout=15)
    app.state.llm = AsyncAnthropic()
    yield
    await app.state.http.aclose()
    get_client().flush()


app = FastAPI(title="Clinical Co-Pilot Agent", lifespan=lifespan)


@app.middleware("http")
async def add_correlation_id(request: Request, call_next):
    cid = request.headers.get("X-Correlation-ID") or str(uuid.uuid4())
    correlation_id.set(cid)
    start = time.perf_counter()
    response = await call_next(request)
    response.headers["X-Correlation-ID"] = cid
    log.info("%s %s -> %s in %.0fms", request.method, request.url.path, response.status_code, (time.perf_counter() - start) * 1000)
    return response


@app.get("/health")
async def health():
    return {"status": "ok"}


@app.get("/ready")
async def ready():
    checks = {}

    async def check(name, coro):
        try:
            await coro
            checks[name] = "ok"
        except Exception as e:  # report which dependency is down, never its secrets
            checks[name] = f"fail: {type(e).__name__}"

    async def fhir():
        (await app.state.http.get(f"{FHIR_BASE}/metadata")).raise_for_status()

    async def langfuse():
        if not await run_in_threadpool(get_client().auth_check):
            raise RuntimeError("auth_check false")

    await asyncio.gather(
        check("openemr_fhir", fhir()),
        check("anthropic", app.state.llm.models.retrieve(MODEL)),
        check("langfuse", langfuse()),
    )
    ok = all(v == "ok" for v in checks.values())
    return JSONResponse({"ready": ok, "checks": checks}, status_code=200 if ok else 503)


class ChatRequest(BaseModel):
    patient_id: str = PATIENT_ID
    message: str = Field(min_length=1, max_length=4000)
    session_id: Optional[str] = None


class ChatResponse(BaseModel):
    session_id: str
    correlation_id: str
    answer: str
    claims: List[VerifiedClaim]
    verification_passed: bool


# ponytail: in-process session memory; lost on restart and not shared across replicas. Move to Redis when scaling out.
SESSIONS: Dict[str, dict] = {}


async def _run_tool(block, patient_id: str, token: str, fetched: Dict[str, dict]) -> dict:
    try:
        inp = PatientInput(**block.input)
        if inp.patient_id != patient_id:
            raise ValueError("tools are locked to the patient in the open chart")
        rows = await TOOLS[block.name](app.state.http, token, inp)
        records = [r.model_dump() for r in rows]
        fetched.update({r["source_id"]: r for r in records})
        log.info("tool %s returned %d records", block.name, len(records))
        return {"type": "tool_result", "tool_use_id": block.id, "content": json.dumps(records)}
    except Exception as e:
        log.warning("tool %s failed: %s", block.name, type(e).__name__)
        return {"type": "tool_result", "tool_use_id": block.id, "content": f"error: {type(e).__name__}", "is_error": True}


@app.post("/chat", response_model=ChatResponse)
@observe(name="chat", capture_input=False)  # input includes the bearer token
async def chat(req: ChatRequest, authorization: str = Header(...)):
    if not authorization.startswith("Bearer "):
        raise HTTPException(401, "Bearer token required")
    session_id = req.session_id or str(uuid.uuid4())
    session = SESSIONS.setdefault(session_id, {"patient_id": req.patient_id, "messages": [], "fetched": {}})
    if session["patient_id"] != req.patient_id:
        raise HTTPException(409, "session belongs to a different patient")
    cid = correlation_id.get()
    lf = get_client()
    lf.update_current_trace(session_id=session_id, metadata={"correlation_id": cid, "patient_id": req.patient_id})

    messages = session["messages"]
    messages.append({"role": "user", "content": f"[patient_id={req.patient_id}] {req.message}"})

    briefing = None
    for _ in range(MAX_TOOL_ROUNDS + 1):
        with lf.start_as_current_generation(name="claude", model=MODEL, metadata={"correlation_id": cid}) as gen:
            resp = await app.state.llm.messages.parse(
                model=MODEL, max_tokens=4000, system=SYSTEM, tools=TOOL_SPECS,
                messages=messages, output_format=Briefing,
            )
            gen.update(usage_details={"input": resp.usage.input_tokens, "output": resp.usage.output_tokens},
                       output=resp.stop_reason)
        messages.append({"role": "assistant", "content": resp.content})
        if resp.stop_reason != "tool_use":
            briefing = resp.parsed_output
            break
        blocks = [b for b in resp.content if b.type == "tool_use"]
        results = await asyncio.gather(*(_run_tool(b, req.patient_id, authorization, session["fetched"]) for b in blocks))
        messages.append({"role": "user", "content": list(results)})

    if briefing is None:
        log.error("no briefing (stop_reason=%s)", resp.stop_reason)
        raise HTTPException(502, "agent did not produce an answer")

    claims = verify(briefing, session["fetched"])
    passed = all(c.verified for c in claims)
    log.info("verification %s (%d/%d claims verified)", "pass" if passed else "fail", sum(c.verified for c in claims), len(claims))
    lf.score_current_trace(name="verification_passed", value=1.0 if passed else 0.0)
    return ChatResponse(session_id=session_id, correlation_id=cid, answer=briefing.answer, claims=claims, verification_passed=passed)
