#!/bin/sh
# Load test against the DEPLOYED agent (LOAD_TEST.md "Running it against the deployed agent").
#
#   sh loadtest/run_deployed.sh <token-file> <users> <spawn_rate> <run_time> <label>
#   sh loadtest/run_deployed.sh ~/copilot-token.txt 10 2 5m deployed-l10
#
# <token-file> holds ONE OAuth access token for the demo physician, from a real browser login against the deployed
# OpenEMR (deploy/local/mint_token.php refuses any non-localhost site by design, so there is no shortcut here).
# The token must carry user/ scopes and NOT launch/patient: with a launch context the session binds to that one
# patient and ignores the patient_id each virtual user sends.
#
# Raise MAX_SESSIONS_PER_USER on the Railway agent before running and set it back to 3 afterwards - the cap is the
# server's, not Locust's, and every virtual user here launches off the same demo account.
set -e

REPO=$(cd "$(dirname "$0")/.." && pwd)
WORKBENCH=${WORKBENCH:-$(dirname "$REPO")}
RESULTS=${RESULTS:-$WORKBENCH/loadtest-results}
AGENT=${AGENT:-https://agent-production-e0ed.up.railway.app}
LOCUST_IMAGE=${LOCUST_IMAGE:-agentforge-locust}
[ -f "$WORKBENCH/tools/docker-env.sh" ] && . "$WORKBENCH/tools/docker-env.sh"

TOKEN_FILE=$1; USERS=$2; RATE=$3; TIME=$4; LABEL=$5
[ -n "$LABEL" ] || { echo "usage: sh loadtest/run_deployed.sh <token-file> <users> <spawn_rate> <run_time> <label>"; exit 2; }
TOKEN=$(tr -d ' \t\n\r"' < "$TOKEN_FILE")
[ -n "$TOKEN" ] || { echo "FATAL: $TOKEN_FILE is empty"; exit 1; }
mkdir -p "$RESULTS"

# Patients the deployed agent will accept on POST /api/sessions (its EVAL_PATIENT_IDS allowlist).
PATIENTS=${PATIENTS:-a2c4925c-53ef-4fc8-96bc-192cb0b79e34,a2c4929d-cdba-4498-b614-d5ede089cc08,a2c492b0-0474-48ed-a44d-85e50f7af5ae,a2c491fd-e29e-4ecb-97fe-2a46ed608c75}
python3 - "$TOKEN" "$RESULTS" "$PATIENTS" <<'PY'
import json, pathlib, sys
token, out, pats = sys.argv[1], pathlib.Path(sys.argv[2]), sys.argv[3].split(",")
p = out / "tokens-deployed.json"
p.write_text(json.dumps([{"access_token": token, "patient_id": x} for x in pats]))
p.chmod(0o600)
PY

# Fail fast rather than burning the whole run on 401s from an expired or wrong-scoped token.
CHECK=$(curl -s -o /dev/null -w '%{http_code}' -m 30 -X POST "$AGENT/api/sessions" -H 'content-type: application/json' \
        -d "$(python3 -c "import json;print(json.dumps(json.load(open('$RESULTS/tokens-deployed.json'))[0]))")")
[ "$CHECK" = "200" ] || { echo "FATAL: session create returned $CHECK (401 = token expired or wrong scopes; 403 = patient not in EVAL_PATIENT_IDS)"; exit 1; }
echo "$LABEL: token ok against $AGENT, starting $USERS users for $TIME"

docker run --rm -v "$RESULTS":/work -v "$REPO/loadtest":/lt \
    -e COPILOT_TOKENS_FILE=/work/tokens-deployed.json "$LOCUST_IMAGE" \
    locust -f /lt/locustfile.py --host "$AGENT" \
    --users "$USERS" --spawn-rate "$RATE" --run-time "$TIME" --headless --only-summary \
    --csv "/work/$LABEL" 2>&1 | tail -14
echo "$LABEL complete: $RESULTS/$LABEL*.csv"
