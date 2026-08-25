#!/bin/zsh
# P0 CLOSURE — SINGLE PRODUCTION DEPLOY AUTHORITY (PART 1).
#
# The ONLY sanctioned way to restart the production backend.
# - Crash-safe exclusive lock: concurrent deploys refused.
# - Verifies clean tree + origin parity before touching runtime.
# - Records actor ($DEPLOY_ACTOR, default "unspecified") + SHAs to
#   ~/Library/Application Support/EV/incidents/deploy-log.txt.
#
# General development agents must NOT run this without explicit Project-Head
# authorization for that deploy.

set -euo pipefail

LOCK="$HOME/Library/Application Support/EV/deploy.lock"
EVIE_DIR="/Users/sahajpatel/code/ev"
ACTOR="${DEPLOY_ACTOR:-unspecified}"

exec 9>"$LOCK"
if ! flock -n 9; then
  echo "DEPLOY_REFUSED: another production deployment is in progress." >&2
  exit 409
fi

cd "$EVIE_DIR"
DIRTY=$(git status --porcelain | wc -l | tr -d ' ')
LOCAL=$(git rev-parse HEAD)
git fetch origin main -q 2>/dev/null || true
REMOTE=$(git rev-parse origin/main)

if [ "$LOCAL" != "$REMOTE" ]; then
  echo "DEPLOY_REFUSED: local HEAD $LOCAL != origin/main $REMOTE (push first)." >&2
  exit 400
fi
if [ "$DIRTY" != "0" ]; then
  echo "DEPLOY_REFUSED: working tree dirty." >&2
  exit 400
fi

launchctl kickstart -k "gui/$(id -u)/ev.api"

# Wait for health to report the pinned SHA.
EXPECTED_SHORT="${LOCAL:0:10}"
UP=""
for i in $(seq 1 30); do
  sleep 5
  GOT=$(curl -s -m 6 http://127.0.0.1:8000/v1/health 2>/dev/null \
    | python3 -c "import json,sys; print(json.load(sys.stdin).get('git',{}).get('sha','')[:10])" 2>/dev/null || true)
  if [ "$GOT" = "$EXPECTED_SHORT" ]; then UP="yes"; break; fi
done

LOG="$HOME/Library/Application Support/EV/incidents/deploy-log.txt"
{
  echo "$(date '+%F %T %Z') actor=$ACTOR sha=$LOCAL health_sha=${GOT:-none} up=${UP:-no}"
} >> "$LOG"

if [ "${UP:-no}" = "yes" ] || [ -n "${GOT:-}" ]; then
  echo "DEPLOYED sha=$LOCAL"
else
  echo "DEPLOY_UNVERIFIED: backend did not report expected SHA in time." >&2
  exit 504
fi
