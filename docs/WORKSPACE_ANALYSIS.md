# EV — Workspace Analysis

**Measured:** 2026-08-12 · **Tree:** `f640bba` (`feat(opencode): add opencode chat provider via local headless server`)
**Method:** every number in this document was produced by running a command on
this checkout. Nothing is quoted from prose. Re-measure with `make baseline`.
**Purpose:** one place that describes what this workspace actually contains, so
that ~20 parallel agents stop rediscovering the same facts and stop inheriting
stale ones. Operating rules for agents live in [`AGENTS.md`](../AGENTS.md);
this document is the evidence behind them.

---

## 1. Executive summary

EV is a large, unusually coherent single-owner AI companion: 237 Python modules
and ~68.7k lines under `backend/app`, 67 ORM tables, 294 route handlers behind a
locked 295-operation contract, 1,035 tests, 50 design docs, and Swift clients
for macOS, iOS, and watchOS. The architecture is genuinely layered — thin
routers, service objects, domain packages, and swappable provider registries —
and it has an unusual and valuable discipline baked in: when a real ML engine is
unavailable it returns a deterministic double flagged `degraded=true`, and the
eval gates treat that flag as SKIP rather than PASS. Doubles cannot masquerade
as quality numbers.

The problems are not in the architecture. They are in the **coordination layer**,
and they cluster into three findings:

1. **Three incompatible fleet plans coexist, and they assign the same agent
   numbers to different owners.** `AGENT_FLEET.md` v3.0 defines 20 agents;
   `AGENT_LAUNCH.md` and `README.md` say exactly 15; `AGENT_BRIEFS.md` and
   `PLAN.md` still describe A0–A9. Under the 20-agent roster Agent 3 is EARS
   (audio); under the 15-agent pack Agent 3 is Voice Reality and owns all of
   `voice/**`. Two agents told "you are Agent 3" from different docs will
   collide on exclusive paths — the exact failure the ownership table exists to
   prevent. See §4.1.
2. **CI has never run.** Every GitHub Actions run since 2026-08-09 was blocked
   before its first step by a billing/spending-limit block. "Offline CI is
   sacred" (FLEET_LAW §7) has therefore only ever been verified on the owner's
   Mac, and the suite is **not** currently portable to the `ubuntu-latest`
   runner the workflow targets. See §5.
3. **Documented baseline numbers have drifted far from the tree** — off by 2x to
   3x in places (`.env.example` is documented as 69 entries and contains 204;
   tests documented as 59 modules / 424 tests, actual 98 / 1,028). Agents that
   read prose for context are being handed a picture of a much smaller system.
   See §4.2.

Product status reads accurately: breadth is complete across 19 domains, and the
honest depth gaps are already written down in `docs/NEXT_STEPS.md` and the
unchecked done-when boxes of `AGENT_FLEET.md` §9. That part of the
documentation is trustworthy. It is the counts and the roster that are not.

---

## 2. What the system is

One owner, one lifelong memory, one continuous conversation, across every
device. Concretely:

- **Immutable event store** as the source of truth (PostgreSQL + pgvector in
  production, SQLite for dev and tests), with derived, versioned memories that
  carry provenance and can be audited, corrected, forgotten, and restored.
- **An intelligence filter** wrapping every model interaction on both the input
  and output side, with a grounding critic and an append-only ledger of filter
  decisions.
- **Permissioned perception**: voice (wake word, ASR, TTS, speaker
  verification, anti-spoof), vision (screen/camera capture, OCR, detection), a
  consented face roster, and ambient collectors — all behind consent records and
  a privacy boundary that strips secrets before anything reaches a provider.
- **A model gateway** with a provider registry, SSE streaming, an audited tool
  loop, a sandboxed executor, cost caps, and a circuit breaker.
- **Clients**: CLI (`ev`), a self-contained web workbench at `/app`, a macOS
  menu-bar app, an iOS/watchOS project, plus a device listener and collectors.

Deliberate constraints, all of them load-bearing: single owner (no multi-user or
guest mode, ever); breadth is frozen (no "Domain 20"); reasoning runs through a
hosted API because the owner's machine is an 8 GB M2, with small local models
permitted only where an API is impossible or clearly worse; and the offline
suite must pass with no API keys and no downloaded weights.

### Runtime topology

| Process | Command | Role |
| --- | --- | --- |
| `api` | `uvicorn app.main:app` | FastAPI surface, SSE streaming, serves the workbench |
| `worker` | `python -m app.workers.runner` | RQ ingestion (`process_event`) |
| `scheduler` | `python -m app.workers.scheduler` | Routines, trigger automations, live retention ticks |
| `runtime` | `python -m app.workers.runtime_daemon` | Session expiry, dead-letter retries, quiet-hours digest, compliance sweep |
| `db` / `redis` / `minio` | pgvector pg17 / redis:7 / minio | Truth, queues + cache, blobs |

Three deployment paths exist and are all real: Docker Compose (`make
compose-up`), a native Homebrew + launchd stack on macOS (`make native-up`, six
LaunchAgents), and a zero-infrastructure SQLite dev loop.

---

## 3. Subsystem inventory

27 packages under `backend/app`. Ownership column is from `AGENT_FLEET.md` §2.

| Package | Files / lines | Role | Owner |
| --- | --- | --- | --- |
| `api/` | 19 / 8,065 | 18 routers, thin, one `APIRouter` each | shared + various |
| `audio/` | 9 / 2,034 | Mic capture, ring buffer, VAD, scene, diarization | 3 EARS |
| `voice/` | 11 / 5,886 | ASR, TTS, wake, speaker, anti-spoof, lifecycle | 3/4/5 |
| `vision/` | 12 / 2,089 | OCR registry, detection, scene, face, capture, eval | 6 EYES |
| `people/` | 11 / 2,362 | Consented face enrolment, recognition, biodata | 7 ROSTER |
| `memory/` | 9 / 2,813 | Extraction, retrieval, versioned writer, entities, temporal | 8/9 |
| `context/` | 2 / 414 | Token-budget window planner | 9 MNEMO |
| `gateway/` | 9 / 2,673 | Provider registry, streaming, routing, cost, reliability | 10 CORTEX |
| `tools/` | 3 / 482 | Sandboxed command/file executor | 10 CORTEX |
| `search/` | 2 / 101 | Web research providers (none/mock/brave) | 10 CORTEX |
| `training/` | 11 / 3,961 | Corpus, personalization, LoRA, adapters, filter self-improvement | 11 FORGE |
| `integrations/` | 8 / 3,273 | Adapter framework, OAuth PKCE, vault, webhooks, plugins | 12 CONDUIT |
| `workers/` | 6 / 484 | Runner, scheduler, runtime daemon, healthcheck | 14 PULSE |
| `notify/` | 9 / 972 | Notification service + console/macOS/webhook/APNs backends | 14 PULSE |
| `routines/` | 4 / 1,277 | Scheduled and trigger automations | 14 PULSE |
| `ev/` | 29 / 8,001 | EDITH modules: HUD, tactical, radars, research, gear, rollups | 15/16 + others |
| `filter/` | 11 / 3,179 | Input/output filters, policy, critic, NLI critic, ledger | 16 CONSCIENCE |
| `identity/` | 3 / 1,557 | Owner ceremony, passkeys/WebAuthn, recovery | 19 VAULT |
| `compliance/` | 7 / 1,148 | Regional policy, consent, erasure, transparency, anomaly | 19 VAULT |
| `security/` | 3 / 280 | Privacy boundary, PII detection | 19 VAULT |
| `scripts/` | 12 / 3,876 | eval_gates, doctor, preflight, seed, prune, backup | 20 LAUNCH |
| `ops/` | 3 / 468 | Latency/cost budgets, metrics aggregation | 20 LAUNCH |
| `ml/` | 7 / 1,173 | Model registry, ModelArbiter, resident-memory tiers | 2 FOUNDRY |
| `datasets/` | 4 / 418 | License-pinned dataset registry, eval-only | 2 FOUNDRY |
| `services/` | 19 / 4,674 | Business logic: processor, recall, rebuild, backup, tool loop | 9/10/13/20 |
| `storage/` | 2 / 79 | Object store abstraction (local or s3) | shared |
| `utils/` | 2 / 38 | Token estimate, hashing, UTC | shared |

Cross-cutting top-level modules: `main.py` (router wiring, lifespan `init_db`),
`config.py` (239 settings), `models.py` (67 tables), `schemas.py` (~3.1k lines),
`db.py`, `auth.py`, `contracts.py`, `embeddings.py`, `rerank.py`.

### Client surfaces

| Surface | Location | Notes |
| --- | --- | --- |
| CLI `ev` | `backend/clients/cli/` (~2.6k lines) | Offline capture queue with idempotency keys, replays on `ev sync` (201/409/422) |
| Web workbench | `backend/clients/web/` | Static SPA served at `/app` under strict CSP |
| Collectors | `backend/clients/collectors/` | Screen/audio/location → derived events, fail-closed privacy |
| Device listener | `backend/clients/device_listener.py` | Heartbeats, wake arbitration, offline replay |
| Ears | `backend/clients/ears/` | Always-on mic: VAD → wake → scene |
| iOS/watchOS | `ios/` (27 Swift files) | `EVClient` SPM package + 4 Xcode targets; validated by `swift run EVClientCheck`, no XCTest target |
| macOS menu bar | `macos/` (13 Swift files) | SwiftPM `.app`, builds with CLT only, no Xcode |
| Vision helper | `helpers/evvision/` | Apple Vision OCR / ScreenCaptureKit binary |

---

## 4. Measured baseline and documentation drift

### 4.1 The roster collision

`AGENT_FLEET.md` v3.0 §0 states "**Exactly 20 agents** (numbered **1–20**)" and
`FLEET_LAW.md` line 1 is "EV FLEET LAW — binding on all 20 agents". Meanwhile:

- `README.md:226` — "**Multi-agent SSOT:** 15 agents (1–15), ownership, merge, bans"
- `README.md:245` — "uses **exactly 15 agents (numbered 1–15)**"
- `docs/NEXT_STEPS.md:10` — "**exactly 15 agents (numbered 1→15)**"
- `docs/NEXT_STEPS.md:348` — "**Multi-agent SSOT:** roster 1–15"
- `docs/AGENT_LAUNCH.md:8` — "Agent count: exactly 15." (header at :4 says roster 1–20)
- `docs/PLAN.md:36` and `docs/WORK_BREAKDOWN.md:12` — still "A0–A9"

The numbering is not merely a count mismatch. The same integer names a
different agent with a different exclusive-path list:

| # | 20-agent roster (`AGENT_FLEET.md`) | 15-agent pack (`AGENT_LAUNCH.md`) |
| --- | --- | --- |
| 3 | EARS — `app/audio/**`, `voice/wake.py`, `clients/ears/**` | Voice Reality — all of `app/voice/**` |
| 4 | VOICE — `voice/{asr,tts,pipeline,lifecycle}.py`, `api/voice.py` | Runtime & Always-On — `workers/**`, `device_listener.py` |
| 14 | PULSE — `workers/**`, `runtime.py`, `notify/**`, `routines/**` | Surface — `cli/**`, `web/**`, iOS |
| 15 | ORACLE — `app/ev/**` (EDITH modules) | Ops & Ship — eval gates, deployment docs |

Merge order is specified three different ways: ascending 1→20
(`AGENT_FLEET.md:137`), 1→15 (`AGENT_LAUNCH.md:115`), and the dependency-ordered
`A0 → A7 → A1 → A2 → A4/A5/A8 → A6 → A3 → A9` (`AGENT_BRIEFS.md:72`).

**Root cause of why this has not been fixed.** `backend/tests/test_agent_fleet_docs.py`
structurally locks the 15-agent model. Its docstring is "Structural lock for
fleet plan + elite 15-agent launch pack", it asserts `"exactly 15" in launch`,
and it parametrizes over `range(1, 16)` requiring a `## Agent N —` section for
each. Upgrading `AGENT_LAUNCH.md` to a 20-agent pack turns the suite red, so the
stale pack is pinned in place by a passing test. Any fix must land the test
change and the doc change together. Both are Agent 1 CONDUCTOR's paths.

`AGENTS.md` §2 now records the precedence (20 agents wins) so that an agent
reading only the root file gets the right answer immediately.

### 4.2 Stale counts

Measured with `python3 tools/baseline.py`:

| Metric | Docs claim | Measured | Drift |
| --- | --- | --- | --- |
| `.env.example` entries | 69 (`README.md:167`, `docs/ENVIRONMENT.md:4`) | **204** | 3.0x |
| Test modules | 59 (`README.md:194`, `WORK_BREAKDOWN.md:923`) | **99** | 1.7x |
| Tests | 424 (`NEXT_STEPS.md:24`) | **1,035** | 2.4x |
| `backend/app` modules | ~157 (`NEXT_STEPS.md:23`) | **237** | 1.5x |
| HTTP endpoints / routers | ~256 across 17 (`NEXT_STEPS.md:23`) | **294 across 18** | — |
| Contract operations | 261 (`FLEET_LAW.md:29`, `WORK_BREAKDOWN.md:936`) | **295** (272 paths) | — |
| Eval gates | 12 (`QA.md:9`, historical) | **18** | — |
| Fleet size | 15 (`README.md`, `NEXT_STEPS.md`) | **20** (roster rows) | — |

`AGENT_FLEET.md` §12 is the one baseline that is close to current (1,028
collected, 272/295 contract, 7 migrations) — but it contradicts itself, citing
977 collected at :217 and 1,028 at :223, 256 mypy files at :218 and 262 at :225,
and two different Alembic heads (`2f31c7d0a1b2` at :99, `c0d0e0f0a7b1` at :231;
the mergepoint at :231 is the real head).

`FLEET_LAW.md:13` lists shared files as `app/config.py` while
`AGENT_FLEET.md:87` lists `backend/app/config.py`. Same files, two path
conventions. The MUST NOT TOUCH column of the ownership table uses the same
`backend/`-less shorthand, which is why it cannot be machine-verified while the
OWNS column can (and now is — every OWNS path resolves on disk, checked by
`make baseline-check`).

### 4.3 Other stale statements worth knowing about

- `docs/PLAN.md:7` still reads "**No implementation should start until this plan
  is approved**" while the same suite reports 19 domains factor-complete.
- `docs/NEXT_STEPS.md:4` pins "HEAD: 431dd95"; HEAD is now `f640bba`.
- `docs/AGENT_FLEET.md` §5 marks all 20 agents `done` while §9's product
  done-when checklist is 19 unchecked boxes out of 20. Both are accurate under
  their own definition — agents finished their assignments; the product gates
  they were meant to satisfy have not been measured — but read together with no
  explanation they look like a contradiction.
- `docs/AGENT_BRIEFS.md:460` points at "AGENT_FLEET.md §6" for done-when; in
  v3.0 §6 is merge order and done-when moved to §9.
- Checked-in launchd plists and the fleet law hardcode
  `/Users/sahajpatel/Code/ev`, so they are owner-machine-specific by design.

---

## 5. What is broken at HEAD

Measured on Linux with `uv sync --extra s3 --extra dev` — the exact extras
`.github/workflows/ci.yml` installs.

### 5.1 CI has never executed

```text
$ gh run list --limit 12
completed  failure  ci             main  push      31588434523  3s
completed  failure  nightly-eval   main  schedule  31562397820  13s
... every run since 2026-08-09: failure, 2–13s

$ gh run view 31588434523
X verify in 1s · X postgres-e2e in 2s
ANNOTATIONS
X The job was not started because recent account payments have failed or your
  spending limit needs to be increased.
```

Both jobs are blocked before their first step. No lint, no mypy, no pytest, no
eval gate has ever run on GitHub. Every "suite green" claim in the docs comes
from a local macOS run. **Owner action required** — this cannot be fixed from
inside the repo.

### 5.2 The suite is macOS-only, so CI would still be red

`ubuntu-latest` is what the workflow targets. On Linux:

```text
$ uv run pytest -q
6 failed, 1018 passed, 23 skipped in 397.51s
```

| Failure | Cause | Fix | Owner |
| --- | --- | --- | --- |
| `test_gear_alerts.py::test_gear_report_is_honest_about_what_this_mac_can_see` | Asserts `report["mac_observed"]["system"] == "Darwin"`; gets `Linux` | Skip unless `sys.platform == "darwin"`, or assert against the running platform | 15 ORACLE |
| `test_tools_sandbox.py::test_escape_14_memory_bomb_blocked_by_watchdog` | Expects the macOS RSS watchdog path; Linux enforces via `RLIMIT_AS` | Skip on non-darwin, or assert the Linux path blocks too | 10 CORTEX |
| `test_vision_corpus.py` (4 tests) | `app/vision/corpus.py:142` raises `RuntimeError("pillow is required …")`; pillow is in the `face` extra, which CI does not install | Add `pytest.importorskip("PIL")` — the existing `skipif` only guards `CORPUS_CLASSES == []` | 6 EYES |

The pillow case is a direct FLEET_LAW §7 violation: "Tests that need weights use
`pytest.mark.skipif` — skip, never fail." It is currently invisible because the
owner's machine has `make face-install` applied.

### 5.3 mypy is red at HEAD

```text
$ uv run mypy app clients
app/ops/metrics.py:125: error: Unsupported operand types for - ("None" and "float")  [operator]
Found 1 error in 1 file (checked 264 source files)
```

`_swap_mb()` parses `/proc/meminfo` and computes `used = total - free` on the
`SwapFree:` branch, but `total` is only assigned on the `SwapTotal:` branch and
is `None` until then. This is also a latent runtime `TypeError` on any Linux
host whose `/proc/meminfo` lists `SwapFree` before `SwapTotal` or omits
`SwapTotal`. Guarding the subtraction fixes both. `AGENT_FLEET.md:225` claims "0
issues in 262 source files"; the tree is now 264 files and one error. Owner:
Agent 20 LAUNCH (`app/ops/**`).

### 5.4 What is green

```text
$ uv run ruff check app clients tests
All checks passed!

$ uv run python -m app.scripts.eval_gates --report eval/last-run.json
Summary: 18/18 gates, 110/110 checks passed, 5 skipped (explicit reasons above).
exit 0
```

The 5 skips are `asr_quality`, `speaker_security`, `retrieval_quality`,
`face_recognition`, and `wake_reliability` — all "no eval artifact" because
`backend/eval/ml/` is gitignored. This is the designed behaviour, and it is the
single best-engineered thing in the repo: a missing or `degraded: true`
artifact skips rather than passes, so no double can be reported as a measured
quality number. `AGENT_FLEET.md:230` cites 124/124 checks with 3 skips, which is
what the same command produces once real ML artifacts exist locally.

### 5.5 Environment reality on a non-macOS agent VM

`uv sync --extra s3 --extra dev` succeeds and the app imports (after
`EV_VAULT_KEY` is set — it has no default and fails validation when empty).
Absent from that install: `PIL`, `numpy`, `onnxruntime`, `cv2`, `torch`. Absent
from Linux entirely: Swift, `swiftc`, Xcode, macOS frameworks (Vision,
ScreenCaptureKit, AVFoundation), Homebrew services, and launchd. So `ios/`,
`macos/`, `helpers/evvision/`, `brew/`, and `launchd/` cannot be built or
verified anywhere except the owner's Mac. Any agent assigned to those paths on a
Linux VM can only do static review, and should say so rather than claiming a
build.

---

## 6. Structural observations

**The good, worth protecting.** Provider registries make engines swappable by
config rather than code, and unknown provider names raise instead of silently
falling back to `echo`. Lazy imports keep a bare `s3,dev` install able to import
every module. The `degraded=true` contract plus SKIP-not-PASS gating is the
mechanism that makes "no lying in code" enforceable rather than aspirational.
The append-only API contract means 295 operations cannot quietly change shape.
Tests are hermetic: SQLite, fresh schema per test, offline doubles.

**The fragile.**

- *Ownership is documentation, not enforcement.* Nothing mechanically prevents
  an agent from editing outside its OWNS list. With 20 agents in one worktree
  this is the highest-probability failure mode, and it is silent. A pre-commit
  or CI check mapping changed paths to the ownership table would close it.
- *Shared append-only files are the real contention point.* `config.py` (239
  settings), `models.py` (67 tables), `schemas.py` (~3.1k lines), and five API
  routers are edited by everyone. The `# --- AGENT <N> <CODENAME> ---` block
  convention is the only thing keeping them mergeable.
- *A single worktree with no branch-per-agent* means every agent sees every
  other agent's uncommitted work, and "verify the suite is green" is a race.
- *Doc volume outruns doc maintenance.* 49 docs totalling ~700 KB, several
  overlapping (`AGENT_FLEET` / `AGENT_LAUNCH` / `AGENT_BRIEFS` / `FLEET_LAW` all
  restate the bans; `PLAN` / `ROADMAP` / `NEXT_STEPS` / `WORK_BREAKDOWN` all
  restate status). Every restatement is a place for drift, which is what §4.2
  measures.
- *Verification is single-platform.* macOS is the only place the full stack has
  ever been validated, and §5.2 shows the suite has hardcoded that assumption in
  at least two tests.

---

## 7. Recommended sequence

Ordered by ratio of confusion removed to work required. Each item names the
owning agent per `AGENT_FLEET.md` §2.

1. **Unblock GitHub Actions billing** (owner, outside the repo). Until this is
   done, no automated claim about this repo is verifiable.
2. **Make the suite pass on `ubuntu-latest`** — three small edits from §5.2 plus
   the mypy guard from §5.3. Agents 6, 10, 15, 20. This is what turns item 1
   into an actual signal.
3. **Collapse the roster to one model.** Publish the 20-agent launch pack and
   update `test_agent_fleet_docs.py` in the same commit, then correct
   `README.md`, `NEXT_STEPS.md`, `PLAN.md`, and `WORK_BREAKDOWN.md` to point at
   it. Agent 1 CONDUCTOR. Until then, `AGENTS.md` §2 is the tiebreak.
4. **Normalise shared-file paths** to the `backend/`-prefixed form in
   `FLEET_LAW.md` §3 and the MUST NOT TOUCH column, so ownership becomes
   machine-checkable in both directions. Agent 1.
5. **Refresh the drifted counts** in `README.md`, `NEXT_STEPS.md`, and
   `ENVIRONMENT.md` from `make baseline`, and reconcile the internal
   contradictions in `AGENT_FLEET.md` §12. Agent 1 and doc owners.
6. **Add ownership enforcement**: a check that maps changed paths to the
   ownership table and fails on a cross-owner edit. Agent 1.
7. **Then resume depth work** on the `NEXT_STEPS.md` tracks — real voice, iOS
   shell, calendar/health signal, retrieval quality, hardening — and produce the
   five missing ML eval artifacts so the skipping gates start measuring.

---

## 8. How to keep this document true

```sh
make baseline          # print measured metrics
make baseline-write    # re-record tools/baseline.json after a legitimate change
make baseline-check    # fail on drift beyond budget or a broken invariant
```

`tools/baseline.py` is stdlib-only and imports nothing from the backend, so it
runs on a bare checkout before `uv sync`. `--check` enforces exact invariants
(fleet size agrees across `FLEET_LAW.md`, `AGENT_FLEET.md`, and `AGENTS.md`;
every OWNS path resolves; every shared append-only file exists; the contract
locks only `/v1`) and compares growth metrics against `tools/baseline.json` with
a 25% drift budget, so ordinary growth is silent and a 2x swing like §4.2 is
loud. `backend/tests/test_workspace_invariants.py` runs the invariants in the
normal suite, which means the roster can never silently fork into two numbering
schemes again.
