#!/usr/bin/env python3
"""Bootstrap recordings from each case's `fixture_plan`, with no API key and no spend.

    python evals/w2/bootstrap_recordings.py

What this is and is not. The recordings it writes carry a REAL surface key -- computed from the exact kwargs the
running app assembles, so the keying rule in replay.py is exercised for real -- but a FIXTURE response rather than
one Claude actually produced. That is the right trade for building the gate before the model work exists: the gate
is provably able to fail today, and the responses get replaced by live captures later without any change to the
harness.

Re-record against live Claude with `--record` on the gate once the Week 2 flow lands. Until then a recording's
`source` field says `fixture` so nobody mistakes one for evidence of model behaviour.
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


def main() -> int:
    import httpx
    from fastapi.testclient import TestClient

    import test_main as T
    from copilot import fhir, main as app_main
    from replay import RecordingClient

    for k, v in {"OPENEMR_FHIR_BASE": T.BASE, "PUBLIC_ISSUER": T.BASE, "SMART_CLIENT_ID": "copilot",
                 "SMART_CLIENT_SECRET": "test-secret", "ALLOW_API_SESSIONS": "true", "EVAL_PATIENT_IDS": T.PID,
                 "AGENT_PUBLIC_URL": "http://agent.test", "HMAC_KEY": "test-hmac", "ANTHROPIC_API_KEY": "dummy",
                 "LLM_WARMUP": "false"}.items():
        os.environ[k] = v
    for k in ("AUDIT_DB_HOST", "LANGFUSE_PUBLIC_KEY"):
        os.environ.pop(k, None)

    table = {"$PENICILLIN": T.PENICILLIN, "$AMOXICILLIN": T.AMOXICILLIN, "$PID": T.PID}

    targets = [(HERE / "cases", HERE / "recordings"), (HERE / "selftest", HERE / "selftest" / "recordings")]
    written = 0
    for case_dir, rec_dir in targets:
        for f in sorted(case_dir.glob("*.json")):
            cases = json.loads(f.read_text())
            for case in (cases if isinstance(cases, list) else [cases]):
                plans = [_resolve(t["fixture_plan"], table) for t in case["turns"] if "fixture_plan" in t]
                if not plans:
                    print(f"  skip {case['id']}: no fixture_plan")
                    continue
                with TestClient(app_main.app) as c:
                    app_main.app.state.http = httpx.AsyncClient(transport=httpx.MockTransport(T.openemr))
                    app_main.app.state.fhir = fhir.FhirClient(app_main.app.state.http, T.BASE, 6)
                    handle = T.open_session(c)
                    fake = T.FakeClaude(*[T.plan_message(p) for p in plans])
                    rec = RecordingClient(fake, case["id"], root=rec_dir)
                    app_main.app.state.llm = rec
                    for turn in case["turns"]:
                        r = T.ask(c, handle, turn["question"])
                        if r.status_code != 200:
                            print(f"  FAIL {case['id']}: HTTP {r.status_code} {r.text[:120]}")
                            break
                    else:
                        path = rec.save()
                        blob = json.loads(path.read_text())
                        blob["source"] = "fixture"   # not a live capture; see the module docstring
                        path.write_text(json.dumps(blob, indent=2) + "\n")
                        written += 1
                        print(f"  ok   {case['id']}: {len(blob['calls'])} call(s) -> {path.relative_to(REPO)}")
    print(f"\n{written} recording(s) written")
    return 0 if written else 1


if __name__ == "__main__":
    sys.exit(main())
