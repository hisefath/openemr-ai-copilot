"""FHIR client, prefetch, tools and schedule scan against a fake OpenEMR (httpx.MockTransport) serving the real
OpenEMR 8.5.0 bundles in tests/fixtures plus small synthetic charts and appointment lists. No network.
Each test names the failure mode it guards against."""
import asyncio
import json
import logging
from datetime import date
from pathlib import Path

import httpx
import pytest

from copilot import fhir
from copilot.deadline import Deadline
from copilot.schemas import EncountersInput, LabHistoryInput, LoadStatus

FIX = Path(__file__).parent / "fixtures"
A = json.loads((FIX / "patient_a.json").read_text())
B = json.loads((FIX / "patient_b.json").read_text())
APPTS_TODAY = json.loads((FIX / "appointments_today.json").read_text())  # real OpenEMR output, dr_chen's day
DR_CHEN = "Practitioner/a2c41ae8-004c-4a01-8ca5-b49c6e8d7397"
PID_A, PID_B = A["patient_uuid"], B["patient_uuid"]
BASE = "http://openemr/apis/default/fhir"
TODAY = date(2026, 9, 17)
TOKEN, CID = "tok-abc", "cid-123"
V2_0203 = "http://terminology.hl7.org/CodeSystem/v2-0203"


def frozen():
    """A Deadline clock that never advances: remaining() is fixed, so only asyncio time bounds a wait."""
    return 0.0


def bundle(rows):
    return {"resourceType": "Bundle", "type": "searchset", "total": len(rows), "entry": [{"resource": r} for r in rows]}


def fake_openemr(charts=None, appointments=None, fault=lambda key, pid: None, seen=None):
    """Routes like OpenEMR FHIR, honours date=ge on Observation/Encounter. fault(key, pid) may return a Response
    or raise to inject a failure."""
    charts = charts or {PID_A: A, PID_B: B}

    def handler(request):
        if seen is not None:
            seen.append(request)
        path, q = request.url.path.removeprefix("/apis/default/fhir/"), request.url.params
        if path == "Appointment":
            key, pid = "Appointment", None
        elif path.startswith("Patient/"):
            key, pid = "Patient", path.split("/", 1)[1]
        else:
            key, pid = path + ("_" + q["category"].replace("-", "_") if "category" in q else ""), q["patient"]
        if (resp := fault(key, pid)) is not None:
            return resp
        if key == "Appointment":
            return httpx.Response(200, json=appointments)
        data = charts[pid][key]
        if key == "Patient":
            return httpx.Response(200, json=data["entry"][0]["resource"])
        since = q.get("date", "ge")[2:]
        rows = [e for e in data.get("entry") or []
                if (e["resource"].get("effectiveDateTime") or (e["resource"].get("period") or {}).get("start") or "")[:10] >= since]
        return httpx.Response(200, json={**data, "entry": rows})
    return handler


def make_client(handler, concurrency=6):
    return fhir.FhirClient(httpx.AsyncClient(transport=httpx.MockTransport(handler)), BASE, concurrency)


def prefetch(handler, pid=PID_A, deadline=None, events=None, client=None):
    client = client or make_client(handler)
    return asyncio.run(fhir.prefetch(client, TOKEN, pid, TODAY, deadline or Deadline(9.0),
                                     (events if events is not None else []).append, CID))


def refresh(ctx, which, handler, events=None):
    return asyncio.run(fhir.refresh(ctx, which, make_client(handler), TOKEN, TODAY, Deadline(9.0),
                                    (events if events is not None else []).append, CID))


def get_one(handler, path="AllergyIntolerance", deadline=None, events=None):
    get = fhir._bind(make_client(handler), TOKEN, deadline or Deadline(9.0), (events if events is not None else []).append,
                     CID, PID_A)
    return asyncio.run(get(path, None if "/" in path else {"patient": PID_A}))


def raise_(exc):
    raise exc


# ---------------------------------------------------------------- prefetch

def test_prefetch_sends_the_exact_date_bounded_queries_with_token_and_correlation_id():
    """Guards: dropping a date bound (one real patient has 905 labs, PERF-4), re-adding a Condition category that
    hides encounter-linked problems (ARCH-M2), or calling OpenEMR without the physician's token."""
    seen = []
    prefetch(fake_openemr(seen=seen))
    calls = {(r.url.path.removeprefix("/apis/default/fhir/"), tuple(sorted(r.url.params.items()))) for r in seen}
    p = ("patient", PID_A)
    assert calls == {
        (f"Patient/{PID_A}", ()),
        ("AllergyIntolerance", (p,)),
        ("MedicationRequest", (p,)),
        ("Condition", (p,)),
        ("Observation", (("category", "laboratory"), ("date", "ge2025-03-17"), p)),
        ("Observation", (("category", "vital-signs"), ("date", "ge2025-09-17"), p)),
        ("Encounter", (("date", "ge2024-09-17"), p)),
    }
    for r in seen:
        assert r.headers["Authorization"] == f"Bearer {TOKEN}"
        assert r.headers["Accept"] == "application/fhir+json"
        assert r.headers["X-Correlation-ID"] == CID


def test_prefetch_normalizes_real_bundles_and_distinguishes_empty_from_ok():
    """Guards: raw duplicated meds reaching the model (DQ-M3), or an empty allergy list reported as 'ok' facts."""
    a = prefetch(fake_openemr())
    assert a.patient_id == PID_A and a.fetched_at
    assert a.allergies.status == LoadStatus.ok and len(a.allergies.records) == 12
    assert a.medications.status == LoadStatus.ok and len(a.medications.records) == 11
    assert all(s.status == LoadStatus.ok for s in (a.patient, a.conditions, a.labs, a.encounters))
    assert a.vitals.status == LoadStatus.empty  # the fixture's vitals are from 2016, outside the 12-month window
    b = prefetch(fake_openemr(), pid=PID_B)
    assert b.allergies.status == LoadStatus.empty and b.allergies.records == []


def test_patient_record_has_mrn_and_banner_age_respects_birthday():
    """Guards: a banner without MRN (a wrong-chart launch goes unnoticed, §2) or an age off by one before the birthday."""
    [pa] = prefetch(fake_openemr()).patient.records
    assert pa.mrn == "15" and pa.source_ids == [f"Patient/{PID_A}"]
    banner = fhir.patient_banner(pa, TODAY)
    assert (banner.name, banner.birth_date, banner.mrn, banner.sex, banner.age) == ("Lonny638 Tromp100", "1965-07-31", "15", "male", 61)
    [pb] = prefetch(fake_openemr(), pid=PID_B).patient.records
    assert pb.mrn == "1" and fhir.patient_banner(pb, TODAY).age == 69  # born 1956-12-20: birthday not reached yet
    assert fhir.patient_banner(pb.model_copy(update={"birth_date": None}), TODAY).age is None
    assert fhir.patient_banner(None, TODAY).name == "Patient details unavailable"


def test_http_failures_map_to_load_statuses_per_resource():
    """Guards: a 403 rendered as 'no medications recorded', a timeout rendered as empty, or one failure sinking the
    whole prefetch."""
    faults = {
        "AllergyIntolerance": lambda: httpx.Response(401),
        "MedicationRequest": lambda: httpx.Response(403),
        "Condition": lambda: httpx.Response(500),
        "Encounter": lambda: httpx.Response(200, text="<html>PHP warning</html>"),
        "Observation_vital_signs": lambda: raise_(httpx.ReadTimeout("slow")),
        "Observation_laboratory": lambda: httpx.Response(200, json=bundle([])),
    }
    ctx = prefetch(fake_openemr(fault=lambda key, pid: faults[key]() if key in faults else None))
    got = {k: (getattr(ctx, k).status, getattr(ctx, k).detail) for k in fhir.ORDER}
    assert got == {
        "allergies": (LoadStatus.expired, "HTTP 401"),
        "medications": (LoadStatus.forbidden, "HTTP 403"),
        "conditions": (LoadStatus.error, "HTTP 500"),
        "encounters": (LoadStatus.error, "unparseable body"),
        "vitals": (LoadStatus.timeout, "ReadTimeout"),
        "labs": (LoadStatus.empty, None),
        "patient": (LoadStatus.ok, None),
    }


@pytest.mark.parametrize("exc, status", [(httpx.ReadTimeout, LoadStatus.timeout), (httpx.WriteTimeout, LoadStatus.timeout),
                                         (httpx.PoolTimeout, LoadStatus.timeout),
                                         (httpx.RemoteProtocolError, LoadStatus.error)])
def test_transport_exceptions_become_a_status_without_retry(exc, status):
    """Guards: a write or pool timeout escaping as an exception that sinks the whole prefetch, or retrying a call
    that may have reached OpenEMR."""
    attempts = []
    fetch = get_one(lambda request: attempts.append(request) or raise_(exc("x")))
    assert (fetch.status, fetch.detail, len(attempts)) == (status, exc.__name__, 1)


NO_SUBJECT = bundle([{k: v for k, v in e["resource"].items() if k != "subject"} for e in A["MedicationRequest"]["entry"]])


@pytest.mark.parametrize("path, body, detail", [
    ("AllergyIntolerance", {"resourceType": "OperationOutcome", "issue": [{"severity": "error"}]}, "unparseable body"),
    ("AllergyIntolerance", {"resourceType": "Bundle", "entry": 5}, "unparseable body"),
    ("AllergyIntolerance", {"resourceType": "Bundle", "entry": [{"resource": "x"}]}, "unparseable body"),
    ("AllergyIntolerance", bundle([{"resourceType": "OperationOutcome", "issue": []}]), "unexpected resource type"),
    ("AllergyIntolerance", A["Condition"], "unexpected resource type"),
    (f"Patient/{PID_A}", B["Patient"]["entry"][0]["resource"], "patient mismatch"),
    ("MedicationRequest", B["MedicationRequest"], "patient mismatch"),
    ("MedicationRequest", NO_SUBJECT, "patient mismatch"),
])
def test_malformed_foreign_or_unattributed_bodies_fail_that_call_with_an_error_event(path, body, detail):
    """Guards: a malformed 200 crashing the whole prefetch, an OperationOutcome or Condition rows normalized as
    allergies, or rows naming another patient or no patient loading as the session patient's (§1 lock fails closed)
    while the event and audit row say 'ok' (FM-09)."""
    events = []
    fetch = get_one(lambda request: httpx.Response(200, json=body), path=path, events=events)
    assert (fetch.status, fetch.detail, fetch.bundle) == (LoadStatus.error, detail, None)
    [e] = events
    assert (e["status"], e["detail"], e["http_status"], e["count"]) == ("error", detail, 200, 0)


def test_rows_for_another_patient_fail_closed_in_prefetch_and_refresh():
    """Guards: OpenEMR returning someone else's medications (PERF-3 practice-wide query) and the agent citing them,
    recording the read as 'ok', or, on refresh, keeping the earlier load so the mismatch leaves no trace (FM-09)."""
    swapped = fake_openemr(charts={PID_A: {**A, "MedicationRequest": B["MedicationRequest"]}})
    events = []
    ctx = prefetch(swapped, events=events)
    assert (ctx.medications.status, ctx.medications.detail, ctx.medications.records) == (LoadStatus.error, "patient mismatch", [])
    [e] = [e for e in events if e["resource"] == "MedicationRequest"]
    assert (e["status"], e["detail"], e["audit_patient_id"]) == ("error", fhir.PATIENT_MISMATCH, PID_A)

    good = prefetch(fake_openemr())
    assert len(good.medications.records) == 11
    new = refresh(good, ["medications"], swapped)
    assert (new.medications.status, new.medications.detail, new.medications.records) == (LoadStatus.error, "patient mismatch", [])


def test_invalid_or_missing_patient_id_is_rejected_before_any_call():
    """Guards: an id with '/' or '..' rewriting the FHIR path, or a None/empty id sending `patient=` with an unbounded
    date and pulling every patient's rows into memory and api_log before the lock rejects them."""
    seen = []
    client = make_client(fake_openemr(seen=seen))
    for bad in ("../Patient", "", None):
        with pytest.raises(ValueError):
            prefetch(None, pid=bad, client=client)
        with pytest.raises(ValueError):
            asyncio.run(fhir.get_lab_history(client, TOKEN, bad, LabHistoryInput(lab="2339-0", since="1900-01-01"),
                                             Deadline(9.0), [].append, CID))
        with pytest.raises(ValueError):
            asyncio.run(fhir.get_encounters(client, TOKEN, bad, EncountersInput(since="1900-01-01"), Deadline(9.0),
                                            [].append, CID))
    assert seen == []


# ---------------------------------------------------------------- client: retry, deadline, semaphore, events, logs

@pytest.mark.parametrize("exc, status", [(httpx.ConnectError, LoadStatus.error), (httpx.ConnectTimeout, LoadStatus.timeout)])
def test_connection_failure_is_retried_once_when_time_remains(exc, status):
    """Guards: a transient connection blip failing a resource, or a retry loop hammering a down OpenEMR."""
    attempts, events = [], []

    def flaky(request):
        attempts.append(request)
        return raise_(exc("refused")) if len(attempts) == 1 else httpx.Response(200, json=A["AllergyIntolerance"])
    assert get_one(flaky, events=events).status == LoadStatus.ok
    assert len(attempts) == 2 and events[0]["retried"] is True

    attempts.clear()
    fetch = get_one(lambda request: attempts.append(request) or raise_(exc("refused")))
    assert (fetch.status, fetch.detail, len(attempts)) == (status, exc.__name__, 2)


def test_no_retry_with_less_than_3s_left_and_never_on_read_timeout():
    """Guards: a retry overrunning the 9 s question deadline, or retrying a slow (not down) OpenEMR (PERF-1)."""
    attempts = []
    refuse = lambda request: attempts.append(request) or raise_(httpx.ConnectError("refused"))  # noqa: E731
    assert get_one(refuse, deadline=Deadline(2.5, clock=frozen)).status == LoadStatus.error
    assert len(attempts) == 1

    attempts.clear()
    assert get_one(lambda request: attempts.append(request) or raise_(httpx.ReadTimeout("slow"))).status == LoadStatus.timeout
    assert len(attempts) == 1


def test_exhausted_deadline_is_timeout_without_calling_openemr():
    """Guards: firing FHIR calls after the question's budget is gone."""
    seen = []
    ctx = prefetch(fake_openemr(seen=seen), deadline=Deadline(0.0))
    assert seen == [] and all(getattr(ctx, k).status == LoadStatus.timeout for k in fhir.ORDER)


def test_queue_wait_is_bounded_by_the_deadline_and_releases_the_slot():
    """Guards: a question blocking on a busy semaphore past its deadline, or a timed-out waiter leaking a slot."""
    base = fake_openemr()

    async def scenario():
        started, release, events = asyncio.Event(), asyncio.Event(), []

        async def handler(request):
            if request.url.path.endswith("AllergyIntolerance"):
                started.set()
                await release.wait()  # holds the only slot until the waiter below has given up
            return base(request)
        client = make_client(handler, concurrency=1)
        long = fhir._bind(client, TOKEN, Deadline(9.0, clock=frozen), events.append, CID, PID_A)
        short = fhir._bind(client, TOKEN, Deadline(0.05, clock=frozen), events.append, CID, PID_A)
        first = asyncio.create_task(long("AllergyIntolerance", {"patient": PID_A}))
        await started.wait()
        second = await short("MedicationRequest", {"patient": PID_A})
        depth = client.queue_depth
        release.set()
        return await first, second, await long("Condition", {"patient": PID_A}), depth, events
    first, second, third, depth, events = asyncio.run(scenario())
    assert first.status == LoadStatus.ok and third.status == LoadStatus.ok
    assert (second.status, second.detail, depth) == (LoadStatus.timeout, "deadline", 0)
    [waited] = [e for e in events if e["resource"] == "MedicationRequest"]
    assert waited["ms"] == 0 and waited["queue_ms"] > 0 and waited["http_status"] is None


def test_semaphore_caps_parallel_calls_and_reports_queue_depth():
    """Guards: a prefetch stampede exhausting OpenEMR's PHP workers (PERF-1) with no queue-depth signal."""
    base, state = fake_openemr(), {"in_flight": 0, "max": 0, "depth": 0}

    async def handler(request):
        state["in_flight"] += 1
        state["max"] = max(state["max"], state["in_flight"])
        await asyncio.sleep(0.01)
        state["depth"] = max(state["depth"], client.queue_depth)
        state["in_flight"] -= 1
        return base(request)
    client = make_client(handler, concurrency=2)
    ctx = prefetch(handler, client=client)
    assert all(getattr(ctx, k).status in (LoadStatus.ok, LoadStatus.empty) for k in fhir.ORDER)
    assert state["max"] == 2 and state["depth"] == 5 and client.queue_depth == 0


def test_call_events_are_structured_and_keep_the_raw_patient_id_out_of_every_loggable_field():
    """Guards: a FHIR call with no event to audit, a query string or resource id in the event path (COMP-3), or
    events that can't tell a 403 from a deadline. The raw uuid sits only under `audit_patient_id`, for the audit row;
    the consumer hashes it before any log or span."""
    events = []
    prefetch(fake_openemr(fault=lambda key, pid: httpx.Response(403) if key == "Condition" else None), events=events)
    assert len(events) == 7
    for e in events:
        assert set(e) == {"method", "resource", "path", "status", "detail", "http_status", "count", "ms", "queue_ms",
                          "retried", "audit_patient_id"}
        assert "?" not in e["path"] and e["path"].startswith("/apis/default/fhir/")
        assert PID_A not in json.dumps({k: v for k, v in e.items() if k != "audit_patient_id"})
        assert e["audit_patient_id"] == PID_A and e["retried"] is False
        assert isinstance(e["ms"], int) and isinstance(e["queue_ms"], int)
    by_resource = {e["resource"]: e for e in events if e["resource"] != "Observation"}
    assert by_resource["Patient"]["path"] == "/apis/default/fhir/Patient/{id}"
    assert (by_resource["AllergyIntolerance"]["http_status"], by_resource["AllergyIntolerance"]["count"]) == (200, 12)
    assert [by_resource["Condition"][k] for k in ("status", "detail", "http_status")] == ["forbidden", "HTTP 403", 403]


def test_no_log_record_carries_a_patient_uuid_or_query_string(caplog):
    """Guards: httpx's INFO 'HTTP Request: GET <url>' writing every patient uuid and query string to stdout and
    Railway logs once observability sets the root logger to INFO (COMP-3)."""
    caplog.set_level(logging.DEBUG)
    prefetch(fake_openemr())
    assert [r.getMessage() for r in caplog.records if PID_A in r.getMessage() or "?" in r.getMessage()] == []


def test_a_failing_event_consumer_cancels_sibling_calls_and_raises_its_own_exception():
    """Guards: an audit write failing (FM-15) while the other calls run on as orphans, holding semaphore slots and
    emitting events for a request that already failed, or the error arriving as an ExceptionGroup callers miss."""
    base = fake_openemr()

    class AuditDown(Exception):
        pass

    async def scenario():
        release, events = asyncio.Event(), []

        async def handler(request):
            if not request.url.path.endswith("AllergyIntolerance"):
                await release.wait()
            return base(request)

        def on_event(e):
            events.append(e)
            if e["resource"] == "AllergyIntolerance":
                raise AuditDown
        client = make_client(handler)
        with pytest.raises(AuditDown):
            await fhir.prefetch(client, TOKEN, PID_A, TODAY, Deadline(9.0), on_event, CID)
        release.set()
        await asyncio.sleep(0.02)  # an orphaned call would complete and emit now
        return events, client
    events, client = asyncio.run(scenario())
    assert [e["resource"] for e in events] == ["AllergyIntolerance"] and client.queue_depth == 0


# ---------------------------------------------------------------- refresh and tools

def test_refresh_orders_calls_and_marks_a_failed_refetch_while_rules_keep_its_records():
    """Guards: a timed-out allergy refetch leaving the 10:00 list rendered as current fact (§3: 'unavailable right
    now'), rules losing allergies they already saw, a revoked token still showing data, or 'Data as of' moving
    after a partial refresh."""
    ctx = prefetch(fake_openemr())
    seen = []
    faults = {"AllergyIntolerance": lambda: raise_(httpx.ReadTimeout("slow")), "MedicationRequest": lambda: httpx.Response(401)}
    client = make_client(fake_openemr(seen=seen, fault=lambda key, pid: faults[key]() if key in faults else None), concurrency=1)
    new = asyncio.run(fhir.refresh(ctx, ["labs", "medications", "allergies"], client, TOKEN, TODAY, Deadline(9.0), [].append, CID))
    assert [r.url.path.rsplit("/", 1)[-1] for r in seen] == ["AllergyIntolerance", "MedicationRequest", "Observation"]
    assert (new.allergies.status, new.allergies.detail) == (LoadStatus.timeout, "ReadTimeout")
    assert new.allergies.records == ctx.allergies.records and len(new.allergies.records) == 12
    assert new.medications.status == LoadStatus.expired and new.medications.records == []
    assert new.labs.status == LoadStatus.ok and new.fetched_at == ctx.fetched_at


def test_full_refresh_advances_data_as_of_even_if_one_resource_keeps_timing_out(monkeypatch):
    """Guards: one slow resource (a 905-lab patient) freezing 'Data as of' so every question refetches all seven and
    times out again, or a bad `which` making no call while returning stale data as refreshed."""
    stamps = iter(["10:00", "10:00:01", "10:25"])
    monkeypatch.setattr(fhir, "_now", lambda: next(stamps))
    seen = []
    slow_labs = fake_openemr(seen=seen, fault=lambda key, pid: raise_(httpx.ReadTimeout("slow")) if key == "Observation_laboratory" else None)
    ctx = prefetch(slow_labs)
    assert ctx.fetched_at == "10:00:01" and ctx.labs.status == LoadStatus.timeout
    new = refresh(ctx, fhir.ORDER, slow_labs)
    assert new.fetched_at == "10:25" and new.labs.status == LoadStatus.timeout and new.allergies.status == LoadStatus.ok
    seen.clear()
    for bad in ("allergies", ["allergies", "allergy"]):
        with pytest.raises(ValueError):
            refresh(new, bad, slow_labs)
    assert seen == []


def test_lab_history_matches_loinc_or_name_for_the_session_patient_only():
    """Guards: the tool querying a patient other than the session's, or a trend mixing different tests (a blank
    query matching every uncoded lab, or 'Lab' matching normalize's fallback name)."""
    seen = []
    client = make_client(fake_openemr(seen=seen))

    def run(lab, since="2025-01-01"):
        return asyncio.run(fhir.get_lab_history(client, TOKEN, PID_A, LabHistoryInput(lab=lab, since=since),
                                                Deadline(9.0), [].append, CID))
    by_code = run("2339-0")
    assert by_code.status == LoadStatus.ok and by_code.records
    assert {r.loinc for r in by_code.records} == {"2339-0"}
    assert run(" glucose [MASS/VOLUME] in blood ").records == by_code.records
    assert run("Glucose").status == LoadStatus.empty  # a fragment is not a name match
    assert dict(seen[0].url.params) == {"patient": PID_A, "category": "laboratory", "date": "ge2025-01-01"}
    assert "patient" not in LabHistoryInput.model_fields
    calls = len(seen)
    assert run(" ").status == LoadStatus.empty and run("Lab").status == LoadStatus.empty and len(seen) == calls


def test_encounters_tool_uses_session_patient_and_since():
    """Guards: UC3 older-visit lookups ignoring `since` or the session patient."""
    seen = []
    load = asyncio.run(fhir.get_encounters(make_client(fake_openemr(seen=seen)), TOKEN, PID_A,
                                           EncountersInput(since="2026-01-01"), Deadline(9.0), [].append, CID))
    assert dict(seen[0].url.params) == {"patient": PID_A, "date": "ge2026-01-01"}
    assert load.status == LoadStatus.ok and all(r.date >= "2026-01-01" for r in load.records)
    assert [r.date for r in load.records] == sorted((r.date for r in load.records), reverse=True)


# ---------------------------------------------------------------- schedule scan (UC5)

DOC = "a2c41000-0000-4000-8000-00000000d0c1"
OTHER_DOC = "a2c41000-0000-4000-8000-00000000d0c2"
PID_C = "a2c41000-0000-4000-8000-0000000000c3"     # synthetic: uncoded Penicillin allergy + active amoxicillin
PID_FAIL = "a2c41000-0000-4000-8000-0000000000f4"  # allergies fetch fails
PID_X, PID_Y = "a2c41000-0000-4000-8000-0000000000e5", "a2c41000-0000-4000-8000-0000000000e6"


def participant(code, display, reference):
    return {"type": [{"coding": [{"system": "http://terminology.hl7.org/CodeSystem/v3-ParticipationType", "code": code,
                                  "display": display}]}],
            "actor": {"reference": reference, "type": reference.split("/")[0]}, "status": "accepted"}


def appointment(aid, status, start, patient=None, provider=f"Practitioner/{DOC}", day="2026-09-17"):
    parts = [participant("PART", "Participant", f"Patient/{patient}")] if patient else []
    parts += [participant("PPRF", "Primary Performer", provider),
              participant("LOC", "Location", "Location/a2c41000-0000-4000-8000-00000000f001")]
    return {"resourceType": "Appointment", "id": aid, "status": status,
            "appointmentType": {"coding": [{"code": "5", "display": "Office Visit"}]},
            "start": f"{day}T{start}:00+00:00", "end": f"{day}T{start[:2]}:15:00+00:00", "participant": parts}


APPOINTMENTS = bundle([
    appointment("ap1", "booked", "09:00", PID_B),
    appointment("ap2", "arrived", "08:30", PID_A, provider=f"Person/{DOC}"),  # provider without NPI
    appointment("ap3", "proposed", "10:00", PID_C),                            # OpenEMR's default status
    appointment("ap4", "pending", "11:00", PID_FAIL),
    appointment("ap5", "cancelled", "12:00", PID_X),
    appointment("ap6", "booked", "13:00", PID_Y, provider=f"Practitioner/{OTHER_DOC}"),
    appointment("ap7", "booked", "14:00"),                                     # no patient: a provider block
    appointment("ap8", "booked", "15:00", PID_A),                              # second visit, same patient
    appointment("ap9", "booked", "16:00", PID_X, day="2026-03-01"),            # another day slipped past the filter
])


def synthetic_chart(pid, mrn, given, allergies=(), meds=()):
    ref = {"reference": f"Patient/{pid}", "type": "Patient"}
    return {
        "Patient": bundle([{"resourceType": "Patient", "id": pid, "gender": "female", "birthDate": "1980-02-29",
                            "identifier": [{"use": "official", "type": {"coding": [{"system": V2_0203, "code": "PT"}]},
                                            "system": V2_0203, "value": mrn}],
                            "name": [{"use": "official", "family": "Synthetic", "given": [given]}]}]),
        "AllergyIntolerance": bundle([{**a, "patient": ref} for a in allergies]),
        "MedicationRequest": bundle([{**m, "subject": ref} for m in meds]),
        "Observation_laboratory": bundle([]),
    }


CHARTS = {
    PID_A: A, PID_B: B,
    PID_C: synthetic_chart(PID_C, "901", "Cora", allergies=[{
        "resourceType": "AllergyIntolerance", "id": "c-allergy-1", "clinicalStatus": {"coding": [{"code": "active"}]},
        "code": {"coding": [{"code": "unknown", "display": "Unknown"}]},
        "text": {"div": "<div xmlns='http://www.w3.org/1999/xhtml'>Penicillin</div>"}}], meds=[{
        "resourceType": "MedicationRequest", "id": "c-med-1", "intent": "order", "status": "active",
        "authoredOn": "2026-09-10T00:00:00+00:00",
        "medicationCodeableConcept": {"coding": [{"system": "http://www.nlm.nih.gov/research/umls/rxnorm", "code": "308191",
                                                  "display": "Amoxicillin 500 MG Oral Capsule"}]}}]),
    PID_FAIL: synthetic_chart(PID_FAIL, "902", "Faye"),
}


def scan(fault=lambda key, pid: None, seen=None, events=None, fhir_user=f"Practitioner/{DOC}"):
    handler = fake_openemr(charts=CHARTS, appointments=APPOINTMENTS, fault=fault, seen=seen)
    return asyncio.run(fhir.scan_schedule(make_client(handler), TOKEN, fhir_user, TODAY, Deadline(60.0),
                                          (events if events is not None else []).append, CID))


def test_todays_patients_keeps_only_this_users_appointments_today_that_will_happen():
    """Guards: scanning cancelled visits, another day's or another provider's patients, provider blocks, or the same
    patient twice."""
    assert fhir.todays_patients(APPOINTMENTS, f"Practitioner/{DOC}", TODAY) == [
        (PID_A, "2026-09-17T08:30:00+00:00"), (PID_B, "2026-09-17T09:00:00+00:00"),
        (PID_C, "2026-09-17T10:00:00+00:00"), (PID_FAIL, "2026-09-17T11:00:00+00:00")]
    assert [p for p, _ in fhir.todays_patients(APPOINTMENTS, f"https://emr.example/fhir/Person/{OTHER_DOC}", TODAY)] == [PID_Y]
    assert fhir.todays_patients(APPOINTMENTS, "", TODAY) == []
    assert fhir.todays_patients(APPOINTMENTS, f"Practitioner/{DOC}", date(2026, 9, 18)) == []


def test_real_openemr_schedule_keeps_proposed_visits_and_drops_cancelled_and_noshow():
    """Guards: OpenEMR's default appointment status ('-' maps to proposed) dropping ordinary visits from UC5, so a
    penicillin-allergic patient with a new amoxicillin order in one of those slots gets a silent all-clear."""
    statuses = {(e["resource"]["participant"][0]["actor"]["reference"].split("/")[1]): e["resource"]["status"]
                for e in APPTS_TODAY["entry"]}
    got = fhir.todays_patients(APPTS_TODAY, DR_CHEN, TODAY)
    assert len(got) == 8 and [start for _, start in got] == sorted(start for _, start in got)
    assert sorted(statuses[p] for p, _ in got) == ["arrived", "booked", "booked", "pending"] + ["proposed"] * 4
    assert {p for p in statuses if p not in dict(got)} == {"a2c40ee3-3f1a-46ca-9af5-a22d0108f467",  # cancelled
                                                           "a2c40ee8-76c4-4520-a49b-ba322f30731f"}  # noshow


def test_schedule_scan_counts_flags_and_failures_in_code():
    """Guards: UC5 reading charts outside today's list, a failed patient counted as checked (a silent all-clear),
    or unflagged patients cluttering the 8:40 AM list."""
    seen, events, faults = [], [], {("AllergyIntolerance", PID_FAIL): lambda: httpx.Response(500)}
    result = scan(fault=lambda key, pid: faults[(key, pid)]() if (key, pid) in faults else None, seen=seen, events=events)

    assert dict(seen[0].url.params) == {"date": "2026-09-17"}
    assert len(events) == len(seen) == 1 + 4 * 4
    assert (events[0]["resource"], events[0]["audit_patient_id"]) == ("Appointment", None)
    assert {e["audit_patient_id"] for e in events[1:]} == {PID_A, PID_B, PID_C, PID_FAIL}
    read = {r.url.params.get("patient") or r.url.path.rsplit("/", 1)[-1] for r in seen[1:]}
    assert read == {PID_A, PID_B, PID_C, PID_FAIL}
    assert {r.url.params.get("date") for r in seen if r.url.params.get("category") == "laboratory"} == {"ge2025-09-17"}
    assert not any(r.url.path.endswith(("Condition", "Encounter")) or r.url.params.get("category") == "vital-signs" for r in seen)

    assert result.correlation_id == CID
    assert result.counts.model_dump() == {"scheduled": 4, "checked": 3, "failed": 1, "flagged": 1}
    flagged, failed = result.patients
    assert (flagged.patient_banner.name, flagged.patient_banner.mrn, flagged.status) == ("Cora Synthetic", "901", "checked")
    assert flagged.appointment_start == "2026-09-17T10:00:00+00:00"
    assert [f.rule_id for f in flagged.flags] == ["allergy-drug-class"]
    assert "AllergyIntolerance/c-allergy-1" in flagged.flags[0].source_ids
    assert (failed.patient_banner.name, failed.status, failed.failed_resources) == ("Faye Synthetic", "failed", ["allergies"])


def test_schedule_scan_raises_when_the_appointment_list_fails():
    """Guards: a 403 or timeout on today's schedule rendered as '0 appointments today'."""
    with pytest.raises(fhir.FhirUnavailable) as err:
        scan(fault=lambda key, pid: httpx.Response(403) if key == "Appointment" else None)
    assert err.value.status == LoadStatus.forbidden and err.value.detail == "HTTP 403"


def test_a_chart_launch_during_a_20_patient_scan_is_served_before_the_rest_of_the_scan():
    """Guards: the scan queueing 80 calls on the shared FIFO semaphore at once, so a physician opening a chart at
    8:40 AM waits behind all of them and sees every section 'unavailable (timeout)'."""
    pids = [f"a2c41000-0000-4000-8000-{i:012d}" for i in range(20)]
    charts = {**{p: synthetic_chart(p, str(1000 + i), "Scan") for i, p in enumerate(pids)}, PID_A: A}
    appts = bundle([appointment(f"s{i}", "booked", f"{8 + i // 4:02d}:{i % 4 * 15:02d}", p) for i, p in enumerate(pids)])
    base, arrived = fake_openemr(charts=charts, appointments=appts), []

    async def handler(request):
        arrived.append(request.url.params.get("patient") or request.url.path.rsplit("/", 1)[-1])
        await asyncio.sleep(0.01)
        return base(request)

    async def scenario():
        client = make_client(handler)
        task = asyncio.create_task(fhir.scan_schedule(client, TOKEN, f"Practitioner/{DOC}", TODAY, Deadline(60.0),
                                                      [].append, CID))
        while len(arrived) < 3:  # the appointment list is read and patient calls have started
            await asyncio.sleep(0)
        ctx = await fhir.prefetch(client, TOKEN, PID_A, TODAY, Deadline(9.0), [].append, CID)
        return ctx, await task
    ctx, result = asyncio.run(scenario())
    assert all(getattr(ctx, k).status in (LoadStatus.ok, LoadStatus.empty) for k in fhir.ORDER)
    # Only the appointment list and 2 patients x 4 calls were ahead of the launch; unbounded, all 80 would be.
    assert max(i for i, p in enumerate(arrived) if p == PID_A) < 1 + 2 * 4 + 7
    assert len(arrived) == 1 + 20 * 4 + 7 and result.counts.model_dump() == {"scheduled": 20, "checked": 20, "failed": 0, "flagged": 0}
