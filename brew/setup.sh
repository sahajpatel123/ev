#!/bin/zsh
# EV native stack bootstrap (LAUNCH, Agent 20).
#
# Installs and starts the daily-driver stack on an Apple Silicon Mac:
#   - PostgreSQL 17 + pgvector (via Homebrew)
#   - Redis (via Homebrew)
#   - the EV launchd services (api, worker, scheduler, runtime, ears, collector)
#   - the nightly encrypted backup launchd job
#
# The filesystem object store is the default (EV_OBJECT_STORE_BACKEND=local);
# MinIO is not part of the daily path. Compose remains for CI only.

set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
START_EPOCH="$(date +%s)"

note() {
  local elapsed=$(( $(date +%s) - START_EPOCH ))
  printf '[ev] +%ss %s\n' "$elapsed" "$*"
}

if ! command -v brew >/dev/null 2>&1; then
  echo "brew is required: https://brew.sh" >&2
  exit 1
fi

echo "[ev] installing/checking PostgreSQL 17"
if ! brew list --versions postgresql@17 >/dev/null 2>&1; then
  brew install postgresql@17
fi

echo "[ev] installing/checking pgvector"
if ! brew list --versions pgvector >/dev/null 2>&1; then
  # pgvector is a homebrew-core formula (0.8.6); the old pgvector/pgvector
  # tap is gone. The formula builds against the active PostgreSQL keg.
  brew install pgvector
fi

echo "[ev] installing/checking Redis"
if ! brew list --versions redis >/dev/null 2>&1; then
  brew install redis
fi

# Redis on this machine carried a stale Redis-Stack config block with
# loadmodule ./modules/*.so paths that do not exist, aborting startup
# ("Can't load module ... server aborting"). Comment those lines out and keep
# a timestamped backup; plain redis-server then boots normally.
REDIS_CONF="/opt/homebrew/etc/redis.conf"
if [[ -f "$REDIS_CONF" ]] && rg -q '^loadmodule ' "$REDIS_CONF" 2>/dev/null; then
  echo "[ev] disabling stale Redis-Stack loadmodule directives"
  cp "$REDIS_CONF" "${REDIS_CONF}.ev-bak-$(date +%Y%m%dT%H%M%S)"
  sed -i '' 's/^loadmodule /# loadmodule /' "$REDIS_CONF"
fi

note "starting brew services"
brew services start postgresql@17
if brew services list 2>/dev/null | grep -q '^redis[[:space:]]*started'; then
  # A stale failed bootstrap may linger; restart picks up the fixed config.
  brew services restart redis 2>/dev/null || true
else
  brew services start redis 2>/dev/null || true
fi

note "ensuring ev role + database"
PG_BIN="$(brew --prefix postgresql@17)/bin"
for attempt in {1..20}; do
  if "$PG_BIN/pg_isready" -h localhost -p 5432 -q; then
    break
  fi
  sleep 0.5
  if [[ "$attempt" == 20 ]]; then
    echo "postgres did not become ready" >&2
    exit 1
  fi
done

if ! "$PG_BIN/psql" -h localhost -d postgres -tAc "SELECT 1 FROM pg_roles WHERE rolname='ev'" | grep -q 1; then
  "$PG_BIN/psql" -h localhost -d postgres -c "CREATE ROLE ev LOGIN PASSWORD 'ev';"
fi
if ! "$PG_BIN/psql" -h localhost -d postgres -tAc "SELECT 1 FROM pg_database WHERE datname='ev'" | grep -q 1; then
  "$PG_BIN/createdb" -h localhost -O ev ev
fi
"$PG_BIN/psql" -h localhost -d ev -c "CREATE EXTENSION IF NOT EXISTS vector;" || {
  echo "pgvector extension creation failed (is the formula installed?)" >&2
  exit 1
}

note "wiring native env defaults into .env (additive only)"
ENV_FILE="$ROOT/.env"
if [[ -f "$ENV_FILE" ]]; then
  for line in \
    "EV_DATABASE_URL=postgresql+psycopg://ev:ev@localhost:5432/ev" \
    "EV_REDIS_URL=redis://localhost:6379/0" \
    "EV_PROCESSING_MODE=queue" \
    "EV_OBJECT_STORE_BACKEND=local"; do
    key="${line%%=*}"
    if ! grep -q "^${key}=" "$ENV_FILE"; then
      echo "$line" >> "$ENV_FILE"
      echo "[ev] appended $key to .env"
    fi
  done
  if ! grep -q '^EV_API_KEY=' "$ENV_FILE"; then
    master="$(grep '^EV_MASTER_KEY=' "$ENV_FILE" | head -1 | cut -d= -f2-)"
    if [[ -n "$master" ]]; then
      printf 'EV_API_KEY=%s\n' "$master" >> "$ENV_FILE"
      echo "[ev] appended EV_API_KEY (master key) for local ears/collector"
    fi
  fi
  mkdir -p "$HOME/.ev/backups"
else
  echo "no .env found at $ENV_FILE; copy .env.example and set keys first" >&2
  exit 1
fi

note "installing EV launchd services"
mkdir -p "$HOME/Library/LaunchAgents"
for name in api worker scheduler runtime ears collector opencode; do
  cp "$ROOT/launchd/ev.$name.plist" "$HOME/Library/LaunchAgents/"
done
# Parallel bootstrap: launchctl calls are slow under cold-start memory pressure;
# the sequential installer (launchd/install.sh) added ~60s. Results are the
# same topology; Agent 14's installer remains available for manual use.
for name in api worker scheduler runtime ears collector opencode; do
  label="ev.$name"
  plist="$HOME/Library/LaunchAgents/$label.plist"
  launchctl bootout "gui/$UID/$label" 2>/dev/null || true
  (
    for attempt in 1 2; do
      if launchctl bootstrap "gui/$UID" "$plist" 2>/dev/null; then
        break
      fi
      sleep 0.5
    done
    launchctl enable "gui/$UID/$label" 2>/dev/null || true
    launchctl kickstart -k "gui/$UID/$label" 2>/dev/null || true
  ) &
done
wait
echo "[ev] EV launchd services loaded"

note "installing nightly backup launchd job"
BACKUP_PLIST="$HOME/Library/LaunchAgents/ev.backup.plist"
if [[ -f "$BACKUP_PLIST" ]]; then
  launchctl bootout "gui/$UID/ev.backup" 2>/dev/null || true
fi
cp "$ROOT/brew/launchd/ev.backup.plist" "$BACKUP_PLIST"
plutil -lint "$BACKUP_PLIST" >/dev/null
launchctl bootstrap "gui/$UID" "$BACKUP_PLIST"
launchctl enable "gui/$UID/ev.backup"
launchctl kickstart -k "gui/$UID/ev.backup" 2>/dev/null || true
echo "[ev] ev.backup loaded (daily 02:30, logs: $HOME/Library/Logs/ev/)"

note "native stack setup complete"
echo "[ev] next: make migrate && make seed && make doctor"
