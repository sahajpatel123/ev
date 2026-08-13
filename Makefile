.PHONY: install dev test e2e-cli eval lint typecheck doctor verify compose-up compose-down migrate seed postgres-e2e

# Backend commands run from backend/ where pydantic looks for ./.env. Load the
# repo-root .env into the environment first so EV_VAULT_KEY and friends are
# visible to host-side commands (make migrate/seed/eval/e2e-cli).
define backend-run
	cd backend && set -a && { [ -f ../.env ] && . ../.env; } ; set +a; $(1)
endef

install:
	cd backend && uv sync --extra s3 --extra dev

dev:
	$(call backend-run, uv run uvicorn app.main:app --reload --port 8000)

test:
	cd backend && uv run pytest -q

e2e-cli:
	$(call backend-run, uv run python -m app.scripts.e2e_cli)

eval:
	cd backend && uv run python -m app.scripts.eval_gates --report eval/last-run.json

lint:
	cd backend && uv run ruff check app clients tests

typecheck:
	cd backend && uv run mypy app clients

doctor:
	$(call backend-run, uv run python -m app.scripts.doctor)

verify:
	$(MAKE) lint typecheck test eval

compose-up:
	docker compose up -d --build

compose-down:
	docker compose down

migrate:
	$(call backend-run, EV_DATABASE_URL="$${EV_DATABASE_URL:-postgresql+psycopg://ev:ev@localhost:5432/ev}" uv run alembic upgrade head)

update-contract:
	$(call backend-run, uv run python -m app.scripts.update_contract)

seed:
	$(call backend-run, EV_DATABASE_URL="$${EV_DATABASE_URL:-postgresql+psycopg://ev:ev@localhost:5432/ev}" uv run python -m app.scripts.seed)

postgres-e2e:
	docker compose up -d --build
	@if [ "$${E2E_RESET_DB:-0}" = "1" ]; then echo "Resetting local dev Postgres schema (E2E_RESET_DB=1)"; docker compose exec -T db psql -U ev -d ev -c "DROP SCHEMA public CASCADE; CREATE SCHEMA public;" >/dev/null; fi
	$(call backend-run, EV_DATABASE_URL="$${EV_DATABASE_URL:-postgresql+psycopg://ev:ev@localhost:5432/ev}" uv run alembic upgrade head)
	$(call backend-run, EV_DATABASE_URL="$${EV_DATABASE_URL:-postgresql+psycopg://ev:ev@localhost:5432/ev}" uv run python -m app.scripts.seed)
	export EV_E2E_BASE_URL=http://127.0.0.1:8000; export EV_E2E_MASTER_KEY="$${EV_MASTER_KEY:-e2e-key}"; $(call backend-run, uv run python -m app.scripts.e2e_cli)

compliance-sweep:
	$(call backend-run, uv run python -m app.scripts.compliance_sweep)

# --- AGENT 2 FOUNDRY ---
.PHONY: ml-install mlx-install face-install ml-doctor ml-list ml-stats datasets-list datasets-prune

ml-install:
	cd backend && uv sync --extra ml --extra dev

mlx-install:
	cd backend && uv sync --extra mlx --extra dev

face-install:
	cd backend && uv sync --extra face --extra dev

ml-doctor:
	cd backend && uv run python -m app.ml.cli doctor

ml-list:
	cd backend && uv run python -m app.ml.cli list

ml-stats:
	cd backend && uv run python -m app.ml.cli stats

datasets-list:
	cd backend && uv run python -m app.datasets.cli list

datasets-prune:
	cd backend && uv run python -m app.datasets.cli prune

# --- AGENT 14 PULSE (append-only) -------------------------------------------
.PHONY: launchd-install launchd-uninstall notify-test notify-status

launchd-install:
	./launchd/install.sh

launchd-uninstall:
	./launchd/uninstall.sh

notify-test:
	mkdir -p backend/storage/notify/EVNotificationHelper.app/Contents/MacOS && cd backend && swiftc -O -framework Foundation -framework UserNotifications -o storage/notify/EVNotificationHelper.app/Contents/MacOS/EVNotificationHelper app/notify/macos/EVNotificationHelper.swift && storage/notify/EVNotificationHelper.app/Contents/MacOS/EVNotificationHelper --check-permission

notify-status:
	@curl -s -H "Authorization: Bearer $${EV_MASTER_KEY:-test-key}" \
		http://127.0.0.1:8000/v1/runtime/notify/status

soak-audit:
	cd backend && uv run python -m app.workers.runtime_healthcheck --soak

# === Agent 8 Synapse (retrieval) — appended marker block ===
.PHONY: ev-eval-retrieval ev-eval-reembed

ev-eval-retrieval:
	cd backend && uv run python -m eval.retrieval.cli retrieval

ev-eval-reembed:
	cd backend && uv run python -m eval.retrieval.cli reembed

# --- AGENT 20 LAUNCH (append-only block) ------------------------------------
.PHONY: native-up native-down native-status native-e2e prune prune-dry-run preflight eval-ml

native-up:
	./brew/setup.sh

native-down:
	./launchd/uninstall.sh
	@brew services stop postgresql@17 2>/dev/null || true
	@brew services stop redis 2>/dev/null || true
	@launchctl bootout "gui/$$UID/ev.backup" 2>/dev/null || true
	@rm -f "$$HOME/Library/LaunchAgents/ev.backup.plist"

native-status:
	@echo "== brew services =="
	@brew services list | grep -E 'postgresql@17|redis' || true
	@echo "== launchd EV services =="
	@launchctl list | grep 'ev\.' || echo "no EV launchd services loaded"
	@echo "== API health =="
	@curl -s -m 3 -H "Authorization: Bearer $${EV_MASTER_KEY:-test-key}" \
		http://127.0.0.1:8000/v1/health || echo "API not reachable"

native-e2e:
	$(call backend-run, EV_E2E_BASE_URL="$${EV_E2E_BASE_URL:-http://127.0.0.1:8000}" EV_E2E_MASTER_KEY="$${EV_MASTER_KEY:-test-key}" EV_E2E_EXPECT_QUEUE=1 EV_E2E_EXPECT_STACK_WORKERS=1 EV_E2E_EXPECT_REAL_VOICE="$${EV_E2E_EXPECT_REAL_VOICE:-0}" uv run python -m app.scripts.e2e_cli)

preflight:
	$(call backend-run, uv run python -m app.scripts.preflight)

eval-ml:
	$(call backend-run, uv run ev-eval all; uv run python -m app.scripts.eval_gates --report eval/last-run.json)

prune:
	$(call backend-run, uv run python -m app.scripts.prune)

prune-dry-run:
	$(call backend-run, uv run python -m app.scripts.prune --dry-run)

# --- AGENT OPENCODE (append-only) -------------------------------------------
# `opencode serve` as EV's chat provider. Kept as separate targets because
# native-up/native-down and launchd/install.sh belong to Agents 20/14; see the
# dependency note in docs/OPENCODE.md to fold ev.opencode into their lists.
.PHONY: opencode-up opencode-down opencode-status opencode-agent-cost

opencode-up:
	@mkdir -p "$$HOME/Library/Logs/ev"
	@plutil -lint launchd/ev.opencode.plist >/dev/null
	@cp launchd/ev.opencode.plist "$$HOME/Library/LaunchAgents/"
	@launchctl bootout "gui/$$UID/ev.opencode" 2>/dev/null || true
	@sleep 1
	@launchctl bootstrap "gui/$$UID" "$$HOME/Library/LaunchAgents/ev.opencode.plist"
	@launchctl enable "gui/$$UID/ev.opencode"
	@echo "[ev] ev.opencode loaded; logs: $$HOME/Library/Logs/ev/opencode.*.log"

opencode-down:
	@launchctl bootout "gui/$$UID/ev.opencode" 2>/dev/null || true
	@rm -f "$$HOME/Library/LaunchAgents/ev.opencode.plist"
	@echo "[ev] ev.opencode removed"

opencode-status:
	@launchctl list | grep 'ev\.opencode' || echo "ev.opencode not loaded"
	@curl -s -m 3 http://localhost:4096/global/health || echo "opencode server not reachable"
	@echo
	@curl -s -m 10 http://localhost:4096/session | head -c 400 || true
	@echo

opencode-agent-cost:
	$(call backend-run, uv run python -m app.scripts.opencode_agent_cost)

# --- HANDS-FREE VOICE (append-only) -----------------------------------------
.PHONY: voice-models hands-free hands-free-up hands-free-down

voice-models:
	cd backend && uv sync --extra voice --extra dev && uv run python -m app.voice.models_setup

hands-free:
	$(call backend-run, uv run python -m clients.hands_free --api-url "$${EV_API_URL:-http://127.0.0.1:8000}" --api-key "$${EV_API_KEY:-$${EV_MASTER_KEY}}")

hands-free-up:
	@mkdir -p "$$HOME/Library/Logs/ev"
	@plutil -lint launchd/ev.hands_free.plist >/dev/null
	@cp launchd/ev.hands_free.plist "$$HOME/Library/LaunchAgents/"
	@launchctl bootout "gui/$$UID/ev.hands_free" 2>/dev/null || true
	@sleep 1
	@launchctl bootstrap "gui/$$UID" "$$HOME/Library/LaunchAgents/ev.hands_free.plist"
	@launchctl enable "gui/$$UID/ev.hands_free"
	@echo "[ev] ev.hands_free loaded; logs: $$HOME/Library/Logs/ev/hands_free.*.log"

hands-free-down:
	@launchctl bootout "gui/$$UID/ev.hands_free" 2>/dev/null || true
	@rm -f "$$HOME/Library/LaunchAgents/ev.hands_free.plist"
	@echo "[ev] ev.hands_free removed"
