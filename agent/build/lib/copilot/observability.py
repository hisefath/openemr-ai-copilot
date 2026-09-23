"""Correlation ids, JSON logs, Langfuse spans, LLM usage and metric counters without PHI (ARCHITECTURE §7, AUDIT COMP-3,
COMP-7). Only explicit, non-clinical fields are ever recorded: resource types, statuses, counts, ms, tokens, HMAC ids."""
import functools
import hashlib
import hmac
import json
import logging
import os
import re
import uuid
from collections import Counter
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Dict, Iterator, Optional

from opentelemetry import trace as otel_trace

from .config import Settings
from .schemas import LoadStatus

log = logging.getLogger("agent.obs")

correlation_id: ContextVar[str] = ContextVar("correlation_id", default="-")


def new_correlation_id() -> str:
    """Server-generated per request; a client header is never used as the correlation id (COMP-7)."""
    cid = str(uuid.uuid4())
    correlation_id.set(cid)
    return cid


def parse_client_request_id(value: Optional[str]) -> Optional[str]:
    """Kept separately from the correlation id, and only if it is a UUID (canonical form, so nothing else reaches logs)."""
    try:
        return str(uuid.UUID(value)) if value else None
    except (ValueError, TypeError, AttributeError):
        return None


def hmac_id(settings: Settings, value: str) -> str:
    """Pseudonym for patient/user ids sent to Langfuse. Without a key: 'unset', never the raw id."""
    if not settings.hmac_key:
        return "unset"
    return hmac.new(settings.hmac_key.encode(), value.encode(), hashlib.sha256).hexdigest()


def error_code(exc: BaseException) -> str:
    """Exception type plus HTTP status when there is one. Messages may quote chart data, so they are never recorded."""
    status = getattr(exc, "status_code", None) or getattr(getattr(exc, "response", None), "status_code", None)
    return f"{type(exc).__name__} {status}" if isinstance(status, int) else type(exc).__name__


# ---------------------------------------------------------------- logs

_STD_ATTRS = set(vars(logging.makeLogRecord({}))) | {"message", "asctime", "correlation_id"}


class CorrelationFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        record.correlation_id = correlation_id.get()
        return True


class JsonFormatter(logging.Formatter):
    """One JSON object per line (newlines escaped, so no log-line injection). `extra=` keys become fields.
    Tracebacks are reduced to the exception type. asyncio's own messages ('Task exception was never retrieved' plus
    'future: <Task ... exception=...>', 'Exception in callback f(args)') embed reprs of exceptions and arguments, so
    only their leading words are kept (COMP-3)."""

    def format(self, record: logging.LogRecord) -> str:
        msg = record.getMessage()
        if record.name == "asyncio":
            msg = re.split(r"[\n<(\[{'\"=:]", msg, maxsplit=1)[0].strip()
        out: Dict[str, Any] = {
            "ts": datetime.fromtimestamp(record.created, timezone.utc).isoformat(timespec="milliseconds"),
            "level": record.levelname,
            "logger": record.name,
            "msg": msg,
            "correlation_id": getattr(record, "correlation_id", correlation_id.get()),
        }
        out.update({k: v for k, v in vars(record).items() if k not in _STD_ATTRS})
        if record.exc_info and record.exc_info[1] is not None:
            out["error"] = error_code(record.exc_info[1])
        return json.dumps(out, default=str)


def _drop(_: logging.LogRecord) -> bool:
    return False


# httpx logs every request URL at INFO (patient uuid and query in the query string); the others log URLs or bodies at
# DEBUG. fhir.py writes its own id-free line per FHIR call (§7).
URL_LOGGERS = ("httpx", "httpcore", "hpack", "anthropic")


def configure_logging(level: int = logging.INFO) -> None:
    """Access lines carry query strings (OAuth code and state, §2), so they are dropped by a logger filter, which
    survives uvicorn's dictConfig whichever runs first; `disabled` would not (§10)."""
    handler = logging.StreamHandler()
    handler.addFilter(CorrelationFilter())
    handler.setFormatter(JsonFormatter())
    root = logging.getLogger()
    root.handlers = [handler]
    root.setLevel(level)
    for name in URL_LOGGERS:
        logging.getLogger(name).setLevel(logging.WARNING)
    logging.getLogger("uvicorn.access").addFilter(_drop)


# ---------------------------------------------------------------- Langfuse (manual spans only)

class _NoopObservation:
    def update(self, **_: Any) -> "_NoopObservation":
        return self

    def update_trace(self, **_: Any) -> "_NoopObservation":
        return self


NOOP = _NoopObservation()


@functools.cache
def langfuse_client() -> Optional[Any]:
    """None when keys are absent: every span below becomes a no-op (FM-14: Langfuse never blocks an answer)."""
    if not (os.environ.get("LANGFUSE_PUBLIC_KEY") and os.environ.get("LANGFUSE_SECRET_KEY")):
        return None
    from langfuse import Langfuse
    return Langfuse()


@contextmanager
def span(name: str, *, as_type: str = "span", model: Optional[str] = None, **metadata: Any) -> Iterator[Any]:
    """Open a Langfuse observation as the current span, with only the given metadata plus the correlation id.
    Never @observe: in 3.7.0 it captures inputs, outputs and exception text. On exception only `error_code(e)` is
    recorded (OTel exception events and status descriptions are switched off) and the exception is re-raised."""
    lf, obs = langfuse_client(), None
    if lf is not None:
        try:
            obs = lf.start_observation(name=name, as_type=as_type, model=model,
                                       metadata={"correlation_id": correlation_id.get(), **metadata})
        except Exception as e:  # FM-14
            log.error("langfuse span not started", extra={"error": error_code(e)})
    if obs is None:
        yield NOOP
        return
    try:
        # _otel_span: langfuse 3.7.0 exposes no public way to make a manually started observation current.
        with otel_trace.use_span(obs._otel_span, end_on_exit=False, record_exception=False, set_status_on_exception=False):
            yield obs
    except BaseException as e:
        obs.update(level="ERROR", status_message=error_code(e))
        raise
    finally:
        obs.end()


# Claude Haiku 4.5, USD per million tokens (5-minute cache writes).
# ponytail: one price table; add rows if ANTHROPIC_MODEL changes, or costs will be misreported.
PRICE_PER_MTOK = {"input": 1.00, "output": 5.00, "cache_read_input_tokens": 0.10, "cache_creation_input_tokens": 1.25}


def llm_cost_usd(tokens: Dict[str, int]) -> float:
    return sum(tokens.get(k, 0) * price for k, price in PRICE_PER_MTOK.items()) / 1_000_000


def record_llm_usage(obs: Any, response: Any, ms: float) -> Dict[str, int]:
    """Tokens, cost, stop reason and Anthropic request id in one log line and on the generation. Never prompt or
    completion text. Never raises: bookkeeping or Langfuse errors are logged as their type (FM-14)."""
    tokens: Dict[str, int] = {}
    try:
        u = response.usage
        tokens = {"input": u.input_tokens or 0, "output": u.output_tokens or 0,
                  "cache_read_input_tokens": u.cache_read_input_tokens or 0,
                  "cache_creation_input_tokens": u.cache_creation_input_tokens or 0}
        cost = llm_cost_usd(tokens)
        request_id = getattr(response, "_request_id", None)
        log.info("claude call", extra={"request_id": request_id, "stop_reason": response.stop_reason, "ms": round(ms),
                                       "cost_usd": round(cost, 6), **tokens})
        obs.update(usage_details=tokens, cost_details={"total": cost},
                   metadata={"request_id": request_id, "stop_reason": response.stop_reason, "ms": round(ms)})
    except Exception as e:
        log.error("llm usage not recorded", extra={"error": error_code(e)})
    return tokens


# ---------------------------------------------------------------- metrics

class Metric(str, Enum):
    """Definitions from ARCHITECTURE §7."""
    error = "error"                    # HTTP 5xx, or a fallback caused by the LLM, schema or verifier
    fhir_call = "fhir_call"            # denominator for the tool failure rate
    tool_failure = "tool_failure"      # any FHIR call (prefetch or tool) ending in error/timeout
    fhir_forbidden = "fhir_forbidden"  # 403, counted apart from tool failures
    retry = "retry"                    # any repeated FHIR or Claude call
    verification = "verification"      # labelled with the Outcome
    queue_wait = "queue_wait"          # a FHIR call that waited on the OpenEMR semaphore (queue depth > 0)


# ponytail: in-process counters (single replica, §10); export to Prometheus/OTel metrics when replicas > 1.
METRICS: Counter = Counter()
GAUGES: Dict[str, float] = {}


def _langfuse(method: str, **kwargs: Any) -> None:
    lf = langfuse_client()
    if lf is None:
        return
    try:
        getattr(lf, method)(**kwargs)
    except Exception as e:  # FM-14
        log.error("langfuse metric not recorded", extra={"error": error_code(e)})


def count(metric: Metric, n: int = 1, **labels: str) -> None:
    """One Langfuse event per count, under the current span, so dashboards and alerts count events by name instead of
    reading span metadata that the next count on the same span would overwrite (§7)."""
    key = metric.value + "".join(f",{k}={v}" for k, v in sorted(labels.items()))
    METRICS[key] += n
    _langfuse("create_event", name=f"metric.{metric.value}", metadata={**labels, "n": n})


def count_fhir(resource_type: str, status: LoadStatus) -> None:
    count(Metric.fhir_call, resource=resource_type)
    if status in (LoadStatus.error, LoadStatus.timeout):
        count(Metric.tool_failure, resource=resource_type, status=status.value)
    elif status is LoadStatus.forbidden:
        count(Metric.fhir_forbidden, resource=resource_type)


def set_queue_depth(waiters: int) -> None:
    """Waiters on the OpenEMR semaphore (§3), also on the current span so the dashboard sees it."""
    GAUGES["openemr_queue_depth"] = waiters
    _langfuse("update_current_span", metadata={"openemr_queue_depth": waiters})
