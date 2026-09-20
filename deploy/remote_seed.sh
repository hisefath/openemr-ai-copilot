#!/bin/sh
# Run one of the deploy/local seeder scripts inside the DEPLOYED OpenEMR service.
#
#   sh deploy/remote_seed.sh seed_demo.php        # refresh today's demo schedule and users
#   sh deploy/remote_seed.sh seed_edge_cases.php  # the edge-case patients
#
# The scripts are not in the OpenEMR image (it is the stock image plus the skin), and Railway has no `docker cp`, so
# the script is base64'd into the ssh command and run as the web user. Same script, same result as locally.
# Demo/synthetic data only. Requires: railway login, and the project linked (`railway link`).
set -e
SCRIPT=${1:-seed_demo.php}
SERVICE=${SERVICE:-openemr}
SRC=$(cd "$(dirname "$0")" && pwd)/local/$SCRIPT
[ -f "$SRC" ] || { echo "no such seeder: $SRC"; exit 2; }

B64=$(base64 < "$SRC" | tr -d '\n')
railway ssh --service "$SERVICE" \
    "sh -c 'echo $B64 | base64 -d > /tmp/$SCRIPT && su-exec apache php /tmp/$SCRIPT'"
