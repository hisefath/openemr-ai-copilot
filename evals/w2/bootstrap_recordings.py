#!/usr/bin/env python3
"""Bootstrap recordings from each case's `fixture_plan`, with no API key and no spend.

    python evals/w2/bootstrap_recordings.py

What this is and is not. The recordings it writes carry a REAL surface key — computed from the exact kwargs the
running app assembles, so the keying rule in replay.py is exercised for real — but a FIXTURE response rather
than one Claude actually produced. That is the right trade for building the gate before the model work exists:
the gate is provably able to fail today, and the responses get replaced by live captures later without any
change to the harness.

A recording's `source` field says `fixture` so nobody mistakes one for evidence of model behaviour. Re-record
against live Claude once the flow is final; `W2_COST_AND_LATENCY.md` prices that at about $1.50 for the set.

Retrieval cases need no recording: they replay the committed Voyage cache instead.
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

HERE = Path(__file__).parent
REPO = HERE.resolve().parents[1]
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(REPO / "agent"))
sys.path.insert(0, str(REPO / "agent" / "tests"))


def _resolve(obj, table: dict):
    """Cases refer to fixture ids symbolically ($PENICILLIN) because the real uuids live in the fixture file."""
    if isinstance(obj, str):
        return table.get(obj, obj) if obj.startswith("$") else obj
    if isinstance(obj, list):
        return [_resolve(v, table) for v in obj]
    if isinstance(obj, dict):
        return {k: _resolve(v, table) for k, v in obj.items()}
    return obj


def _vision_message(plan: dict):
    """A vision response shaped exactly as the schema constrains the model to."""
    import json as _json
    from anthropic.types import Message

    m = Message.model_validate({
        "id": "msg", "type": "message", "role": "assistant", "model": "claude-haiku-4-5",
        "content": [{"type": "text", "text": _json.dumps(plan)}], "stop_reason": "end_turn",
        "stop_sequence": None, "usage": {"input_tokens": 2600, "output_tokens": 300}})
    m._request_id = "req_fixture"
    return m


def main() -> int:
    import httpx
    from fastapi.testclient import TestClient

    import fixtures
    import test_main as T
    from copilot import fhir, main as app_main, staging as staging_mod
    from copilot.emr_write import content_filename
    from replay import RecordingClient
    from run_gate import _openemr, load_cases

    for k, v in {"OPENEMR_FHIR_BASE": T.BASE, "PUBLIC_ISSUER": T.BASE, "SMART_CLIENT_ID": "copilot",
                 "SMART_CLIENT_SECRET": "test-secret", "ALLOW_API_SESSIONS": "true", "EVAL_PATIENT_IDS": T.PID,
                 "AGENT_PUBLIC_URL": "http://agent.test", "HMAC_KEY": "test-hmac", "ANTHROPIC_API_KEY": "dummy",
                 "LLM_WARMUP": "false"}.items():
        os.environ[k] = v
    for k in ("AUDIT_DB_HOST", "LANGFUSE_PUBLIC_KEY", "VOYAGE_API_KEY"):
        os.environ.pop(k, None)

    table = {"$PENICILLIN": T.PENICILLIN, "$AMOXICILLIN": T.AMOXICILLIN, "$PID": T.PID}
    targets = [(HERE / "cases", HERE / "recordings"), (HERE / "selftest", HERE / "selftest" / "recordings")]
    written = skipped = failed = 0

    for case_dir, rec_dir in targets:
        for case in load_cases(case_dir):
            if case.get("kind") == "retrieval":
                skipped += 1
                continue

            doc = case.get("document")
            plans = ([_resolve(doc["fixture_plan"], table)] if doc
                     else [_resolve(t["fixture_plan"], table) for t in case.get("turns", [])
                           if "fixture_plan" in t])
            if not plans:
                print(f"  skip {case['id']}: no fixture_plan")
                skipped += 1
                continue

            state = {}
            with TestClient(app_main.app) as c:
                app_main.app.state.http = httpx.AsyncClient(transport=httpx.MockTransport(_openemr(T, state)))
                app_main.app.state.fhir = fhir.FhirClient(app_main.app.state.http, T.BASE, 6)
                app_main.app.state.emr_write.__init__(app_main.app.state.http, T.BASE, 6)
                app_main.app.state.staging = staging_mod.MemoryStagingStore()
                handle = T.open_session(c)

                replies = ([_vision_message(p) for p in plans] if doc
                           else [T.plan_message(p) for p in plans])
                rec = RecordingClient(T.FakeClaude(*replies), case["id"], root=rec_dir)
                app_main.app.state.llm = rec

                ok = True
                if doc:
                    data = fixtures.get(doc["fixture"])
                    state["bytes"] = data
                    state["prefix"] = "lab" if doc["doc_type"] == "lab_pdf" else "intake"
                    r = c.post("/api/session/documents", headers={"Authorization": f"Bearer {handle}"},
                               files={"file": (doc["fixture"] + ".pdf", data, "application/pdf")},
                               data={"doc_type": doc["doc_type"]})
                    ok = r.status_code == 200
                    if not ok:
                        print(f"  FAIL {case['id']}: ingest HTTP {r.status_code} {r.text[:100]}")
                else:
                    for turn in case["turns"]:
                        r = T.ask(c, handle, turn["question"])
                        if r.status_code != 200:
                            ok = False
                            print(f"  FAIL {case['id']}: HTTP {r.status_code}")
                            break

                if not ok:
                    failed += 1
                    continue
                path = rec.save()
                blob = json.loads(path.read_text())
                blob["source"] = "fixture"
                path.write_text(json.dumps(blob, indent=2) + "\n")
                written += 1

    print(f"\n  {written} recorded   {skipped} need none   {failed} failed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
