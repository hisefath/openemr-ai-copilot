"""OpenEMR FHIR R4 access: the rate-limited client, launch prefetch, patient tools and the schedule scan
(ARCHITECTURE §3, §4.3). Every call carries the physician's own token (user/ scopes), so OpenEMR's role ACL decides
what is visible. The patient always comes from the server-side session or today's appointment list, never from
model input."""
import asyncio
import calendar
import logging
import re
import time
from datetime import date, datetime, timezone
from functools import partial
from typing import Any, Awaitable, Callable, Coroutine, Dict, Iterable, List, NamedTuple, Optional, Tuple

import httpx

import normalize
import rules
from deadline import Deadline
from schemas import (EncounterRecord, EncountersInput, LabHistoryInput, LabRecord, LoadStatus, PatientBanner,
                     PatientContext, PatientRecord, ResourceLoad, ScanCounts, ScanPatient, ScheduleScanResponse)

# httpx logs every request URL, query string and patient uuid included, at INFO (COMP-3); call events replace it.
for _name in ("httpx", "httpcore"):
    logging.getLogger(_name).setLevel(logging.WARNING)

OnEvent = Callable[[Dict[str, Any]], None]
Get = Callable[[str, Optional[dict]], Awaitable["Fetch"]]

RETRY_MIN_S = 3.0
# Deviation from §4.3's allowlist: OpenEMR maps its default status '-' to `proposed` and unknown codes to `pending`
# (FhirAppointmentService), so an allowlist skips ordinary visits. Skip only visits that won't happen or are done.
NOT_LIVE_APPOINTMENT = {"cancelled", "noshow", "entered-in-error", "fulfilled", "waitlist"}
SCAN_PATIENTS_IN_FLIGHT = 2  # ≤ 8 scan calls wait on the shared FIFO semaphore, so chart launches interleave (§3)
# Also the priority order: semaphore waiters are served FIFO, so allergies and medications reach OpenEMR first (§3).
ORDER = ("allergies", "medications", "patient", "conditions", "labs", "vitals", "encounters")
SCAN_RESOURCES = ("allergies", "medications", "labs", "patient")
PATIENT_MISMATCH = "patient mismatch"  # event/load detail; the session layer writes a `denied` audit row (FM-09)
_ID = re.compile(r"[A-Za-z0-9-]{1,64}")  # FHIR uuids; no '/' or '.', so an id can't change the URL path
_OK = (LoadStatus.ok, LoadStatus.empty)
_TRANSIENT = (LoadStatus.error, LoadStatus.timeout)


class Fetch(NamedTuple):
    status: LoadStatus
    bundle: Optional[dict] = None  # a searchset, or a read wrapped as a one-entry bundle
    detail: Optional[str] = None   # non-PHI reason: 'HTTP 500', 'ReadTimeout', 'deadline', 'patient mismatch'


class FhirUnavailable(Exception):
    """Today's appointment list could not be read. Raised so a failed scan never renders as 'nothing scheduled'."""

    def __init__(self, status: LoadStatus, detail: Optional[str]):
        super().__init__(f"{status.value}: {detail}")
        self.status, self.detail = status, detail


class FhirClient:
    """One per process. The semaphore sits below OpenEMR's PHP worker count (§3, PERF-1); queue_depth is the §7 metric."""

    def __init__(self, http: httpx.AsyncClient, base: str, concurrency: int = 6):
        self._http, self._base = http, base.rstrip("/")
        self._base_path = httpx.URL(self._base).path.rstrip("/")
        self._sem = asyncio.Semaphore(concurrency)
        self.queue_depth = 0

    async def get(self, path: str, params: Optional[dict], *, token: str, deadline: Deadline, correlation_id: str,
                  on_event: OnEvent, patient_id: Optional[str]) -> Fetch:
        """GET {base}/{path}. Never raises for transport, HTTP or body failures: they become a LoadStatus (§3 table).
        Retries once, only on a connection error, only if RETRY_MIN_S remain. The body is checked (resource type and,
        unless patient_id is None for the Appointment search, the §1 patient lock) before the event is emitted, so a
        cross-patient response is recorded as an error, never as ok.

        The event carries no URL, query string or resource id. `audit_patient_id` is the raw uuid for the audit row
        only (§7); consumers HMAC it before logs or Langfuse. `ms` is OpenEMR time, `queue_ms` the semaphore wait."""
        resource, _, rid = path.partition("/")
        start, acquired, retried, http_status = time.monotonic(), None, False, None
        try:
            async with asyncio.timeout(deadline.remaining()):  # bounds the queue wait too
                self.queue_depth += 1
                try:
                    await self._sem.acquire()
                finally:
                    self.queue_depth -= 1
                acquired = time.monotonic()
                try:
                    for attempt in (0, 1):
                        if deadline.expired():
                            raise TimeoutError
                        r = deadline.remaining()
                        try:
                            resp = await self._http.get(
                                f"{self._base}/{path}", params=params,
                                headers={"Authorization": f"Bearer {token}", "Accept": "application/fhir+json",
                                         "X-Correlation-ID": correlation_id},
                                timeout=httpx.Timeout(connect=min(1.0, r), read=min(3.0, r), write=min(3.0, r),
                                                      pool=min(1.0, r)))
                            break
                        except (httpx.ConnectError, httpx.ConnectTimeout):
                            if attempt or not deadline.has(RETRY_MIN_S):
                                raise
                            retried = True
                finally:
                    self._sem.release()
            http_status = resp.status_code
            fetch = _classify(resp, resource, patient_id)
        except TimeoutError:
            fetch = Fetch(LoadStatus.timeout, detail="deadline")
        except httpx.TimeoutException as e:  # connect (after the retry policy), read, write, pool
            fetch = Fetch(LoadStatus.timeout, detail=type(e).__name__)
        except httpx.HTTPError as e:  # connection failures after the retry policy, protocol errors
            fetch = Fetch(LoadStatus.error, detail=type(e).__name__)
        end = time.monotonic()
        held = end if acquired is None else acquired
        on_event({"method": "GET", "resource": resource, "path": f"{self._base_path}/{resource}" + ("/{id}" if rid else ""),
                  "status": fetch.status.value, "detail": fetch.detail, "http_status": http_status,
                  "count": len(normalize.resources(fetch.bundle)), "ms": round((end - held) * 1000),
                  "queue_ms": round((held - start) * 1000), "retried": retried, "audit_patient_id": patient_id})
        return fetch


def _classify(resp: httpx.Response, resource: str, patient_id: Optional[str]) -> Fetch:
    code = resp.status_code
    if code in (401, 403):
        return Fetch(LoadStatus.expired if code == 401 else LoadStatus.forbidden, detail=f"HTTP {code}")
    if code != 200:
        return Fetch(LoadStatus.error, detail=f"HTTP {code}")
    try:
        body = resp.json()
    except ValueError:
        body = None
    kind = body.get("resourceType") if isinstance(body, dict) else None
    if kind in (None, "OperationOutcome"):
        return Fetch(LoadStatus.error, detail="unparseable body")
    bundle = body if kind == "Bundle" else {"resourceType": "Bundle", "entry": [{"resource": body}]}
    entries = bundle.get("entry") or []
    if not isinstance(entries, list) or not all(isinstance(e, dict) and isinstance(e.get("resource"), dict)
                                                for e in entries):
        return Fetch(LoadStatus.error, detail="unparseable body")
    rows = normalize.resources(bundle)
    if any(r.get("resourceType") != resource for r in rows):
        return Fetch(LoadStatus.error, detail="unexpected resource type")
    if patient_id is not None and any(_patient_of(r) != patient_id for r in rows):
        return Fetch(LoadStatus.error, detail=PATIENT_MISMATCH)
    return Fetch(LoadStatus.ok if rows else LoadStatus.empty, bundle)


def _patient_of(r: dict) -> Optional[str]:
    """The patient a resource belongs to; None (a mismatch: the lock fails closed) when it names none."""
    if r.get("resourceType") == "Patient":
        return r.get("id")
    ref = r.get("subject") or r.get("patient")
    parts = ref.get("reference").split("/")[-2:] if isinstance(ref, dict) and isinstance(ref.get("reference"), str) else []
    return parts[1] if len(parts) == 2 and parts[0] == "Patient" else None


# ---------------------------------------------------------------- normalization into loads

def _patients(bundle: dict) -> List[PatientRecord]:
    """normalize.patient plus the MRN: the identifier whose type is v2-0203 'PT' (see tests/fixtures)."""
    recs = normalize.patient(bundle)
    for rec, res in zip(recs, normalize.resources(bundle)):
        rec.mrn = next((i.get("value") for i in res.get("identifier") or [] if isinstance(i, dict)
                        and any(isinstance(c, dict) and c.get("code") == "PT"
                                for c in (i.get("type") or {}).get("coding") or [])), None)
    return recs


_NORMALIZE: Dict[str, Callable[[dict, Optional[date]], list]] = {
    "patient": lambda b, today: _patients(b),
    "allergies": lambda b, today: normalize.allergies(b),
    "medications": normalize.medications,
    "conditions": lambda b, today: normalize.conditions(b),
    "labs": lambda b, today: normalize.labs(b),
    "vitals": lambda b, today: normalize.vitals(b),
    "encounters": lambda b, today: normalize.encounters(b),
}


def _load(key: str, fetch: Fetch, today: Optional[date]) -> ResourceLoad:
    if fetch.status not in _OK:
        return ResourceLoad(status=fetch.status, detail=fetch.detail)
    try:
        records = _NORMALIZE[key](fetch.bundle, today)
    except (AttributeError, LookupError, TypeError, ValueError):  # a field shape normalize can't read: this resource only
        return ResourceLoad(status=LoadStatus.error, detail="unparseable body")
    # Status follows what survives normalization: only entered-in-error rows means none recorded.
    return ResourceLoad(status=LoadStatus.ok if records else LoadStatus.empty, records=records)


def _months_ago(d: date, months: int) -> date:
    y, m = divmod(d.year * 12 + d.month - 1 - months, 12)
    return date(y, m + 1, min(d.day, calendar.monthrange(y, m + 1)[1]))


def _query(key: str, pid: str, today: date, lab_months: int = 18) -> Tuple[str, Optional[dict]]:
    """The fixed §3 queries. OpenEMR has no server paging, so the date bound is the only limit (PERF-4)."""
    p = {"patient": pid}
    return {
        "patient": (f"Patient/{pid}", None),
        "allergies": ("AllergyIntolerance", p),
        "medications": ("MedicationRequest", p),
        "conditions": ("Condition", p),  # no category: problem-list-item drops encounter-linked problems (ARCH-M2)
        "labs": ("Observation", {**p, "category": "laboratory", "date": f"ge{_months_ago(today, lab_months)}"}),
        "vitals": ("Observation", {**p, "category": "vital-signs", "date": f"ge{_months_ago(today, 12)}"}),
        "encounters": ("Encounter", {**p, "date": f"ge{_months_ago(today, 24)}"}),
    }[key]


def _bind(client: FhirClient, token: str, deadline: Deadline, on_event: OnEvent, correlation_id: str,
          patient_id: str) -> Get:
    """client.get locked to one patient. The id must be a FHIR uuid: None or '' would send `patient=` and could pull
    every patient's rows into memory and OpenEMR's api_log (PERF-3) before the lock rejects them."""
    if not (isinstance(patient_id, str) and _ID.fullmatch(patient_id)):
        raise ValueError("invalid patient id")
    return partial(client.get, token=token, deadline=deadline, correlation_id=correlation_id, on_event=on_event,
                   patient_id=patient_id)


async def _run_all(coros: Iterable[Coroutine[Any, Any, Any]]) -> list:
    """Results in order. If one raises (e.g. an audit write in on_event, FM-15), the rest are cancelled before that
    exception is re-raised as itself, so no orphaned call keeps a semaphore slot or emits events."""
    try:
        async with asyncio.TaskGroup() as tg:
            tasks = [tg.create_task(c) for c in coros]
    except BaseExceptionGroup as eg:
        raise eg.exceptions[0]
    return [t.result() for t in tasks]


async def _load_many(get: Get, pid: str, today: date, wanted: Iterable[str], lab_months: int = 18) -> Dict[str, ResourceLoad]:
    wanted = set(wanted)
    keys = [k for k in ORDER if k in wanted]
    fetches = await _run_all(get(*_query(k, pid, today, lab_months)) for k in keys)
    return {k: _load(k, f, today) for k, f in zip(keys, fetches)}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# ---------------------------------------------------------------- patient sessions

async def prefetch(client: FhirClient, token: str, patient_id: str, today: date, deadline: Deadline,
                   on_event: OnEvent, correlation_id: str) -> PatientContext:
    """Launch prefetch (§3): all seven resource types in parallel through the client's semaphore."""
    pending = ResourceLoad(status=LoadStatus.pending)
    context = PatientContext(patient_id=patient_id, fetched_at=_now(), **{k: pending for k in ORDER})
    return await refresh(context, ORDER, client, token, today, deadline, on_event, correlation_id)


async def refresh(context: PatientContext, which: Iterable[str], client: FhirClient, token: str, today: date,
                  deadline: Deadline, on_event: OnEvent, correlation_id: str) -> PatientContext:
    """Refetch the PatientContext fields named in `which`, allergies and medications first (§3 Freshness).
    Every refetched field takes the new status and detail, so a failed refresh renders as 'unavailable right now'.
    On error/timeout the previous records are kept under that status, so rules still see allergies already loaded;
    401/403 and a patient mismatch drop them. fetched_at moves only when all seven were refetched."""
    wanted = None if isinstance(which, str) else set(which)  # a bare 'labs' would be a set of letters
    if wanted is None or not wanted <= set(ORDER):
        raise ValueError("which must name PatientContext resources")
    started = _now()
    get = _bind(client, token, deadline, on_event, correlation_id, context.patient_id)
    fields = dict(context)
    for key, load in (await _load_many(get, context.patient_id, today, wanted)).items():
        if load.status in _TRANSIENT and load.detail != PATIENT_MISMATCH:
            load = ResourceLoad(status=load.status, detail=load.detail, records=fields[key].records)
        fields[key] = load
    if wanted == set(ORDER):
        fields["fetched_at"] = started
    return PatientContext(**fields)


async def get_lab_history(client: FhirClient, token: str, patient_id: str, inp: LabHistoryInput, deadline: Deadline,
                          on_event: OnEvent, correlation_id: str) -> ResourceLoad[LabRecord]:
    """UC4 tool: labs since `inp.since` whose LOINC code or name equals `inp.lab` (case-insensitive).
    The patient is the session's; the input has no patient field (§3 Tools)."""
    get = _bind(client, token, deadline, on_event, correlation_id, patient_id)
    want = inp.lab.strip().lower()
    if want in ("", "lab"):  # '' equals every uncoded lab's code; 'Lab' is normalize's fallback name, not a test
        return ResourceLoad(status=LoadStatus.empty)
    load = _load("labs", await get("Observation", {"patient": patient_id, "category": "laboratory",
                                                   "date": f"ge{inp.since}"}), None)
    if load.status not in _OK:
        return load
    records = [r for r in load.records if want in ((r.loinc or "").lower(), r.name.lower())]
    return ResourceLoad(status=LoadStatus.ok if records else LoadStatus.empty, records=records)


async def get_encounters(client: FhirClient, token: str, patient_id: str, inp: EncountersInput, deadline: Deadline,
                         on_event: OnEvent, correlation_id: str) -> ResourceLoad[EncounterRecord]:
    """UC3 tool: encounters since `inp.since`, newest first. The patient is the session's (§3 Tools)."""
    get = _bind(client, token, deadline, on_event, correlation_id, patient_id)
    return _load("encounters", await get("Encounter", {"patient": patient_id, "date": f"ge{inp.since}"}), None)


def patient_banner(record: Optional[PatientRecord], today: date) -> PatientBanner:
    """On-screen identity (§2 'Patient identity on screen'); never sent to the model."""
    if record is None:
        return PatientBanner(name="Patient details unavailable")
    try:
        b = date.fromisoformat((record.birth_date or "")[:10])
        age: Optional[int] = today.year - b.year - ((today.month, today.day) < (b.month, b.day))
    except ValueError:
        age = None
    return PatientBanner(name=record.name, birth_date=record.birth_date, mrn=record.mrn, sex=record.gender, age=age)


# ---------------------------------------------------------------- schedule scan (UC5)

def todays_patients(bundle: dict, fhir_user: str, today: date) -> List[Tuple[str, Optional[str]]]:
    """(patient id, start) for the user's appointments today that aren't cancelled, no-show or done, one per patient,
    earliest first (§4.3). OpenEMR names the provider Practitioner/{uuid} with an NPI and Person/{uuid} otherwise.
    `start` is OpenEMR's clinic-local event date plus time, so its date must equal `today`, the clinic's date: the
    server-side date filter is not trusted alone."""
    uuid, day = fhir_user.rstrip("/").rsplit("/", 1)[-1], today.isoformat()
    found: Dict[str, Optional[str]] = {}
    for a in sorted(normalize.resources(bundle), key=lambda a: str(a.get("start") or "")):
        refs = [((p.get("actor") or {}).get("reference") or "").split("/")[-2:]
                for p in a.get("participant") or [] if isinstance(p, dict)]
        if (a.get("resourceType") != "Appointment" or a.get("status") in NOT_LIVE_APPOINTMENT or not uuid
                or str(a.get("start") or "")[:10] != day
                or not any(r in (["Practitioner", uuid], ["Person", uuid]) for r in refs)):
            continue
        pid = next((r[1] for r in refs if len(r) == 2 and r[0] == "Patient" and _ID.fullmatch(r[1])), None)
        if pid:
            found.setdefault(pid, a.get("start"))
    return list(found.items())


async def _scan_one(client: FhirClient, token: str, pid: str, start: Optional[str], today: date, deadline: Deadline,
                    on_event: OnEvent, correlation_id: str) -> ScanPatient:
    get = _bind(client, token, deadline, on_event, correlation_id, pid)
    loads = await _load_many(get, pid, today, SCAN_RESOURCES, lab_months=12)
    failed = [k for k, load in loads.items() if load.status not in _OK]
    # Rules still run on what loaded: a flag found is real; what failed is listed so no one reads it as all-clear.
    flags = rules.check(loads["allergies"].records, loads["medications"].records, loads["labs"].records)
    return ScanPatient(patient_banner=patient_banner(next(iter(loads["patient"].records), None), today),
                       appointment_start=start, status="failed" if failed else "checked", failed_resources=failed,
                       flags=flags)


async def scan_schedule(client: FhirClient, token: str, fhir_user: str, today: date, deadline: Deadline,
                        on_event: OnEvent, correlation_id: str) -> ScheduleScanResponse:
    """UC5 (§4.3): today's appointments for this user -> allergies, medications, labs (12 months) and banner per
    patient -> rules, SCAN_PATIENTS_IN_FLIGHT patients at a time. Counts are per patient (two visits are one check),
    so scheduled == checked + failed and renders as 'N patients today'. Only flagged or failed patients are listed.
    Raises FhirUnavailable when the appointment list itself can't be read."""
    appts = await client.get("Appointment", {"date": today.isoformat()}, token=token, deadline=deadline,
                             correlation_id=correlation_id, on_event=on_event, patient_id=None)
    if appts.status not in _OK:
        raise FhirUnavailable(appts.status, appts.detail)
    gate = asyncio.Semaphore(SCAN_PATIENTS_IN_FLIGHT)

    async def one(pid: str, start: Optional[str]) -> ScanPatient:
        async with gate:
            return await _scan_one(client, token, pid, start, today, deadline, on_event, correlation_id)
    scanned = await _run_all(one(pid, start) for pid, start in todays_patients(appts.bundle, fhir_user, today))
    counts = ScanCounts(scheduled=len(scanned), checked=sum(p.status == "checked" for p in scanned),
                        failed=sum(p.status == "failed" for p in scanned), flagged=sum(bool(p.flags) for p in scanned))
    return ScheduleScanResponse(correlation_id=correlation_id, counts=counts,
                                patients=[p for p in scanned if p.flags or p.status == "failed"])
