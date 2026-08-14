#!/bin/zsh
# EV boot / health status board (PULSE WAVE LIFE).
#
# Verifies, in one shot: launchd services, API health, runtime daemon ticks,
# fleet registry + push readiness, notification backend, and the 72 h soak
# audit. Exit code is non-zero when any check fails, so the menu-bar app or a
# cron/launchd job can surface "EVIE is NOT alive" without a terminal.

set -uo pipefail

API_URL="${EV_API_URL:-http://127.0.0.1:8000}"
MASTER_KEY="${EV_MASTER_KEY:-}"
BACKEND_DIR="$(cd "$(dirname "$0")/../backend" && pwd)"
FAILED=0

say() { print -r -- "[ev] $*"; }

if [[ -z "$MASTER_KEY" ]]; then
  say "EV_MASTER_KEY is not set; auth checks will fail"
  FAILED=1
fi

AUTH=()
[[ -n "$MASTER_KEY" ]] && AUTH=(-H "Authorization: Bearer $MASTER_KEY")

say "launchd services:"
for label in api opencode ears runtime scheduler worker collector; do
  if launchctl print "gui/$UID/ev.$label" >/dev/null 2>&1; then
    say "  ev.$label: loaded"
  else
    say "  ev.$label: NOT loaded"
    FAILED=1
  fi
done

health="$(curl -s --max-time 5 "$API_URL/v1/health" 2>/dev/null)"
if [[ "$health" == *'"status":"ok"'* || "$health" == *'"status": "ok"'* ]]; then
  say "api /v1/health: ok"
else
  say "api /v1/health: FAILED ($health)"
  FAILED=1
fi

runtime="$(curl -s --max-time 5 "${AUTH[@]}" "$API_URL/v1/runtime/health" 2>/dev/null)"
if [[ "$runtime" == *'"overall":"ok"'* || "$runtime" == *'"overall": "ok"'* ]]; then
  say "runtime health: ok"
elif [[ "$runtime" == *'"overall":"degraded"'* || "$runtime" == *'"overall": "degraded"'* ]]; then
  say "runtime health: degraded (see /v1/runtime/health)"
else
  say "runtime health: FAILED"
  FAILED=1
fi

devices="$(curl -s --max-time 5 "${AUTH[@]}" "$API_URL/v1/devices" 2>/dev/null)"
if [[ "$devices" == *'"name":"Mac"'* && "$devices" == *'"name":"Phone A"'* && "$devices" == *'"name":"Phone B"'* ]]; then
  say "fleet registry: Mac + Phone A + Phone B present"
else
  say "fleet registry: incomplete; run: cd backend && uv run python -m app.notify.registry --tokens"
  FAILED=1
fi

notify="$(curl -s --max-time 5 "${AUTH[@]}" "$API_URL/v1/runtime/notify/status" 2>/dev/null)"
if [[ "$notify" == *'"available":true'* || "$notify" == *'"available": true'* ]]; then
  say "notification backend: available"
else
  say "notification backend: unavailable ($notify)"
  FAILED=1
fi

if (cd "$BACKEND_DIR" && EV_VAULT_KEY="${EV_VAULT_KEY:-test-vault-key-0123456789abcdef}" \
    uv run python -m app.workers.runtime_healthcheck --soak >/dev/null 2>&1); then
  say "soak audit: healthy"
else
  say "soak audit: gap detected (or daemon not started)"
  FAILED=1
fi

if [[ "$FAILED" -eq 0 ]]; then
  say "EVIE is alive."
else
  say "EVIE has failing checks; see docs/WAVE_LIFE.md recovery section."
fi
exit "$FAILED"
