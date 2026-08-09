# Routines & Automations

EVIE's controlled proactivity engine: scheduled routines, trigger-based
automations over state/live data, approval-gated actions, run history, failure
recovery, one-tap disable, and undo records.

## Model

- `routines` — a definition: kind (`scheduled` | `trigger`), cron schedule,
  trigger spec, action type/payload, quiet-hours policy, backfill limit,
  cooldown, approval strictness, undoability, enabled flag.
- `routine_runs` — one immutable-per-trigger run record: status, scheduled
  slot or trigger provenance, linked `ApprovedAction`, attempts, error, result,
  undo state. The unique `dedupe_key` is the duplicate-prevention invariant.

## Behavior guarantees

- **Deterministic scheduling**: 5-field cron (`minute hour dom month dow`) is
  validated at creation and computed in the routine's timezone (default UTC).
- **Missed-run recovery**: every tick processes at most `1 + backfill_max`
  due occurrences per routine, in order. Anything still due is picked up on
  later ticks — work is recovered, never silently skipped.
- **Trigger correctness**: triggers match on `event_type`, `source`,
  `channel_kind`, and ANDed path conditions with safe ops
  (`eq ne lt lte gt gte contains in exists`). Evaluation is wired into event
  and live-event ingestion.
- **Idempotency**: a scheduled occurrence or (routine, event) pair can only
  produce one run row, even under concurrent ticks or replayed events.
- **Authorization**: actions route through the existing runtime
  `ApprovedAction` permission matrix. A routine may only require *more*
  approval (`requires_approval`), never less. Sensitive actions stop at
  `awaiting_approval`; the user approves, then executes.
- **Auditability**: every run is visible via run history with status filters;
  user decisions (approve/deny/execute/cancel/retry/rollback/disable) are also
  written to `access_log`.
- **Failure recovery**: per-run failures are isolated and recorded on the run
  (retryable); worker-level scheduler failures go to the dead-letter queue.
- **One-tap disable**: `POST /v1/routines/{id}/disable` stops future firing
  immediately. Manual `POST /v1/routines/{id}/run` remains available for
  explicit owner-triggered runs.
- **Undo**: routines marked `undoable` support
  `POST /v1/routines/runs/{id}/rollback`, which records the reversal payload
  and transitions the run to `rolled_back`.

## API

| Method | Path | Purpose |
| --- | --- | --- |
| POST | `/v1/routines` | Create a routine |
| GET | `/v1/routines` | List routines |
| PATCH | `/v1/routines/{id}` | Update a routine |
| POST | `/v1/routines/{id}/enable` / `/disable` | One-tap enable/disable |
| POST | `/v1/routines/{id}/run` | Manual run now |
| POST | `/v1/routines/tick` | Advance due scheduled routines |
| GET | `/v1/routines/runs` | Global run history |
| GET | `/v1/routines/{id}/runs` | Per-routine run history |
| POST | `/v1/routines/runs/{id}/approve` / `/deny` | Approval decisions |
| POST | `/v1/routines/runs/{id}/execute` | Execute an approved run |
| POST | `/v1/routines/runs/{id}/cancel` | Cancel a queued/approved run |
| POST | `/v1/routines/runs/{id}/retry` | Retry a failed/denied/cancelled run |
| POST | `/v1/routines/runs/{id}/rollback` | Roll back an undoable executed run |

## Runtime

`app/workers/scheduler.py` runs the periodic tick (default every 60 s,
`EV_SCHEDULER_TICK_SECONDS`); the `scheduler` compose service ships with it.
