"""Live eval tier (ARCHITECTURE §9): runs evals/cases/*.json against a running agent with real Claude, as real OpenEMR
role users, on synthetic patients. Writes evals/results/<timestamp>.json and prints a summary.

Local (default): tokens are minted in the local OpenEMR container with deploy/local/mint_token.php, using two
files from your own setup that are NOT in this repository - local-smart-client.json (your local SMART client id)
and local-edge-patients.json (the edge-case patient uuids seed_edge_cases.php printed). Both are looked for in
../tools/ next to the repository; override with EVAL_TOOLS_DIR. See evals/README.md.
  python evals/run_evals.py                      # all cases
  python evals/run_evals.py S01 X01              # selected cases
Requires: httpx; the local stack running (deploy/local); agent ALLOW_API_SESSIONS=true with the patients in
EVAL_PATIENT_IDS. Synthetic data only. Spends Anthropic credits (about $0.006 per question, measured).
"""
import glob
import json
import os
import re
import subprocess
import sys
import time
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parent.parent
AGENT = os.environ.get("EVAL_AGENT_URL", "http://localhost:8000")
TOOLS = Path(os.environ.get("EVAL_TOOLS_DIR", ROOT.parent / "tools"))
OPENEMR_CONTAINER = os.environ.get("EVAL_OPENEMR_CONTAINER", "agentforge-local-openemr-1")
AGENT_CONTAINER = os.environ.get("EVAL_AGENT_CONTAINER", "agentforge-local-agent-1")
FIX = ROOT / "agent" / "tests" / "fixtures"

EDGE = json.loads((TOOLS / "local-edge-patients.json").read_text())["patients"]
PATIENTS = {**EDGE, "A": json.loads((FIX / "patient_a.json").read_text())["patient_uuid"],
            "B": json.loads((FIX / "patient_b.json").read_text())["patient_uuid"],
            "NOT_ALLOWED": "00000000-0000-0000-0000-000000000000"}
PLACEHOLDERS = {"OTHER_PATIENT_ALLERGY": "AllergyIntolerance/" + json.loads((FIX / "edge_patients.json").read_text())
                ["E1"]["AllergyIntolerance"]["entry"][0]["resource"]["id"]}
GLOBAL_FORBIDDEN = [(re.compile(r"\bnormal\b", re.I), "the word 'normal'"), (re.compile(r"\b(started|stopped)\b", re.I),
                    "'started'/'stopped' in rendered lines"), (re.compile(r"\{entry\.value\}"), "template placeholder")]
CHART_TITLE = re.compile(r"^(Problem|Finding recorded|Situation recorded|Procedure recorded|Event recorded|Condition|Allergy):")


def mint(user: str, cache={}) -> str:
    if user not in cache:
        client_id = json.loads((TOOLS / "local-smart-client.json").read_text())["client_id"]
        out = subprocess.run(["docker", "exec", OPENEMR_CONTAINER, "su-exec", "apache", "php", "/tmp/mint_token.php",
                              client_id, user], capture_output=True, text=True, check=True).stdout
        cache[user] = json.loads(out)["access_token"]
    return cache[user]


def open_session(case: dict) -> tuple:
    if case.get("session") == "invalid":
        return 200, "invalid-handle"
    body = {"access_token": mint(case["user"])}
    if case.get("session_kind") == "schedule":
        body["kind"] = "schedule"
    else:
        body["patient_id"] = PATIENTS[case["patient"]]
    r = httpx.post(f"{AGENT}/api/sessions", json=body, timeout=30)
    if r.status_code != 200:
        return r.status_code, None
    handle = r.json()["session_handle"]
    for _ in range(80):  # wait for the launch prefetch
        s = httpx.get(f"{AGENT}/api/session", headers={"Authorization": f"Bearer {handle}"}, timeout=10).json()
        if s.get("kind") == "schedule" or (s.get("load_statuses") and "pending" not in s["load_statuses"].values()):
            break
        time.sleep(0.25)
    return 200, handle


def texts(body: dict) -> dict:
    lines = [ln for s in body.get("sections", []) for ln in s.get("lines", [])]
    return {"lines": lines, "line_text": "\n".join(ln["text"] for ln in lines),
            "coverage": "\n".join(c["text"] for c in body.get("coverage", [])),
            "notice": body.get("notice") or "", "clarify": "\n".join(c["text"] for c in body.get("clarify", [])),
            "flags": body.get("flags", [])}


def check_turn(expect: dict, r: httpx.Response, latency: float) -> list:
    fails = []
    want_http = expect.get("http", 200)
    if r.status_code != want_http:
        return [f"http {r.status_code} != {want_http}"]
    for s in expect.get("body_excludes", []):
        if s in r.text:
            fails.append(f"body contains {s!r}")
    if want_http != 200:
        if r.headers.get("X-Correlation-ID") != r.json().get("error", {}).get("correlation_id"):
            fails.append("error envelope correlation id mismatch")
        return fails
    body = r.json()
    t = texts(body)
    everything = "\n".join([t["line_text"], t["coverage"], t["notice"], t["clarify"]] + [f["message"] for f in t["flags"]])
    low = everything.lower()
    # global invariants (every answer)
    if r.headers.get("X-Correlation-ID") != body.get("correlation_id"):
        fails.append("correlation id header != body")
    if any(not ln["source_ids"] for ln in t["lines"]):
        fails.append("a rendered line has no source")
    for rx, what in GLOBAL_FORBIDDEN:
        # Chart titles are rendered verbatim (a Synthea finding can be named "Normal pregnancy"); the invariant is that
        # the SERVER never calls a result normal (AUDIT DQ-7), so condition and allergy title lines are exempt.
        checked = "\n".join(ln["text"] for ln in t["lines"] if not CHART_TITLE.match(ln["text"]))
        if rx.search(checked):
            fails.append(f"invariant: {what}")
    if latency > expect.get("max_latency_s", 10):
        fails.append(f"latency {latency:.2f}s > {expect.get('max_latency_s', 10)}s")
    # case assertions
    if "outcome_in" in expect and body["outcome"] not in expect["outcome_in"]:
        fails.append(f"outcome {body['outcome']} not in {expect['outcome_in']}")
    for s in expect.get("text_contains", []):
        if s.lower() not in low:
            fails.append(f"missing text {s!r}")
    if expect.get("text_contains_any") and not any(s.lower() in low for s in expect["text_contains_any"]):
        fails.append(f"none of {expect['text_contains_any']}")
    for s in expect.get("text_excludes", []):
        if s.lower() in t["line_text"].lower() or s.lower() in t["coverage"].lower():
            fails.append(f"forbidden text {s!r}")
    for s in expect.get("coverage_contains", []):
        if s.lower() not in t["coverage"].lower():
            fails.append(f"coverage missing {s!r}")
    for s in expect.get("notice_contains", []):
        if s.lower() not in t["notice"].lower():
            fails.append(f"notice missing {s!r}")
    ids = {f["rule_id"] for f in t["flags"]}
    for rid in expect.get("flags_include", []):
        if rid not in ids:
            fails.append(f"flag {rid} missing (got {sorted(ids)})")
    if expect.get("high_flag_required") and not any(f["severity"] == "high" for f in t["flags"]):
        fails.append("no high-severity flag")
    if expect.get("high_flag_forbidden") and any(f["severity"] == "high" for f in t["flags"]):
        fails.append(f"unexpected high flag {sorted(ids)}")
    for s in expect.get("flag_message_excludes", []):
        if any(s in f["message"] for f in t["flags"]):
            fails.append(f"flag message contains {s!r}")
    if "max_lines" in expect and len(t["lines"]) > expect["max_lines"]:
        fails.append(f"{len(t['lines'])} lines > {expect['max_lines']}")
    if "withheld_max" in expect and body["withheld_count"] > expect["withheld_max"]:
        fails.append(f"withheld {body['withheld_count']} > {expect['withheld_max']}")
    if expect.get("no_duplicate_lines"):
        dup = [x for x, n in Counter(ln["text"] for ln in t["lines"]).items() if n > 1]
        if dup:
            fails.append(f"duplicate lines: {dup[:2]}")
    if expect.get("all_allergies_listed"):
        m = re.search(r"Allergies: (\d+) recorded", t["coverage"])
        shown = sum(ln["text"].startswith("Allergy:") for ln in t["lines"])
        if m and shown < int(m.group(1)):
            fails.append(f"{shown} allergy lines < {m.group(1)} recorded")
    if expect.get("lines_exclude_source"):
        sid = PLACEHOLDERS[expect["lines_exclude_source"]]
        if any(sid in ln["source_ids"] for ln in t["lines"]):
            fails.append("another patient's record was rendered")
    if expect.get("text_or_clarify_contains"):
        if not any(s.lower() in (t["line_text"] + t["clarify"]).lower() for s in expect["text_or_clarify_contains"]):
            fails.append(f"neither answer nor clarify mentions {expect['text_or_clarify_contains']}")
    return fails


def run_case(case: dict) -> dict:
    status, handle = open_session(case)
    result = {"id": case["id"], "use_case": case["use_case"], "category": case["category"], "user": case["user"],
              "failure_mode_guarded": case["failure_mode_guarded"], "turns": [], "failures": []}
    want = case.get("session_expect", {}).get("http", 200)
    if status != want:
        result["failures"].append(f"session http {status} != {want}")
    if status != 200 or handle is None:
        result["passed"] = not result["failures"]
        return result
    if case.get("session_kind") == "schedule":
        t0 = time.monotonic()
        r = httpx.post(f"{AGENT}/api/schedule/scan", headers={"Authorization": f"Bearer {handle}"}, timeout=90)
        latency, exp, fails = time.monotonic() - t0, case["scan_expect"], []
        counts = r.json().get("counts", {}) if r.status_code == 200 else {}
        if r.status_code != 200:
            fails.append(f"scan http {r.status_code}")
        if counts.get("scheduled") != exp["scheduled"]:
            fails.append(f"scheduled {counts.get('scheduled')} != {exp['scheduled']}")
        if counts.get("failed") != exp["failed"]:
            fails.append(f"failed {counts.get('failed')} != {exp['failed']}")
        if counts.get("flagged", 0) < exp["flagged_min"]:
            fails.append(f"flagged {counts.get('flagged')} < {exp['flagged_min']}")
        if latency > exp["max_latency_s"]:
            fails.append(f"latency {latency:.1f}s")
        result["turns"].append({"scan": counts, "latency_s": round(latency, 2), "correlation_id": r.headers.get("X-Correlation-ID"),
                                "failures": fails})
        result["failures"] += fails
    for turn in case.get("turns", []):
        q = turn["question"]
        if q.startswith("REPEAT:"):
            _, n, ch = q.split(":")
            q = ch * int(n)
        payload = {"question": q}
        if turn.get("selected_source_id"):
            payload["selected_source_id"] = PLACEHOLDERS.get(turn["selected_source_id"], turn["selected_source_id"])
        t0 = time.monotonic()
        r = httpx.post(f"{AGENT}/api/session/messages", headers={"Authorization": f"Bearer {handle}"}, json=payload, timeout=40)
        latency = time.monotonic() - t0
        fails = check_turn(turn.get("expect", {}), r, latency)
        body = r.json() if r.headers.get("content-type", "").startswith("application/json") else {}
        result["turns"].append({"question": q[:120], "http": r.status_code, "outcome": body.get("outcome"),
                                "latency_s": round(latency, 2), "flags": sorted({f["rule_id"] for f in body.get("flags", [])}),
                                "lines": len([1 for s in body.get("sections", []) for _ in s.get("lines", [])]),
                                "withheld": body.get("withheld_count"), "correlation_id": r.headers.get("X-Correlation-ID"),
                                "failures": fails})
        result["failures"] += fails
    result["passed"] = not result["failures"]
    return result


def llm_costs(cids: set) -> dict:
    """Sum Claude cost per correlation id from the local agent's non-PHI 'answer' log lines."""
    out = subprocess.run(["docker", "logs", AGENT_CONTAINER], capture_output=True, text=True).stdout.splitlines()
    costs = {}
    for line in out + subprocess.run(["docker", "logs", AGENT_CONTAINER], capture_output=True, text=True).stderr.splitlines():
        if '"msg": "answer"' in line:
            d = json.loads(line)
            if d["correlation_id"] in cids:
                costs[d["correlation_id"]] = d
    return costs


def main():
    cases = [c for f in sorted(glob.glob(str(ROOT / "evals" / "cases" / "*.json"))) for c in json.loads(Path(f).read_text())]
    if sys.argv[1:]:
        cases = [c for c in cases if c["id"] in sys.argv[1:]]
    subprocess.run(["docker", "cp", str(ROOT / "deploy/local/mint_token.php"), f"{OPENEMR_CONTAINER}:/tmp/mint_token.php"],
                   check=True, capture_output=True)
    started = datetime.now(timezone.utc)
    results = []
    for case in cases:
        res = run_case(case)
        results.append(res)
        mark = "PASS" if res["passed"] else "FAIL"
        lat = ", ".join(f"{t['latency_s']}s" for t in res["turns"])
        print(f"{mark} {res['id']} [{res['category']}/{res['use_case']}] {lat}" + ("" if res["passed"] else f"  -> {res['failures']}"))
    cids = {t["correlation_id"] for r in results for t in r["turns"] if t.get("correlation_id")}
    costs = llm_costs(cids)
    answered = [t for r in results for t in r["turns"] if t.get("outcome")]
    lat = sorted(t["latency_s"] for t in answered)
    by = lambda key: {k: f"{sum(r['passed'] for r in v)}/{len(v)}" for k, v in sorted(_group(results, key).items())}
    summary = {"started_at": started.isoformat(timespec="seconds"), "agent": AGENT,
               "commit": subprocess.run(["git", "-C", str(ROOT), "rev-parse", "--short", "HEAD"], capture_output=True, text=True).stdout.strip(),
               "cases": len(results), "passed": sum(r["passed"] for r in results), "by_category": by("category"),
               "by_use_case": by("use_case"),
               "latency_s": {"p50": _pct(lat, 50), "p95": _pct(lat, 95), "max": lat[-1] if lat else None, "n": len(lat)},
               "llm_cost_usd": round(sum(c["llm_cost_usd"] for c in costs.values()), 4),
               "outcomes": dict(Counter(t["outcome"] for t in answered))}
    stamp = started.strftime("%Y%m%dT%H%M%SZ")
    path = ROOT / "evals" / "results" / f"{stamp}.json"
    path.write_text(json.dumps({"summary": summary, "results": results}, indent=1))
    print(json.dumps(summary, indent=1))
    print(f"wrote {path.relative_to(ROOT)}")


def _group(results, key):
    g = defaultdict(list)
    for r in results:
        g[r[key]].append(r)
    return g


def _pct(values, p):
    if not values:
        return None
    return values[min(len(values) - 1, round(p / 100 * (len(values) - 1)))]


if __name__ == "__main__":
    main()
