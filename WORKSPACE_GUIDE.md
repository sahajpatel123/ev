# EV Workspace Guide — Read This First

**Purpose:** single orientation document for any human or AI agent working in
this repository. Read this before touching code. It eliminates repeated
re-discovery and prevents the most common confusions.

---

## 0. What this project is (30 seconds)

EV ("Evie") is a **local-first, self-hosted personal AI companion**: one
lifelong memory, one continuous conversation, one owner identity, across CLI,
web workbench, macOS menu-bar app, iOS/watchOS clients. Built on an immutable
event store (Postgres + pgvector), derived/versioned memories with provenance,
an intelligence filter, and a swappable model gateway.

- Persona name/voice = configuration (`EV_PERSONA_NAME`). Product brand = "EV".
- Owner runs everything on one Mac (8 GB) — offline-first, API-first inference
  (DeepSeek hosted; local models only for wake word, OCR, speaker verification).

## 1. Repository map (where things live)

```
backend/
  app/                  # FastAPI backend (~all server logic)
    api/                #   HTTP routers (core, ev, edith, voice, runtime, identity, ...)
    memory/             #   extraction, retrieval, versioned writer
    context/            #   ContextCompiler (token budgeting)
    filter/             #   intelligence filter: input/output filters, ledger, policy
    gateway/            #   provider registry (DeepSeek / local / echo)
    services/           #   processor, recall, importer, backup, live stream...
    ev/                 #   EDITH layer: HUD, tactical, radars, research, people...
    voice/              #   wake, ASR/TTS, speaker verification, live/ (frozen path!)
    identity/ compliance/ training/ routines/ integrations/ tools/
    workers/            #   RQ runner, scheduler, runtime daemon
    config.py models.py schemas.py main.py db.py contracts.py
  clients/
    cli/                # `ev` command-line client
    web/                # web workbench served at /app
    collectors/         # macOS perception agent
    device_listener.py
  tests/                # pytest suite (offline-safe)
  alembic/              # forward-only migrations
  eval/                 # eval gates + contract_v1.json (LOCKED, additive-only)
ios/                    # EVClient Swift package + EvieShell + Watch + Share ext
macos/                  # SwiftPM menu-bar app (Sources/EV), build/, Tests/
docs/                   # ~55 plan documents (see §6 for which ones matter)
scripts/                # gen_listener_assets.py, ios build/release scripts
launchd/                # launchd plists for always-on services (api, worker, ...)
brew/                   # brew/launchd backup plist only
helpers/evvision/       # Swift OCR helper
voice-sample/           # owner voice samples for enrollment (PRIVATE, local)
infra/                  # empty placeholder
compose.yaml Makefile .env.example
```

## 2. Canonical commands (Makefile is the entry point)

| Task | Command |
| --- | --- |
| Install deps | `make install` |
| Full stack up/down | `make compose-up` / `make compose-down` |
| Zero-Docker dev | `cd backend && uv run uvicorn app.main:app --reload --port 8000` |
| Migrate | `make migrate` |
| Tests | `make test` (offline-safe; never need API keys or weights) |
| Lint / types | `make lint` (ruff) / `make typecheck` (mypy) |
| Eval gates | `make eval` |
| Re-lock API contract | `make update-contract` (only after deliberate endpoint changes) |
| Package macOS app | `make package-macos` |

Two keys are always required: `EV_MASTER_KEY`, `EV_VAULT_KEY`. Backend defaults
are fully offline (SQLite + echo provider + hash embeddings).

## 3. Naming disambiguation (read before assuming anything)

| Name | Meaning |
| --- | --- |
| **EV** | The product/system. Repo name, env prefix (`EV_*`). |
| **Evie / EVIE** | The companion persona (owner-facing name). Configuration, not a separate system. Also the iOS/mobile release pipeline name (`docs/EVIE_MOBILE_RELEASE.md`). |
| **EDITH** | The *software layer* the owner experiences: HUD cards, tactical briefings, radars, research, gear telemetry (`docs/EDITH.md`, owned by Agent 15/ORACLE). No AR hardware. |
| **E.V. (Spider-Man)** | Design inspiration only. |
| **EV LIVE / live voice** | Full-duplex conversational voice runtime (`WS /v1/voice/live`, `backend/app/voice/live/`). |

Do **not** treat Evie/EDITH/EV as three systems. One system, three names for
different layers.

## 4. Hard rules (violating these breaks the project)

1. **FLEET LAW governs all agent work** — `docs/FLEET_LAW.md` is binding.
   Highlights:
   - Shared worktree `/Users/sahajpatel/Code/ev`. Never commit/push/rebase/stash
     unless the human explicitly orders it.
   - **Exclusive path ownership** per agent (`AGENT_FLEET.md` §2). Editing
     outside your OWNS list invalidates your work. Need something outside it →
     write a DEPENDENCY NOTE, don't edit.
   - Shared files (`Makefile`, `.env.example`, `compose.yaml`,
     `app/{config,models,schemas}.py`, `app/api/{core,ev,edith,companion,tools}.py`)
     are **append-only**, inside `# --- AGENT <N> <CODENAME> ---` blocks.
     Additive only — never change existing signatures/columns/defaults.
   - Dependencies are Agent 2's job only (`pyproject.toml`, `uv.lock`).
   - Alembic migrations: set `down_revision` to head at start; never edit
     another migration; SQLite upgrades must stay clean (no pgvector).
   - **API contract is additive-only** (`backend/eval/contract_v1.json`,
     locked). Never hand-edit; regenerate via `make update-contract`.
   - **Offline CI is sacred:** every real engine degrades to a deterministic
     double when weights are absent (`degraded=true`); weight-dependent tests
     use `skipif`; never reimplement production logic inside tests.
   - **No lying in code:** no fabricated confidence values, no echoing caller
     input as results, no trusting client-supplied security claims. Honest gap
     > disguised stub.
2. **LIVE VOICE PATH IS FROZEN** (`.cursor/rules/live-voice-freeze.mdc`, tag
   `live-working-2026-08-19`). Do NOT rewrite/retune/simplify:
   - `backend/app/voice/live/grok_voice.py`, `session.py`, `transport.py`
   - `macos/Sources/EV/TTSPlayer.swift`, `LiveConversation.swift`
   - `ios/EVClient/Sources/EVClient/LiveVoice.swift`
   Unless the owner explicitly asks. If it regresses, compare fingerprints
   (`/v1/health` → `runtime.realtime_bridge_source_fingerprint`) against the
   running API and a fresh `macos/build/EV.app`. Don't layer fixes onto a
   stale process.
3. **Tests must never touch production data.** `backend/tests/conftest.py`
   refuses live-DB tests against `EV_ENV=production` (see §7). pytest drops
   tables — inheriting the owner's real `DATABASE_URL` would wipe Postgres.
4. **Privacy invariants:** tombstone deletion + redaction cascade; consent-gated
   everything; remote processing requires `remote_processing_allowed()` gate
   (FLEET_LAW §13); privacy levels `private|normal|sensitive|never_send_to_model`.

## 5. Environment & config

- `.env.example` — canonical reference (documented key-by-key in
  `docs/ENVIRONMENT.md`). Copy → `.env` for compose.
- `.env` — actual local secrets. **Never commit** (already gitignored).
- ⚠️ `.env.api-first` — appears to be an older snapshot (Aug 14); do not treat
  as current. Ask the owner before resurrecting values from it.
- Backend config surface: `backend/app/config.py` (pydantic-settings, `EV_*`).
- Client env: `EV_API_URL`, `EV_API_KEY` (= master key or device token).

## 6. Documentation map — which doc answers which question

| Question | Doc |
| --- | --- |
| What should I build next? | `docs/NEXT_STEPS.md`, `docs/WORK_BREAKDOWN.md` |
| How does X component work? | `docs/ARCHITECTURE.md`, domain doc (`MEMORY.md`, `RETRIEVAL.md`, `GATEWAY.md`, `AUDIO.md`, `LIVE_VOICE.md`, ...) |
| What does the API expose? | `docs/API.md` + locked manifest `backend/eval/contract_v1.json` |
| What does an env var do? | `docs/ENVIRONMENT.md` |
| Who owns which paths? | `docs/AGENT_FLEET.md` §2 (SSOT) |
| What are the rules? | `docs/FLEET_LAW.md` |
| Past decisions & rationale | `docs/DECISIONS.md` (D-xx open, DC-xx decided) |
| What does a term mean? | `docs/GLOSSARY.md` |
| How do I deploy/operate? | `docs/DEPLOYMENT.md`, `docs/OPS.md` |
| iOS release process | `docs/EVIE_MOBILE_RELEASE.md`, `scripts/ios/*` |

## 7. Known inconsistencies & gotchas (found during workspace audit)

These are **open questions for the owner** — do not silently "fix" them:

1. **Agent count conflict:** `README.md` and `docs/NEXT_STEPS.md` say
   "**exactly 15 agents (1–15)**" and point to `AGENT_FLEET.md` as SSOT —
   but `AGENT_FLEET.md` v3.0 says "**exactly 20 agents (1–20)**", while
   `AGENT_LAUNCH.md` still ships the 15-agent pack. Before any fleet work,
   ask the owner which version is authoritative.
2. **`docs/VOICE.md` does not exist** but is referenced by `README.md`'s
   architecture map and several docs (`AGENT_BRIEFS.md`, `BUILDABLE_*.md`,
   `LIVE_VOICE.md`). Voice docs currently live in `AUDIO.md` /
   `LIVE_VOICE.md` / `MAC_VOICE_BASELINE.md`.
3. **Uncommitted change:** `backend/tests/conftest.py` carries an uncommitted
   production-test guard (P0 closure guard). Leave it reviewable; commit only
   when the owner orders it.
4. **Duplicate tool-cache dirs** exist at both repo root and `backend/`
   (`.mypy_cache/`, `.pytest_cache/`, `.ruff_cache/`) — harmless artifacts,
   gitignored; don't confuse root-level caches with backend ones.
5. **`launchd/` vs `brew/launchd/`:** real service plists live in `launchd/`;
   `brew/launchd/` holds only a backup plist.
6. **`infra/` is empty** — infrastructure lives in `compose.yaml` +
   `launchd/`, not there.
7. **Alembic head moves.** Verify current head (`uv run alembic heads` in
   `backend/`) instead of trusting a doc-stamped head (docs have said
   `2f31c7d0a1b2` since 2026-08-11).
8. **Branches:** stray remote branches (`cursor/hands-free-wake-word-*`,
   `cursor/setup-dev-environment-*`, `cursor/workspace-analysis-*`) may be
   stale — confirm before merging/rebasing anything.

## 8. Session checklist (for any agent/human starting work here)

1. Read this file top-to-bottom.
2. Read `docs/FLEET_LAW.md` (binding) and your ownership row in
   `docs/AGENT_FLEET.md` §2 if doing agent work.
3. Check `git status` — leave a reviewable working tree; never commit unless
   ordered.
4. Run `make test && make lint && make typecheck` before claiming done.
5. Respect the frozen live-voice path (§4.2).
6. New endpoints → additive; new settings/models → append-only shared-file
   rules; new deps → DEP REQUEST, don't touch lockfiles.
7. Record non-obvious decisions in `docs/DECISIONS.md`.
