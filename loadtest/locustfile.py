"""Load test for the Clinical Co-Pilot agent (see LOAD_TEST.md). NOT run automatically.

Each simulated physician opens an API session on a synthetic patient, asks for a brief, then asks follow-ups
with think time, like the between-rooms workflow in USERS.md. A small share runs the schedule scan.

Required environment (never commit values):
  COPILOT_TOKENS_FILE   JSON file: [{"access_token": "...", "patient_id": "<uuid or omit>"}], one per virtual user,
                        minted from synthetic demo users (tokens last 1 hour)
Optional:
  SCHEDULE_TOKENS_FILE  same shape, tokens from a schedule (standalone) launch

Run:  locust -f loadtest/locustfile.py --host https://<agent> --users 10 --spawn-rate 2 --run-time 10m --headless --csv results/u10
"""
import itertools
import json
import os
import uuid

from locust import HttpUser, between, events, task

TOKENS = json.load(open(os.environ["COPILOT_TOKENS_FILE"])) if os.environ.get("COPILOT_TOKENS_FILE") else []
SCHEDULE_TOKENS = json.load(open(os.environ["SCHEDULE_TOKENS_FILE"])) if os.environ.get("SCHEDULE_TOKENS_FILE") else []
_next = itertools.cycle(range(max(len(TOKENS), 1)))

FOLLOW_UPS = ["Any allergies or interactions I should know about?", "What changed since the last visit?",
              "Show the trend on creatinine", "Is it safe to start amoxicillin?"]


@events.test_start.add_listener
def _check(environment, **_):
    if not TOKENS:
        raise SystemExit("COPILOT_TOKENS_FILE is required (see LOAD_TEST.md)")


class Physician(HttpUser):
    """Between rooms: open the chart, brief, 1-2 follow-ups, move on (think time = walking to the next room)."""
    weight = 9
    wait_time = between(20, 60)

    def on_start(self):
        creds = TOKENS[next(_next)]
        r = self.client.post("/api/sessions", json={k: v for k, v in creds.items() if v}, name="POST /api/sessions")
        self.handle = r.json().get("session_handle") if r.ok else None

    def _ask(self, question: str, name: str):
        if not self.handle:
            return
        headers = {"Authorization": f"Bearer {self.handle}"}
        with self.client.post("/api/session/messages", headers=headers, name=name, catch_response=True,
                              json={"question": question, "client_request_id": str(uuid.uuid4())}) as r:
            body = r.json() if r.ok else {}
            if r.status_code == 401:
                r.failure("session expired")
            elif r.ok and body.get("outcome") == "fail":
                r.failure("fallback answer (counted as error per ALERTS.md)")

    @task(3)
    def brief(self):
        self._ask("Brief me on this patient.", "brief")

    @task(2)
    def follow_up(self):
        self._ask(FOLLOW_UPS[hash(self) % len(FOLLOW_UPS)], "follow_up")


class MorningScan(HttpUser):
    """8:40 AM: one schedule scan per physician."""
    weight = 1
    wait_time = between(120, 300)

    def on_start(self):
        self.handle = None
        if SCHEDULE_TOKENS:
            r = self.client.post("/api/sessions", json=SCHEDULE_TOKENS[0], name="POST /api/sessions (schedule)")
            self.handle = r.json().get("session_handle") if r.ok else None

    @task
    def scan(self):
        if self.handle:
            self.client.post("/api/schedule/scan", headers={"Authorization": f"Bearer {self.handle}"}, name="schedule_scan")
