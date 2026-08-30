# EV — Personal AI Companion

EV is a local-first, self-hosted personal AI companion: one lifelong memory,
one continuous conversation, and one owner identity across every device. It is
inspired by E.V. from *Spider-Man* — a persistent intelligence layer that
remembers everything you tell it, explains how it knows what it knows, and is
swappable at the model layer without changing who EV is.

The system is built around an immutable event store (PostgreSQL + pgvector),
derived and versioned memories, an intelligence filter between you and the
model, permissioned voice/vision/live-data perception, and a client surface
spanning CLI, web, and an iOS/watchOS Swift package.

## Quickstart

### 1. Prerequisites

- Python 3.12 and [uv](https://docs.astral.sh/uv/) for the backend.
- Docker Desktop or OrbStack for the full stack (Postgres, Redis, MinIO, API,
  workers). For a zero-infrastructure dev loop you can run the API directly
  against SQLite with offline providers.

### 2. Zero-setup development (no Docker)

The backend defaults are intentionally offline: SQLite, `echo` chat provider,
`hash` embeddings, and sync processing. Two keys are always required:
`EV_MASTER_KEY` (auth) and `EV_VAULT_KEY` (integration credential encryption —
the server refuses to derive it from the master key).

```sh
cd backend
uv sync --extra s3 --extra dev
export EV_MASTER_KEY=local-dev-key
export EV_VAULT_KEY=$(openssl rand -base64 48)
uv run uvicorn app.main:app --reload --port 8000
```

In another shell:

```sh
cd backend
export EV_API_URL=http://127.0.0.1:8000
export EV_API_KEY=local-dev-key
uv run ev onboarding "I decided to move the project to SQLite for local testing"
uv run ev ask "What did I decide about the project?"
```

The web workbench is served at <http://localhost:8000/app> (same-origin API;
store the master key in the connection panel). Tests and the eval suite set
their own dev vault keys, so `make test` / `make eval` work without this step.

### Hands-free "EVIE"

Say **EVIE** (or "hey EVIE") with the mic open. The server spots the name,
listens to the command, answers, then keeps the mic open for a follow-up —
no button.

```sh
cd backend
uv sync --extra voice --extra mic --extra dev
uv run python -m app.voice.models_setup
# API already running on :8000
uv run python -m clients.hands_free --api-key "$EV_MASTER_KEY"
```

Or open `/app` and use the Hands-free panel. On a Mac, the menu-bar app has a
**Hands-free — say “EVIE”** toggle; permissions are documented in
[macos/README.md](macos/README.md). Status:
`GET /v1/voice/live/status`. Details: [docs/VOICE.md](docs/VOICE.md) §13.

### 3. Full stack (Docker Compose)

```sh
cp .env.example .env
```

Edit `.env` minimally:

- `EV_MASTER_KEY` — long random value; every API call is authenticated with it
  or with a device token issued from it.
- `EV_VAULT_KEY` — dedicated random value (min 16 chars; the example ships a
  placeholder). It encrypts integration OAuth tokens/webhook secrets and is
  never derived from the master key. Rotating it later requires
  `POST /v1/integrations/vault/rotate`.
- `EV_DEEPSEEK_API_KEY` / `EV_EMBEDDING_API_KEY` — optional; the stack runs
  fully offline in echo/hash mode.
- `EV_BACKUP_PASSPHRASE` — optional, but set it before relying on backups.

Then:

```sh
make install      # backend deps (uv sync)
make compose-up   # db, redis, minio, api, worker, scheduler, runtime
make migrate      # Alembic -> full schema
make seed         # optional demo corpus
curl http://localhost:8000/v1/health
```

Compose overrides the dev defaults for a production-like topology: Postgres
instead of SQLite, `EV_PROCESSING_MODE=queue` with an RQ worker, and
`EV_OBJECT_STORE_BACKEND=s3` against MinIO. See
[docs/DEPLOYMENT.md](docs/DEPLOYMENT.md) for the full deployment story and
[docs/ENVIRONMENT.md](docs/ENVIRONMENT.md) for every configuration key.

### 4. CLI reference

`ev` is installed by `uv sync` (or run `uv run python -m clients.cli`):

```text
ev capture "remember this" | ev capture -     # text or stdin -> immutable event
ev attach note.pdf                             # file/share -> attachment event
ev ask "why did I choose SQLite?"
ev timeline --limit 20                         # recent events
ev memories --type decision --search sqlite
ev audit <memory_id>                           # "why does EV know this?"
ev correct <memory_id> "new text" --reason ...
ev forget <memory_id> | ev restore <memory_id>
ev card                                        # ev.hud.card.v1 text render
ev quickcard "Renegotiation with X"            # cached <800 ms briefing
ev doctor | ev checkup                         # health / full diagnostics
ev export --output ev-bundle.json
ev import ev-bundle.json --mode merge          # event-sourced restore/merge
ev onboarding "first memory" ...               # guided first memories + audit
ev queue | ev sync                             # offline captures -> server
ev identity status | owner | passkey | recovery | verify
```

Offline captures queue with idempotency keys and replay on `ev sync`
(201 synced / 409 duplicate / 422 quarantined). See
[docs/CLIENTS.md](docs/CLIENTS.md) for the full client and sync protocol.

## Architecture map

```text
                          ┌─────────────────────────── Clients ───────────────────────────┐
                          │ iOS/Watch Swift package · Web workbench (/app) · CLI `ev`     │
                          │ macOS collector agent · device listener · offline queues     │
                          └────────────────────────────┬──────────────────────────────────┘
                                                       │ HTTPS + SSE · Bearer tokens
                          ┌────────────────────────────▼──────────────────────────────────┐
                          │ API — FastAPI :8000                                          │
                          │ /v1/events /v1/chat /v1/memories /v1/recall/week             │
                          │ /v1/identity /v1/runtime /v1/filter /v1/training             │
                          │ /v1/compliance /v1/voice /v1/hud /v1/edith /v1/routines ...  │
                          └───┬───────────────┬───────────────┬──────────────┬────────────┘
                              │               │               │              │
                  ┌───────────▼────┐   ┌───────▼──────┐   ┌───▼───────┐   ┌───▼──────────────┐
                  │ PostgreSQL     │   │ Redis        │   │ MinIO/S3  │   │ Background       │
                  │ + pgvector     │   │ RQ queues ·  │   │ blobs/    │   │ worker (ingest)  │
                  │ source of truth│   │ cache        │   │ backups   │   │ scheduler (rout.)│
                  └────────────────┘   └──────────────┘   └───────────┘   │ runtime daemon   │
                                                                          └──────────────────┘

   In-process pipeline (API worker or RQ):

   capture ─► processor ─► memory engine ─► orchestrator ─► AI gateway ─► reply
               extract      typed, versioned    retrieve +     provider      filtered,
               embed        memories,           assemble       registry      grounded
               dedup        provenance,         context        (DeepSeek,    response
               version      conflicts,          tool loop      local, echo)  + SSE
                            audit trail

   Every step is wrapped by the intelligence filter (input/output), the
   compliance & privacy boundary, and the filter ledger.
```

### Services

| Service | Image / command | Role |
| --- | --- | --- |
| `db` | `pgvector/pgvector:pg17` | Events, memories, entities, audit, identity, voiceprints, training, routines — single source of truth |
| `redis` | `redis:7-alpine` | RQ queues, runtime/cache state |
| `minio` | `minio/minio:latest` | S3-compatible object store (attachments, blobs) |
| `api` | Dockerfile → `uvicorn app.main:app` | FastAPI surface, SSE streaming, web workbench |
| `worker` | `python -m app.workers.runner` | RQ ingestion worker (`process_event`) |
| `scheduler` | `python -m app.workers.scheduler` | Routines + trigger automations on a tick loop |
| `runtime` | `python -m app.workers.runtime_daemon` | Expires runtime sessions, retries dead letters, quiet-hours digest, health, scheduled compliance sweep (healthcheck verifies recent daemon ticks) |

### Repository layout

```text
ev/
  README.md              # this file
  Makefile               # install / dev / test / lint / typecheck / eval / compose / migrate / seed
  compose.yaml           # db + redis + minio + api + worker + scheduler + runtime
  AGENTS.md              # start here if you are an agent: rules, baseline, gotchas
  .env.example           # 204 quickstart configuration entries
  docs/                  # plan suite (PLAN, ARCHITECTURE, API, ENVIRONMENT, DEPLOYMENT, ...)
  backend/
    app/
      api/               # HTTP routers (core, ev, edith, runtime, identity, voice, filter, ...)
      memory/            # extraction, retrieval, versioned writer, patterns
      context/           # ContextCompiler (per-request window planning + budget monitor)
      filter/            # input/output filters, critic, ledger, policy
      gateway/           # provider registry (DeepSeek, local, echo/mock) + model-call audit
      services/          # processor, recall, importer, backup, live stream/rebuild/retention, ...
      ev/                # intelligence modules (EV Sense, HUD, tactical, research, gear, people, ...)
      voice/             # wake, speaker verification, anti-spoof, ASR, TTS, lifecycle
      identity/          # owner ceremony, passkeys, recovery, re-verification
      compliance/        # regional policy, consent, erasure, transparency, anomaly detection
      training/          # corpus, personalization, adapter, filter self-improvement
      routines/          # scheduled + trigger automations
      integrations/      # adapter framework, vault, webhooks, plugins
      tools/             # sandboxed code/file executor
      workers/           # runner, scheduler, runtime_daemon, RQ jobs
      config.py          # pydantic-settings, EV_* env surface
      models.py          # SQLAlchemy ORM
      schemas.py         # Pydantic API schemas
    clients/
      cli/               # `ev` command-line client (capture, ask, audit, identity, queue/sync, ...)
      web/               # self-contained web workbench served at /app
      hands_free/        # always-on "EVIE" listener (mic → /v1/voice/live → speaker)
      ears/              # on-device VAD + wake process (posts /v1/ears/wake)
      collectors/        # macOS perception agent (screen, audio scene, location)
      device_listener.py # always-on device listener / fleet ear
    tests/               # 99 test modules; CI runs lint + mypy + pytest + eval gates
    alembic/             # forward-only migrations
  ios/
    EVClient/            # shared Swift package (API client, HUD, offline queue, UI)
    README.md            # iOS/watchOS client notes
```

## Development

```sh
make test         # pytest (backend/tests)
make lint         # ruff check
make typecheck    # mypy
make eval         # eval gates: contract, retrieval, filter, voice, latency, restore drill, roadmap
make update-contract  # re-lock the API contract manifest after deliberate changes
make voice-models     # download Vosk + Piper for hands-free "EVIE"
make hands-free       # always-on listener (needs API + models)
```

CI is configured to run lint, typecheck, the full test suite, and the eval gates
on every push to `main` and on pull requests, plus a nightly eval report
artifact. **Those jobs are not currently executing:** every GitHub Actions run
since 2026-08-09 has been blocked before its first step by an account
billing/spending-limit block, so all verification to date is local. See
[docs/WORKSPACE_ANALYSIS.md](docs/WORKSPACE_ANALYSIS.md) §5 for the measured
state, including the three reasons the suite would still be red on the
`ubuntu-latest` runner once billing is restored.

## Documentation map

| Document | Contents |
| --- | --- |
| [AGENTS.md](AGENTS.md) | **Start here if you are an agent:** binding rules, measured baseline, known-broken state |
| [docs/WORKSPACE_ANALYSIS.md](docs/WORKSPACE_ANALYSIS.md) | Measured workspace analysis, subsystem map, documentation drift ledger |
| [docs/PLAN.md](docs/PLAN.md) | Vision, product principles, data model, roadmap, open decisions |
| [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) | Components, tech stack, data model, retrieval, orchestrator, API examples |
| [docs/API.md](docs/API.md) | v1 API contract and endpoint map |
| [docs/ENVIRONMENT.md](docs/ENVIRONMENT.md) | Complete `EV_*` configuration reference (server + clients) |
| [docs/DEPLOYMENT.md](docs/DEPLOYMENT.md) | Self-hosting, TLS, backups, upgrades, failure modes |
| [docs/WORK_BREAKDOWN.md](docs/WORK_BREAKDOWN.md) | Factor-by-factor build status across every domain |
| [docs/NEXT_STEPS.md](docs/NEXT_STEPS.md) | Post-breadth strategy: dogfood, depth tracks, what not to build |
| [docs/FLEET_LAW.md](docs/FLEET_LAW.md) | **Binding law** for every agent: ownership, append-only files, bans |
| [docs/AGENT_FLEET.md](docs/AGENT_FLEET.md) | **Multi-agent SSOT:** 20 agents (1–20), ownership, merge, bans |
| [docs/AGENT_LAUNCH.md](docs/AGENT_LAUNCH.md) | Paste-ready launch messages (superseded 15-agent pack; roster is 1–20) |
| [docs/AGENT_BRIEFS.md](docs/AGENT_BRIEFS.md) | Historical work orders (A0–A9); read for intent, not ownership |
| [docs/SECURITY.md](docs/SECURITY.md) | Auth, privacy boundary, encryption, ethics guardrails |
| [docs/CLIENTS.md](docs/CLIENTS.md) | Client architecture, CLI reference, sync protocol |
| [docs/BEHAVIOR.md](docs/BEHAVIOR.md) | Interaction intelligence, persona, assertiveness, permissions |
| [docs/IDENTITY_TRUST.md](docs/IDENTITY_TRUST.md) | Owner identity, trust levels, recovery |
| [docs/ROADMAP.md](docs/ROADMAP.md) | Milestones M0–M5, sequencing, gates |
| [docs/QA.md](docs/QA.md) · [docs/OPS.md](docs/OPS.md) | Testing strategy · operations runbook |

## Status

The backend, CLI, web workbench, and iOS Swift foundation are implemented and
covered by tests and eval gates. All 19 product domains are factor-complete
(see [docs/WORK_BREAKDOWN.md](docs/WORK_BREAKDOWN.md)). Historical milestones
live in [docs/ROADMAP.md](docs/ROADMAP.md). **What to do next** after breadth —
stabilize, dogfood, then deepen voice / phone / calendar — is in
[docs/NEXT_STEPS.md](docs/NEXT_STEPS.md).

**Multi-agent depth work** uses **exactly 20 agents (numbered 1–20)** with exclusive path
ownership for a personal shippable EVIE/EDITH stack (presence, perception,
notifications, real-dataset training, EDITH software modules, gateway/tools) —
not Domain 20 and not 19 parallel equal agents. Roster and ownership:
[docs/AGENT_FLEET.md](docs/AGENT_FLEET.md) §1–2. Binding rules:
[docs/FLEET_LAW.md](docs/FLEET_LAW.md).

The paste-ready messages in [docs/AGENT_LAUNCH.md](docs/AGENT_LAUNCH.md) are
still the superseded 15-agent pack, in which agents 3, 4, 14, and 15 name
*different* owners than the 1–20 roster. Until Agent 1 publishes the v3 pack,
treat `AGENT_FLEET.md` §2 as the only ownership table, and read
[AGENTS.md](AGENTS.md) §2 for the precedence rule.
