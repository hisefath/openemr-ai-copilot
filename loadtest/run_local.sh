#!/bin/sh
# Local load test against the deploy/local stack (LOAD_TEST.md).
#
#   sh loadtest/run_local.sh <users> <spawn_rate> <run_time> <label>
#   sh loadtest/run_local.sh 10 2 5m l10
#
# Why this exists rather than a bare locust command:
#   * OpenEMR access tokens last an hour, and an expired one turns every session create into a 403 — one run was
#     lost to exactly that — so the token is minted immediately before the run and the run aborts unless a session
#     create returns 200.
#   * Every virtual user launches off the same demo account, and the agent caps live sessions per user at 3, so the
#     cap is raised for this run only (never in deployment) and the agent is recreated to pick it up.
#   * CPU and memory have to be sampled alongside the run to produce the baselines the assignment asks for.
#
# Results land in $RESULTS (default: ../loadtest-results, outside the repository, since they contain a token file).
set -e

REPO=$(cd "$(dirname "$0")/.." && pwd)
WORKBENCH=${WORKBENCH:-$(dirname "$REPO")}          # holds the demo client id and edge-patient uuids
RESULTS=${RESULTS:-$WORKBENCH/loadtest-results}
SMART_CLIENT=${SMART_CLIENT:-$WORKBENCH/tools/local-smart-client.json}
EDGE_PATIENTS=${EDGE_PATIENTS:-$WORKBENCH/tools/local-edge-patients.json}
AGENT_URL=${AGENT_URL:-http://localhost:8000}
OPENEMR_CONTAINER=${OPENEMR_CONTAINER:-agentforge-local-openemr-1}
LOCUST_IMAGE=${LOCUST_IMAGE:-agentforge-locust}     # docker build -t agentforge-locust - <<< 'FROM python:3.12-slim
                                                    # RUN pip install locust==2.32.4'
[ -f "$WORKBENCH/tools/docker-env.sh" ] && . "$WORKBENCH/tools/docker-env.sh"

USERS=$1; RATE=$2; TIME=$3; LABEL=$4
[ -n "$LABEL" ] || { echo "usage: sh loadtest/run_local.sh <users> <spawn_rate> <run_time> <label>"; exit 2; }
mkdir -p "$RESULTS"

export MAX_SESSIONS_PER_USER=$(( USERS * 3 ))
docker compose -f "$REPO/deploy/local/compose.yml" up -d --build agent >/dev/null 2>&1
end=$(( $(date +%s) + 120 ))
until [ "$(curl -s -o /dev/null -w '%{http_code}' -m 5 "$AGENT_URL/health")" = "200" ]; do
    [ "$(date +%s)" -lt "$end" ] || { echo "FATAL: agent did not come up"; exit 1; }
    sleep 3
done

CID=$(python3 -c "import json;print(json.load(open('$SMART_CLIENT'))['client_id'])")
TOKEN=$(docker exec "$OPENEMR_CONTAINER" su-exec apache php /tmp/mint_token.php "$CID" dr_chen \
        | python3 -c "import json,sys;print(json.load(sys.stdin)['access_token'])")
python3 - "$TOKEN" "$RESULTS" "$EDGE_PATIENTS" "$REPO" <<'PY'
import json, pathlib, sys
token, out, edge, repo = sys.argv[1], pathlib.Path(sys.argv[2]), pathlib.Path(sys.argv[3]), pathlib.Path(sys.argv[4])
fix = repo / "agent" / "tests" / "fixtures"
pats = [json.loads(edge.read_text())["patients"][k] for k in ("E1", "E2", "E3", "E4")]
pats += [json.loads((fix / f).read_text())["patient_uuid"] for f in ("patient_a.json", "patient_b.json")]
p = out / "tokens.json"
p.write_text(json.dumps([{"access_token": token, "patient_id": x} for x in pats]))
p.chmod(0o600)   # a bearer token for a demo physician: not a secret worth keeping, still not world-readable
PY
CHECK=$(curl -s -o /dev/null -w '%{http_code}' -m 20 -X POST "$AGENT_URL/api/sessions" \
        -H 'content-type: application/json' \
        -d "$(python3 -c "import json;print(json.dumps(json.load(open('$RESULTS/tokens.json'))[0]))")")
[ "$CHECK" = "200" ] || { echo "FATAL: session create returned $CHECK, not running $LABEL"; exit 1; }
echo "$LABEL: token ok, starting $USERS users for $TIME"

# Sample CPU/memory until the run is over. Bounded so a failed run can never leave a sampler behind.
( end=$(( $(date +%s) + 900 ))
  while [ "$(date +%s)" -lt "$end" ]; do
      docker stats --no-stream --format "{{.Name}},{{.CPUPerc}},{{.MemUsage}}" \
          agentforge-local-agent-1 "$OPENEMR_CONTAINER" agentforge-local-mysql-1 >> "$RESULTS/$LABEL-stats.csv" 2>/dev/null
      sleep 10
  done ) &
SAMPLER=$!

docker run --rm --network agentforge-local_default -v "$RESULTS":/work -v "$REPO/loadtest":/lt \
    -e COPILOT_TOKENS_FILE=/work/tokens.json "$LOCUST_IMAGE" \
    locust -f /lt/locustfile.py --host http://agentforge-local-agent-1:8000 \
    --users "$USERS" --spawn-rate "$RATE" --run-time "$TIME" --headless --only-summary \
    --csv "/work/$LABEL" 2>&1 | tail -12

kill $SAMPLER 2>/dev/null || true
echo "$LABEL complete: $RESULTS/$LABEL*.csv"
