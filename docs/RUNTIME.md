# EV Runtime & Notification Delivery (PULSE)

Agent 14 PULSE owns the 24/7 runtime daemon, real notification delivery, and
the launchd supervision that replaces Docker as the daily driver on an 8 GB
Mac.

## Stack

| Process | Entrypoint | Job |
| --- | --- | --- |
| API | `uvicorn app.main:app` | HTTP surface, action approval, receipts API |
| Worker | `python -m app.workers.runner` | RQ ingestion |
| Scheduler | `python -m app.workers.scheduler` | routines + live maintenance |
| Runtime | `python -m app.workers.runtime_daemon` | daemon tick, DLQ, alerts, digest |
| Ears | `python -m clients.ears.main` | always-on mic/wake (Agent 3) |
| Collector | `python -m clients.collectors` | perception collector |
| Listener | `python -m clients.device_listener` | heartbeat + offline capture (any machine) |

## Bring the stack up (launchd, the daily driver)

```zsh
cp .env.example .env            # set EV_MASTER_KEY, EV_VAULT_KEY, EV_BACKUP_PASSPHRASE
make install
./launchd/install.sh            # user LaunchAgents (login start)
# Boot-time, before login (sudo):
sudo ./launchd/install.sh --system
sudo cp launchd/ev.newsyslog.conf /etc/newsyslog.d/ev.conf
```

Each plist uses `KeepAlive` + `ThrottleInterval` and restarts after `kill -9`.
Logs: `~/Library/Logs/ev/*.log`, rotated daily by newsyslog.

Compose remains for Postgres/Redis/minio and for a containerized fallback:

```zsh
make compose-up
```

## Notification delivery

Backend is selected with `EV_NOTIFY_BACKEND`:

- `console` (default, CI double) — prints a JSON receipt line to stdout.
- `macos` — builds `app/notify/macos/EVNotificationHelper.swift` into
  `EV_NOTIFY_MACOS_BUILD_DIR` with `swiftc` and sends via
  `UNUserNotificationCenter`, so notifications appear in Notification Center
  with every app closed, support actions, and respect Focus modes. If the
  helper cannot be built or the sandbox blocks its `usernotifications.listener`
  connection, `terminal-notifier` is tried, then `osascript display
  notification` when `EV_NOTIFY_MACOS_ALLOW_OSASCRIPT=true` (documented
  limitations: no action buttons, bundled-app identity quirks).
- `webhook` — signed POST (`X-EV-Signature: sha256=…`) to
  `EV_NOTIFY_WEBHOOK_URL` with `EV_NOTIFY_WEBHOOK_SECRET`.
- `apns` — written but inert until Agent 18's app registers a token
  (`EV_NOTIFY_APNS_ENABLED` must be true and a token present; otherwise every
  send fails honestly with `apns_inert`).

### Attention budget (enforced in code)

- Quiet hours (`EV_QUIET_HOURS_START`/`END`): non-emergency notifications are
  suppressed with reason `quiet_hours`. Emergency = explicit `emergency`
  flag, `priority >= EV_NOTIFY_EMERGENCY_PRIORITY_THRESHOLD` (0.7), or
  `urgent`/`notify_card` tier. The only quiet-hours path is the daily digest.
- Daily cap: `EV_DAILY_ALERT_BUDGET`; past-cap items are suppressed with
  reason `daily_cap`.
- Dedup: identical fingerprints inside `EV_NOTIFY_DEDUP_WINDOW_SECONDS` are
  suppressed with reason `duplicate`.
- Failure limit: `EV_NOTIFY_MAX_ATTEMPTS` failed deliveries for the same item
  suppress further attempts with reason `max_attempts`.

Everything is recorded in the `notifications` delivery ledger.

## Verify a notification actually arrived

1. Send one:
   ```zsh
   curl -H "Authorization: Bearer $EV_MASTER_KEY" \
     -H "Content-Type: application/json" \
     -d '{"title":"EV test","body":"Notification Center check","emergency":true}' \
     http://127.0.0.1:8000/v1/runtime/notify
   ```
2. Read the receipt:
   ```zsh
   curl -H "Authorization: Bearer $EV_MASTER_KEY" \
     http://127.0.0.1:8000/v1/runtime/notifications?status=delivered
   ```
3. Check the backend is healthy:
   ```zsh
   curl -H "Authorization: Bearer $EV_MASTER_KEY" \
     http://127.0.0.1:8000/v1/runtime/notify/status
   ```
4. Look at Notification Center. `delivered_at` is only stamped from backend
   evidence, never from a caller-supplied result.

macOS permission: the helper requests authorization on first send. If it is
denied, receipts fail with `notification permission denied` and the status
endpoint reports `permission: denied`; enable EV in System Settings >
Notifications.

## daemon_tick_seen

The runtime daemon writes a `RuntimeEvent` with `kind=daemon` every
`EV_RUNTIME_DAEMON_TICK_SECONDS` (30 s). The compose healthcheck verifies a
tick exists in the last 120 s:

```zsh
cd backend && uv run python -m app.workers.runtime_healthcheck
```

Or read the event feed directly:

```zsh
curl -H "Authorization: Bearer $EV_MASTER_KEY" \
  "http://127.0.0.1:8000/v1/runtime/sync?limit=50"
# look for {"kind":"daemon", ...}
```

### 72 h soak audit

The acceptance "0 missed daemon ticks under launchd" is measured, not prose:

```zsh
cd backend && uv run python -m app.workers.runtime_healthcheck --soak
# window default 72 h; tune with --window-hours and --tolerance-gaps
```

It counts `kind=daemon` runtime events in the window, finds the largest
inter-tick gap, and exits non-zero when a gap exceeds
`runtime_daemon_tick_seconds × (tolerance_gaps + 1) + 10`. Run it at the end
of the 72 h drill; `make soak-audit` is the same thing. A healthy 72 h run
looks like:

```text
runtime soak audit: healthy=True, interval_seconds=30, max_gap_seconds=34.5,
ticks=8639, tolerance_seconds=70, window_hours=72.0
```

## Recover a dead worker

Workers are supervised by launchd (`KeepAlive`). To inspect/restart manually:

```zsh
launchctl print gui/$UID/ev.worker
launchctl kickstart -k gui/$UID/ev.worker
```

If a process dies mid-job, the failure lands in the dead-letter queue (DLQ)
instead of disappearing.

## Drain the DLQ

```zsh
curl -H "Authorization: Bearer $EV_MASTER_KEY" \
  http://127.0.0.1:8000/v1/runtime/dead-letters
```

- `retry` re-enqueues a letter (daemon re-enqueues `retrying` letters each
  tick).
- `discard` stops retrying; the daemon escalates the discard once as a
  notification (`kind=dead_letter`) so "why didn't she tell me?" has an
  answer.
- Permanently discarded jobs are visible in the ledger forever.

## Offline captures

`clients/device_listener.py` heartbeats, polls wake arbitration, and replays
an offline queue (JSONL with idempotency keys) to `/v1/events` or
`/v1/live/events`. Start it:

```zsh
cd backend
EV_API_URL=http://127.0.0.1:8000 EV_API_KEY=$EV_MASTER_KEY EV_DEVICE_ID=<uuid> \
  uv run python -m clients.device_listener
```

Wake intents require real evidence (text hint for dev/test, audio ref/frames
from the ears pipeline); client-supplied signal scores are never trusted.

## 72 h acceptance drill

```text
Day 1  install launchd (or compose), register a device, start the listener
Day 1  make a watchlist item, scan, verify Notification Center arrival <= 5 s
Day 2  kill -9 the runtime daemon, verify launchd restarts it and ticks resume
Day 3  reboot, verify all six services are up without manual intervention
Every day  check daemon ticks (healthcheck), DLQ count, notification receipts
```

Acceptance gates are covered by `tests/test_notify.py`, `tests/test_runtime.py`,
and `tests/test_device_listener.py`.
