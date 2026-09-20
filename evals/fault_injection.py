"""Fires one ALERTS.md alert on purpose against the local stack, then evaluates that window with agent/copilot/alerts.py.

A second agent container (127.0.0.1:8001) runs with one fault injected; the normal local agent is left alone.
  A1  Claude is slow: Anthropic calls go through a proxy that adds 9 s (question deadline raised to 15 s)
  A2  Claude rejects the API key (invalid key)
  A3  FHIR calls fail (FHIR base URL points at a path OpenEMR doesn't serve; OAuth still works)
  python evals/fault_injection.py A1|A2|A3
22 sequential questions over 4 sessions: enough for the 20-request minimum, not a load test. A1 and A3 spend about
$0.05 of Anthropic credit each; A2 spends nothing. Synthetic data only. Requires the local stack (deploy/local).
"""
import json
import os
import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone

os.environ["EVAL_AGENT_URL"] = "http://localhost:8001"
sys.path.insert(0, os.path.dirname(__file__))
import httpx  # noqa: E402
import run_evals  # noqa: E402

ROOT = run_evals.ROOT
NETWORK = "agentforge-local_default"
QUESTIONS_ASKED = 22
AGENT, PROXY = "agentforge-fault-agent", "agentforge-fault-slow-claude"
QUESTIONS = ["Brief me on this patient.", "What are the active medications?", "Any allergies?",
             "What are the most recent labs?", "What are the active problems?", "Anything I should worry about today?"]
SLOW_PROXY = r"""
import http.server, time, urllib.error, urllib.request
SKIP = {"host", "content-length", "accept-encoding", "connection", "transfer-encoding", "content-encoding"}
class H(http.server.BaseHTTPRequestHandler):
    def do_POST(self):
        body = self.rfile.read(int(self.headers.get("content-length", 0)))
        time.sleep(9)
        req = urllib.request.Request("https://api.anthropic.com" + self.path, data=body, method="POST",
                                     headers={k: v for k, v in self.headers.items() if k.lower() not in SKIP})
        try:
            resp = urllib.request.urlopen(req, timeout=60)
        except urllib.error.HTTPError as e:
            resp = e
        data = resp.read()
        self.send_response(resp.status)
        for k, v in resp.headers.items():
            if k.lower() not in SKIP:
                self.send_header(k, v)
        self.send_header("content-length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)
http.server.ThreadingHTTPServer(("0.0.0.0", 8080), H).serve_forever()
"""


def env_value(key: str) -> str:
    for line in (ROOT / "agent" / ".env.local").read_text().splitlines():
        if line.startswith(key + "="):
            return line.split("=", 1)[1].strip().strip('"')
    raise SystemExit(f"{key} missing from agent/.env.local")


def docker(*args: str) -> str:
    return subprocess.run(["docker", *args], capture_output=True, text=True, check=True).stdout.strip()


def fault_env(fault: str) -> dict:
    return {"A1": {"QUESTION_DEADLINE_S": "15", "ANTHROPIC_BASE_URL": f"http://{PROXY}:8080"},
            "A2": {"ANTHROPIC_API_KEY": "sk-ant-invalid-fault-injection"},
            "A3": {"OPENEMR_FHIR_BASE": env_value("OPENEMR_FHIR_BASE").rstrip("/") + "/fault-injection"}}[fault]


def start(fault: str) -> None:
    env = fault_env(fault)
    if fault == "A1":
        docker("run", "-d", "--rm", "--name", PROXY, "--network", NETWORK, "python:3.12-slim", "python", "-c", SLOW_PROXY)
    docker("run", "-d", "--rm", "--name", AGENT, "--network", NETWORK, "-p", "127.0.0.1:8001:8000",
           "--env-file", str(ROOT / "agent/.env"), "--env-file", str(ROOT / "agent/.env.local"), "-e", "PORT=8000",
           *[a for k, v in env.items() for a in ("-e", f"{k}={v}")],
           "-v", f"{ROOT / 'deploy/local/certs/ca.pem'}:/certs/ca.pem:ro", "agentforge-local-agent:latest")
    for _ in range(60):  # bounded: 60 s
        try:
            if httpx.get("http://localhost:8001/health", timeout=2).status_code == 200:
                return
        except httpx.HTTPError:
            pass
        if AGENT not in docker("ps", "--format", "{{.Names}}"):
            raise SystemExit("fault agent exited: " + docker("logs", "--tail", "5", AGENT))
        time.sleep(1)
    raise SystemExit("fault agent not healthy after 60 s")


def stop() -> None:
    for name in (AGENT, PROXY):
        subprocess.run(["docker", "stop", "-t", "5", name], capture_output=True)


def ask(n: int, per_session: int = 6) -> None:
    handle = None
    for i in range(n):
        if i % per_session == 0:
            status, handle = run_evals.open_session({"user": "dr_chen", "patient": "A"})
            if status != 200:
                raise SystemExit(f"session create failed: HTTP {status}")
        t0 = time.monotonic()
        r = httpx.post(f"{run_evals.AGENT}/api/session/messages", headers={"Authorization": f"Bearer {handle}"},
                       json={"question": QUESTIONS[i % len(QUESTIONS)]}, timeout=40)
        body = r.json() if r.headers.get("content-type", "").startswith("application/json") else {}
        print(f"  q{i + 1:02d} http={r.status_code} outcome={body.get('outcome')} {time.monotonic() - t0:.1f}s", flush=True)


def evaluate(start_at: datetime, end_at: datetime, deadline_s: str) -> dict:
    """copilot.alerts over exactly this run's window, retried until Langfuse has ingested every request (bounded: 15 min;
    ingestion was measured at 1-8 min)."""
    window = [start_at.isoformat().replace("+00:00", "Z"), end_at.isoformat().replace("+00:00", "Z")]
    deadline = time.monotonic() + 900
    while True:
        out = subprocess.run(["docker", "run", "--rm", "-v", f"{ROOT / 'agent'}:/app", "-w", "/app",
                              "--env-file", str(ROOT / "agent/.env"), "-e", "ALERT_ENVIRONMENT=default", "-e", f"QUESTION_DEADLINE_S={deadline_s}",
                              "agentforge-agent-dev", "python", "-m", "copilot.alerts", *window],
                             capture_output=True, text=True).stdout.strip().splitlines()
        result = json.loads(out[-1]) if out else {"results": []}
        if (result["results"] and result["results"][0]["requests"] >= QUESTIONS_ASKED) or time.monotonic() > deadline:
            return result
        time.sleep(30)


def main() -> None:
    fault = sys.argv[1] if len(sys.argv) == 2 else ""
    if fault not in ("A1", "A2", "A3"):
        raise SystemExit(__doc__)
    try:
        start(fault)
        began = datetime.now(timezone.utc) - timedelta(seconds=5)
        print(f"{fault}: fault agent up, asking questions", flush=True)
        ask(QUESTIONS_ASKED)
        ended = datetime.now(timezone.utc) + timedelta(seconds=5)
        time.sleep(10)  # let the agent's Langfuse client flush
    finally:
        stop()
    print(f"{fault}: waiting for Langfuse ingestion", flush=True)
    result = evaluate(began, ended, fault_env(fault).get("QUESTION_DEADLINE_S", "9"))
    print(json.dumps(result, indent=1))


if __name__ == "__main__":
    main()
