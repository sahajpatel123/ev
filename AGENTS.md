# AGENTS.md — orientation for every agent working in this repo

Read this file first, then [`docs/FLEET_LAW.md`](docs/FLEET_LAW.md), which is
binding. This file exists to remove the recurring confusions: which fleet plan
is authoritative, which numbers are real, and what "green" actually means on
this tree. Every number below was measured on the working tree, not copied from
prose. Re-measure any time with `make baseline`.

A full analysis — subsystem map, doc-drift ledger, and the open problems at
HEAD — lives in [`docs/WORKSPACE_ANALYSIS.md`](docs/WORKSPACE_ANALYSIS.md).

## 1. What this project is

EV is a local-first, self-hosted personal AI companion for **one owner**: an
immutable event store, derived and versioned memories with provenance, an
intelligence filter between the owner and the model, permissioned
voice/vision/live-data perception, and clients spanning CLI, web, macOS
menu-bar, and iOS/watchOS. Product scope is breadth-complete across 19 domains;
the current work is **depth** — replacing deterministic doubles with real
engines behind contracts that are already locked.

Single-owner design is a hard constraint, not a stage. Multi-user and guest
mode are banned, as is inventing a new product domain ("Domain 20").

## 2. Which plan is authoritative

The repo contains three fleet models written at different times. They assign
**the same agent numbers to different owners**, which is the single largest
source of confusion in this workspace. Precedence:

| Model | Where | Status |
| --- | --- | --- |
| **20 numbered agents, 1–20** | [`docs/AGENT_FLEET.md`](docs/AGENT_FLEET.md) v3.0 §1–2 + [`docs/FLEET_LAW.md`](docs/FLEET_LAW.md) | **Authoritative.** Newest, and the one the law is written against. |
| 15 numbered agents, 1–15 | [`docs/AGENT_LAUNCH.md`](docs/AGENT_LAUNCH.md) (v2 paste pack) | Superseded roster, still the only paste-ready launch text. Numbers 3, 4, 14, 15 mean different agents here. |
| A0–A9 | [`docs/AGENT_BRIEFS.md`](docs/AGENT_BRIEFS.md), [`docs/PLAN.md`](docs/PLAN.md) | Historical. Read for intent, never for ownership. |

**Authoritative fleet size: 20** numbered agents, 1–20. `AGENT_FLEET.md` §2 is
the only ownership table to trust. When any other doc disagrees about the roster,
`AGENT_FLEET.md` + `FLEET_LAW.md` win, and the disagreement belongs in the
drift ledger in `docs/WORKSPACE_ANALYSIS.md` rather than in a silent local fix.

Codename ↔ number, from `AGENT_FLEET.md` §1:

```text
1 CONDUCTOR   2 FOUNDRY     3 EARS       4 VOICE      5 SENTRY
6 EYES        7 ROSTER      8 SYNAPSE    9 MNEMO     10 CORTEX
11 FORGE     12 CONDUIT    13 AMBIENT   14 PULSE     15 ORACLE
16 CONSCIENCE 17 WORKBENCH 18 SUIT      19 VAULT     20 LAUNCH
```

## 3. Rules that get work rejected

These are the ones violated most often. `docs/FLEET_LAW.md` is the complete text.

- **Edit only your OWNS paths.** Anything outside invalidates the whole report.
  Need a neighbour's file? Stop and write a DEPENDENCY NOTE naming the agent
  number who owns it. `make baseline-check` verifies every OWNS path still
  resolves on disk.
- **Shared files are append-only.** `Makefile`, `.env.example`, `compose.yaml`,
  `docs/ENVIRONMENT.md`, `backend/app/{config,models,schemas}.py`, and
  `backend/app/api/{core,ev,edith,companion,tools}.py` are shared by design.
  Append inside a block marked `# --- AGENT <N> <CODENAME> ---`. Never reorder,
  reformat, or delete another agent's lines; never change an existing endpoint
  signature, table column, or setting default.
- **Dependencies are Agent 2's alone.** Do not touch `backend/pyproject.toml`
  or `backend/uv.lock`. File a DEP REQUEST with reason and wheel size, import
  lazily, and guard the import so the suite still passes before it lands.
- **The API contract is additive-only.** `backend/eval/contract_v1.json` locks
  272 paths / 295 operations. New endpoints are fine; changed or removed ones
  are not. Never hand-edit it — Agent 1 regenerates with `make update-contract`.
- **Offline runs must stay green.** No API keys, no downloaded weights. Every
  real engine degrades to a deterministic double that sets `degraded=true`;
  tests needing weights use `pytest.mark.skipif` and **skip, never fail**.
- **No lying in code.** No fabricated confidence values, no echoing the
  caller's input back as a result, no trusting client-supplied security claims,
  no silently applying trained weights or filter policy. An honest gap
  documented in your report is a pass; a disguised stub is a fail.
- **Migrations:** set `down_revision` to the Alembic head that existed when you
  started, never edit another agent's migration, and keep SQLite upgrades clean
  (`CREATE EXTENSION vector` is PostgreSQL-only). Agent 1 linearizes at merge.
- **Models register with the ModelArbiter** declaring name, license,
  source_url, sha256, and memory tiers. Never load a model outside the arbiter;
  never download without a checksum. Budget: `docs/MODEL_BUDGET.md`.
- **Reasoning runs through a hosted API** (DeepSeek). No local LLM on a
  required path. Small local models are permitted only where an API is
  impossible or clearly worse: wake word, OCR, speaker verification, face
  embedding. Every remote path passes `remote_processing_allowed()`.
- **Do not commit, push, rebase, force-push, stash, or revert** unless the
  human explicitly asks. Leave a reviewable working tree.
- **End every report with the mandatory footer** from `AGENT_FLEET.md` §8,
  including a non-empty "WHAT IS STILL NOT REAL" section.

## 4. Running the code

Two keys are always required. `EV_VAULT_KEY` is never derived from the master
key, and the server refuses to start without it:

```sh
cd backend
uv sync --extra s3 --extra dev
export EV_MASTER_KEY=local-dev-key
export EV_VAULT_KEY=$(openssl rand -base64 48)
uv run uvicorn app.main:app --reload --port 8000     # workbench at /app
```

Defaults are deliberately offline: SQLite, `echo` chat provider, `hash`
embeddings, sync processing. Docker Compose (`make compose-up`) swaps in
Postgres + pgvector, Redis/RQ queue mode, and MinIO.

Verification, from the repo root:

```sh
make lint         # ruff check app clients tests
make typecheck    # mypy app clients
make test         # pytest -q
make eval         # 18 eval gates -> eval/last-run.json
make verify       # all four
make baseline     # measure this workspace
make baseline-check   # fail on doc/tree drift and broken invariants
```

Optional extras gate whole test groups: `make ml-install` (onnxruntime, numpy),
`make face-install` (pillow, opencv), `make mlx-install` (Apple silicon only).
`make test` with only `s3,dev` installed **will fail** the vision-corpus tests —
see §6.

## 5. Measured baseline

Measured on the working tree by `tools/baseline.py`; recorded in
`tools/baseline.json`. Prose in `README.md`, `docs/NEXT_STEPS.md`, and
`docs/ENVIRONMENT.md` still carries older numbers — trust this table.

| Metric | Value |
| --- | --- |
| Python modules under `backend/app` | 237 (68,699 lines, 27 subpackages) |
| Python modules under `backend/clients` | 27 (5,960 lines) |
| Test modules / test functions | 99 / 1,035 |
| API routers / route decorators | 18 / 294 |
| Locked contract paths / operations | 272 / 295 |
| `Settings` fields (`EV_*`) | 239 |
| ORM tables | 67 |
| Alembic migrations | 7 |
| `docs/*.md` files | 50 |
| `EV_*` keys in `.env.example` / `.env.api-first` | 204 / 32 |
| Swift files / lines | 44 / 6,780 |
| Fleet size | 20 |

When a change legitimately moves these numbers, run `make baseline-write` and
update this table in the same commit. That is the whole anti-drift mechanism:
one measured table, one command, one place to fix.

## 6. What is actually broken at HEAD

Measured on this Linux checkout, `uv sync --extra s3 --extra dev` — the exact
extras CI installs. Details and one-line fixes are in
`docs/WORKSPACE_ANALYSIS.md` §5. Do not re-diagnose these from scratch:

| Signal | Measured | Note |
| --- | --- | --- |
| `ruff` | clean | |
| `mypy` | **1 error in 264 files** | `app/ops/metrics.py:125` — `total` may be `None` on the Linux `/proc/meminfo` branch. Docs claim 0. Owner: Agent 20. |
| `pytest` | **1,018 passed, 6 failed, 23 skipped** | 4 × `test_vision_corpus` hard-fail without pillow instead of skipping (breaks FLEET_LAW §7); `test_gear_alerts` asserts `system == "Darwin"`; 1 sandbox test relies on the macOS RSS watchdog. |
| `eval_gates` | **18/18 gates, 110/110 checks, 5 skipped, exit 0** | The 5 skips are absent ML artifacts (gitignored), which is the designed behaviour. |
| GitHub Actions | **never executed** | Every run since 2026-08-09 was blocked before starting: *"job was not started because recent account payments have failed or your spending limit needs to be increased."* |

The consequence worth internalising: **"CI is green" has never been true on
GitHub.** All verification to date is local, on the owner's macOS machine. The
suite is not currently portable to the `ubuntu-latest` runner the workflow
targets, so fixing billing alone would still produce a red build.

## 7. Codebase conventions

- **Layering.** `app/api/*` routers stay thin and delegate to `app/services/*`
  or a domain package (`memory/`, `voice/`, `vision/`, `filter/`, `ev/`, …).
  Business logic in a router is a review failure.
- **Provider registries.** Swappable backends follow one shape: a
  `dict[str, Callable[[], Provider]]` registry plus a `get_*_provider()` that
  reads `settings.*_provider`. Chat, embeddings, ASR, TTS, wake, voiceprint,
  face, OCR, and search all use it. Swapping an engine is config, not code.
  Unknown provider names raise rather than silently falling back.
- **Degraded doubles.** When weights or deps are absent the provider returns a
  deterministic result with `degraded=True`. Eval gates treat `degraded: true`
  as SKIP, never PASS, so a double can never be mistaken for a quality number.
- **Lazy imports.** Heavy optional deps (onnxruntime, torch, speechbrain,
  pillow, opencv, mlx, faster-whisper) are imported inside factories and
  methods, never at module top, so a bare `s3,dev` install can import
  everything.
- **Config.** One `Settings` object in `app/config.py`, `env_prefix="EV_"`,
  snake_case field → `EV_UPPER_SNAKE` env var. Every feature is flag-gated.
- **Privacy boundary.** Events and memories carry a `privacy_level`;
  `app/security/boundary.py` strips secrets and `never_send_to_model` content
  before anything reaches a provider. Biometric enrolment requires a consent
  record.
- **Auth.** Bearer master key, or a device token issued from it. Ops,
  maintenance, and sensitive identity routes require the master key; some
  require fresh re-verification.
- **Tests** are hermetic: SQLite via aiosqlite, fresh schema per test through
  the autouse `fresh_db` fixture, offline provider doubles, `httpx.AsyncClient`
  over `ASGITransport`. `asyncio_mode = "auto"`, so no per-test asyncio marker.
  At least one test per subsystem must exercise the real factory entry point —
  never reimplement production logic inside a test.

## 8. Where to look

| Question | Doc |
| --- | --- |
| Who owns this file? Merge order? | `docs/AGENT_FLEET.md` §2, §6 |
| What are the binding rules? | `docs/FLEET_LAW.md` |
| Full workspace analysis, drift ledger, open problems | `docs/WORKSPACE_ANALYSIS.md` |
| Vision, data model, roadmap | `docs/PLAN.md`, `docs/ARCHITECTURE.md`, `docs/ROADMAP.md` |
| What to build next, and what not to | `docs/NEXT_STEPS.md` |
| Endpoint contract | `docs/API.md`, `backend/eval/contract_v1.json` |
| Every `EV_*` setting | `docs/ENVIRONMENT.md` |
| Per-domain build status | `docs/WORK_BREAKDOWN.md` |
| Test and gate strategy | `docs/QA.md`, `docs/EVALUATION.md` |
| Decisions already made (do not relitigate) | `docs/DECISIONS.md` |

Subsystem deep dives exist for memory, retrieval, voice, voice security,
vision, people, gateway, filter, training, integrations, live data, runtime,
routines, identity, security, clients, Apple clients, and opencode — one doc
per domain in `docs/`.

## 9. Before you report

1. Every file you touched is inside your OWNS list.
2. `make lint typecheck test` is no worse than the state in §6, and you can say
   exactly which of those pre-existing failures you saw.
3. Numbers in your report were measured, with the command quoted. If you moved
   a baseline number, `make baseline-write` ran and §5 was updated.
4. Anything real that is still missing is named in "WHAT IS STILL NOT REAL".
5. You did not commit, push, or rewrite history unless asked.
