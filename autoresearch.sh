#!/usr/bin/env bash
# Workspace deep-analysis health harness (deterministic, offline).
# Primary: mypy type errors over backend/app (real headroom at baseline).
# Secondary: ruff errors, baseline invariant failures, structural size.
set -u
cd "$(dirname "$0")"

BIN="backend/.venv/bin"
VENV_PY="$BIN/python"
RUFF="$BIN/ruff"
# --- 1. mypy errors (primary; deterministic, offline; must run from backend/) ---
MYPY_OUT=$(cd backend && "../$VENV_PY" -m mypy app --no-error-summary --no-pretty 2>&1 || true)
MYPY_ERRORS=$(printf '%s\n' "$MYPY_OUT" | grep -cE ': error: ' || true)

# --- 2. ruff error count ---
RUFF_ERRORS=0
if [ -x "$RUFF" ]; then
  RUFF_OUT=$("$RUFF" check backend/app backend/clients backend/tests --output-format=concise --no-cache 2>&1 || true)
  RUFF_ERRORS=$(printf '%s\n' "$RUFF_OUT" | grep -cE '^[A-Za-z0-9_/.-]+\.py:[0-9]+:[0-9]+: ' || true)
fi

# --- 3. baseline invariant failures ---
INV_FAIL=0
if "$VENV_PY" tools/baseline.py --check >/dev/null 2>&1; then
  INV_FAIL=0
else
  INV_FAIL=$("$VENV_PY" tools/baseline.py --check 2>&1 | grep -c '^  - ' || true)
fi

# --- structural size (context, from baseline json) ---
BASELINE_JSON=$("$VENV_PY" tools/baseline.py --json 2>/dev/null)
get() { printf '%s' "$BASELINE_JSON" | "$VENV_PY" -c "import json,sys; print(json.load(sys.stdin).get('$1',0))"; }

echo "METRIC mypy_errors=${MYPY_ERRORS}"
echo "METRIC ruff_errors=${RUFF_ERRORS}"
echo "METRIC invariant_failures=${INV_FAIL}"
echo "METRIC app_modules=$(get app_python_modules)"
echo "METRIC test_functions=$(get test_functions)"
echo "METRIC route_decorators=$(get api_route_decorators)"
echo "METRIC orm_tables=$(get orm_tables)"
echo "METRIC settings_fields=$(get settings_fields)"
exit 0
