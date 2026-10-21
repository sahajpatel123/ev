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

# Prefer the SIGNED staged bundle: TCC records grants against a code
# signature, so GUI/permission probes run against the unsigned .build binary
# always read "denied" and prove nothing. Run package.sh first for valid probes.
BIN="$ROOT/build/EV.app/Contents/MacOS/EV"
if [[ ! -x "$BIN" ]]; then
    echo "WARNING: no signed build/EV.app — falling back to the unsigned .build" >&2
    echo "         binary; TCC/permission probes are invalid there. Run package.sh" >&2
    echo "         first for meaningful results." >&2
    BIN_DIR="$(swift build -c release --show-bin-path 2>/dev/null || echo "$ROOT/.build/release")"
    BIN="$BIN_DIR/EV"
fi

exec "$BIN" --smoke-test
