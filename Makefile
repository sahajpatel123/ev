.PHONY: install dev test e2e-cli eval lint typecheck doctor verify compose-up compose-down migrate seed postgres-e2e package-macos mac-control-live-e2e mac-control-live-e2e-full mac-control-dev-restart evie-cross-platform-dev evie-home-station evie-cross-platform-ready cross-platform-e2e mobile-voice-e2e mobile-voice-config-diff mobile-actions-e2e evie-shell-check iphone-parity-check

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

evie-cross-platform-dev:
	$(call backend-run, uv run python -m app.scripts.cross_platform_dev)

evie-home-station:
	$(call backend-run, uv run python -m app.scripts.home_station)

evie-cross-platform-ready:
	$(call backend-run, uv run python -m app.scripts.cross_platform_ready)

cross-platform-e2e:
	$(call backend-run, uv run python -m app.scripts.cross_platform_e2e)

mobile-voice-config-diff:
	$(call backend-run, uv run python -m app.scripts.mobile_voice_config_diff)

mobile-voice-e2e:
	cd backend && uv run pytest -q tests/test_mobile_voice_core.py tests/test_phone_audio_architecture.py tests/test_pwa_audio.py tests/test_webrtc_connection.py

mobile-actions-e2e:
	cd backend && uv run pytest -q tests/test_mobile_actions.py tests/test_mobile_shell.py tests/test_pure_pwa_no_native_shell.py tests/test_phone_audio_architecture.py tests/test_device_gateway.py

evie-shell-check:
	cd ios/EvieShell && swift run EvieBrokerCheck

pwa-release-manifest:
	cd backend && uv run python -m app.scripts.gen_release_manifest

ios-ci-check:
	bash -n scripts/ios/build-evie-ipa.sh && bash -n scripts/ios/verify-release.sh
	cd backend && uv run pytest -q tests/test_release_portal.py
	@echo "ios-ci-check OK (no Xcode needed)"

iphone-parity-check:
	node --check backend/clients/pwa/app.js
	node --check backend/clients/pwa/webrtc.js
	bash -n scripts/ios/build-evie-ipa.sh
	bash -n scripts/ios/verify-release.sh
	bash -n scripts/ios/physical-acceptance.sh
	bash -n scripts/ios/archive-if-possible.sh
	cd ios/EvieShell && swift run EvieBrokerCheck
	cd backend && uv run pytest -q tests/test_iphone_capability_plan.py tests/test_g2_trust_lifecycle.py tests/test_release_contract.py tests/test_device_gateway.py tests/test_pwa_audio.py tests/test_webrtc_connection.py tests/test_everywhere_g2.py tests/test_regression_golden.py tests/test_mobile_actions.py tests/test_mobile_shell.py
	@echo "iphone-parity-check OK (automated + broker; ship path is Tailscale PWA, not Xcode)"

# Full native build — requires macOS with Xcode.app (CI runner or dev Mac).
ios-canary:
	CHANNEL=canary ./scripts/ios/build-evie-ipa.sh

ios-release-verify:
	./scripts/ios/verify-release.sh --ipa $${IPA:-build/ios-release/canary/Evie.ipa} --expect-bundle-id com.ev.evie.shell

# Promote the owner-approved canary artifact to stable WITHOUT rebuilding.
ios-stable-promote:
	cd backend && uv run python -m app.scripts.promote_stable $${FROM_BUILD:-}

package-macos:
	macos/scripts/package.sh

mac-control-dev-restart:
	$(call backend-run, uv run python -m app.scripts.mac_control_live_e2e --restart-only --skip-package)

mac-control-live-e2e:
	$(call backend-run, uv run python -m app.scripts.mac_control_live_e2e --suite music)

mac-control-live-e2e-full:
	$(call backend-run, uv run python -m app.scripts.mac_control_live_e2e --suite full --timeout 120)

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

# --- AGENT 2 FOUNDRY · VOICE ACTIVATION (append-only) -----------------------
# NOTE: the existing `make preflight` (app.scripts.preflight) is Agent 20's;
# voice-preflight adds Foundry's deeper per-engine diagnostics without
# overriding it.
.PHONY: voice-deps model-pull-voice voice-preflight

voice-deps:
	cd backend && uv sync --extra ml --extra face --extra dev

model-pull-voice:
	$(call backend-run, uv run python -m app.ml.cli pull tts-kokoro-82m-int8 tts-kokoro-voices-v1.0)

voice-preflight:
	$(call backend-run, uv run python -m app.ml.voice_preflight)

# --- WAKE TRAINING (openwakeword custom head) --------------------------------
# The ears process works today via the local Whisper spotter
# (EV_EARS_WAKE_LOCAL_SPOTTER=true). Training the real always-on keyword head
# replaces it with a <300 ms on-device detector; requires the PyTorch stack.
.PHONY: wake-train-deps wake-train

wake-train-deps:
	cd backend && uv pip install torch torchinfo torchmetrics scipy tqdm pyyaml

# Train from the owner's recorded EVIE clips (voice-sample/wav) with other-voice
# EVIE takes (voice-tryouts/evie) as adversarial negatives. Export lands at
# ~/.ev/models/wake-openwakeword.onnx.
wake-train:
	$(call backend-run, uv run python -m clients.ears.train.train_head \
		--real-clips \
		--positive-dir "$$(pwd)/voice-sample/wav" \
		--negative-dir "$$(pwd)/voice-sample/voice-tryouts/evie" \
		--output-dir "$$HOME/.ev/models")

wake-train-dry-run:
	$(call backend-run, uv run python -m clients.ears.train.train_head \
		--real-clips --no-train \
		--positive-dir "$$(pwd)/voice-sample/wav" \
		--negative-dir "$$(pwd)/voice-sample/voice-tryouts/evie" \
		--output-dir "$$HOME/.ev/models")

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

seed-devices:
	cd backend && uv run python -m app.notify.registry --tokens

life-jobs:
	@curl -s -H "Authorization: Bearer $${EV_MASTER_KEY:-test-key}" \
		"http://127.0.0.1:8000/v1/runtime/life-jobs?limit=50"

boot-check:
	./launchd/check.sh

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
	@launchctl bootout "gui/$$UID/ev.opencode" 2>/dev/null || true
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
