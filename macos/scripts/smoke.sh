#!/bin/zsh
# Headless smoke test for the SUIT client wiring.
#
# Requires a running EV backend (default http://127.0.0.1:8000) and either
# EV_API_KEY in the environment or the repo-root .env (EV_MASTER_KEY).
#
# Usage: ./scripts/smoke.sh

set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"

if [ -z "${EV_API_KEY:-}" ] && [ -f "$ROOT/../.env" ]; then
    set -a
    # shellcheck disable=SC1091
    . "$ROOT/../.env"
    set +a
    EV_API_KEY="${EV_API_KEY:-$EV_MASTER_KEY}"
fi

export EV_API_URL="${EV_API_URL:-http://127.0.0.1:8000}"
export EV_API_KEY="${EV_API_KEY:-dev}"
export EV_DEVICE_ID="${EV_DEVICE_ID:-mac-smoke}"

exec "$ROOT/.build/release/EV" --smoke-test
