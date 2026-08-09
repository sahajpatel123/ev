# EV — Operations, Evaluation Gates & Roadmap Exit Gates

**Version 1.0** — the executable layer under `EVALUATION.md`, `DEPLOYMENT.md`, and
`ROADMAP.md`. The engineering invariants are: the system is rebuildable from
scripts, regressions are caught before they propagate, API contracts change
deliberately, and every important signal has an observable home.

## 1. Reproducibility

```text
make install      # backend deps (uv sync)
make compose-up   # Postgres/pgvector + Redis + MinIO + API + worker
make migrate      # Alembic forward-only
make seed         # optional demo corpus
make test         # pytest suite (fast, offline)
make eval         # ops evaluation gates -> eval/last-run.json
```

`make eval` must exit 0 on a healthy tree. It is the regression gate for the
five-minute execution loop: API contract drift, filter regressions, retrieval
quality drift, missing observability, or broken roadmap gates fail the run and
must be fixed before new behavior ships.

## 2. Evaluation gates

| Gate | Checks | Fails when |
| --- | --- | --- |
| `api_contract` | Locked manifest `backend/eval/contract_v1.json` matches live OpenAPI; every v1 route is intentional | A locked endpoint disappears or an unlocked v1 route appears |
| `retrieval` | Seeded corpus; target in top-5; `never_send_to_model` excluded; score components and weights present | Target not retrieved, privacy boundary leaks, or scoring formula drifts |
| `filter` | Benign input passes; injection blocked; credentials redacted; HUD contract repaired; grounded claims kept, ungrounded removed | Any deterministic filter invariant breaks |
| `voice` | Voice endpoints present; training enrollment accepts base64 string samples | Voice surface regressed or the consent-gated contract changed shape |
| `observability` | Health, calibration, evaluations summary, gateway calls, ops center, filter ledger endpoints exist; latency/cost budgets defined | Observability surface missing or budgets removed |
| `roadmap` | M0–M5 exit-gate endpoints all present per `ROADMAP.md` | A milestone's documented API surface regresses |

## 3. API contract stability

- v1 is additive-only. New endpoints are added to the locked manifest in the
  same commit that introduces them (`backend/eval/contract_v1.json`).
- Removing or renaming an endpoint requires a new major version and client
  capability negotiation (`docs/API.md` §1, §13).
- The manifest is a JSON file, not generated at runtime, so contract changes
  are reviewable in the diff.

## 4. Budgets (engineering invariants)

From `EVALUATION.md` §8 and `DEPLOYMENT.md` §10, enforced in
`backend/app/scripts/eval_gates.py`:

| Budget | Value |
| --- | --- |
| Event ack | < 1000 ms |
| Chat first token | < 1500 ms |
| Timeline / memory browse p95 | < 500 ms |
| Tactical pre-event briefing | < 3000 ms |
| Tactical quick card | < 800 ms |
| Typical monthly model+infra spend | ≤ $40 |

Measured values live in `/v1/gateway/calls` (latency, provider, model, tokens),
`/v1/evaluations/summary` (calibration), `/v1/diagnostics/calibrate` (component
health), `/v1/filter/ledger/aggregate` (filter decisions), and `/v1/ops/center`.

## 5. Failure recovery & graceful degradation

- Model/embedding providers down → gateway falls back to echo/mock providers;
  memory features keep working (`/v1/diagnostics/calibrate` reports the failure).
- Queue down → `EV_PROCESSING_MODE=sync` keeps ingestion functional.
- Worker crashes → Redis/RQ jobs retry; dead letters land in
  `/v1/runtime/dead-letters` with retry/discard lifecycle.
- Host reboot → `restart: unless-stopped` in `compose.yaml`; health checks
  gate API/worker startup on Postgres readiness.
- Disk/backup failure → surfaced via gear telemetry and the ops center.

## 6. Roadmap exit gates (executable)

`make eval` reports one check per milestone from `ROADMAP.md`:

- M0 Skeleton: events, timeline, chat, health.
- M1 Memory core: memories, audit, conflicts, export, rebuild.
- M2 App surfaces: devices, conversation, continue.
- M3 Intelligence: gateway tools/models, filter evaluate, patterns, sense.
- M4 Hardening: export, tombstone, identity status, calibration (backup
  restore and at-rest encryption remain manual drills).
- M5 EV Advanced: health snapshot, gear, alerts, tactical brief, research,
  projects, voice wake, live status.

Nightly runs should publish deltas of `eval/last-run.json`; any gate that flips
from pass to fail blocks the next milestone's exit sign-off.
