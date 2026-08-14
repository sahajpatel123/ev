# WAVE LIFE — Always-on presence (Agent 14 PULSE)

EVIE is present on the Mac and both iPhones, not only when a terminal is open.
This document is the operator contract for the device registry, push paths,
life-action routing, job lifecycle, launchd topology, and the owner boot
checklist.

## 1. Multi-device registry schema

Source of truth: the `devices` table (extended by Agent 14) plus runtime
heartbeats.

| Column | Meaning |
| --- | --- |
| `id` | Stable UUID device identity |
| `name` | Human label (`Mac`, `Phone A`, `Phone B`) |
| `device_type` | `mac` / `phone` / `watch` / `desktop` / `unknown` |
| `platform` | `apple` / `android` / `web` / `unknown` |
| `trust_level` | `device` (paired) or `owner` (owner-promoted) |
| `paired_at` | When the device was registered/paired |
| `owner_id` | Owning identity (single-owner invariant) |
| `capabilities` | Actuator/sensor set (`wake`, `voice`, `notifications`, `attention`, `messaging`, `call`, `push`) |
| `last_seen_at` | Refreshed by auth and `/v1/runtime/heartbeat` |
| `push_token` / `push_token_updated_at` / `push_bundle_id` | APNs registration |
| `revoked_at` / `revoked_reason` | Deregistration |

Reachability is derived, never claimed: `online` = heartbeat within
`EV_RUNTIME_HEARTBEAT_GRACE_SECONDS`, `away` = within 24 h, `unknown` otherwise.

Seed the fleet once (idempotent):

```zsh
cd backend && uv run python -m app.notify.registry --tokens
```

This creates `Mac`, `Phone A`, and `Phone B` with the documented capability
sets and prints each device's bearer token once.

## 2. Device trust / pairing flow

1. Owner runs `POST /v1/devices` with the master key (`DeviceCreate` now
   accepts `device_type` and `platform`); the response contains the one-time
   device bearer token.
2. The device uses that token for every API call; `_resolve_actor` refreshes
   `last_seen_at`, which is the reachability source.
3. `trust_level=owner` is only set through the owner ceremony
   (`POST /v1/identity/...`); plain pairing stays `trust_level=device`.
4. `DELETE /v1/devices/{id}` revokes; `DELETE /v1/devices/{id}/push-token`
   removes just the push registration.

## 3. APNs registration and delivery path

SUIT's iOS app calls the exact endpoint the fleet agreed on:

```text
POST /v1/devices/{device_id}/push-token
{"token": "<hex>", "platform": "apns", "bundle_id": "com.ev.ios"}
```

Implemented by Agent 14 (register/rotate = same POST, deregister = DELETE,
status = `GET /v1/devices/{id}/status`). The token is stored on the Device row
and is the routing key for APNs.

Delivery: `APNsNotifier` builds an ES256 provider JWT from
`EV_NOTIFY_APNS_KEY_PATH/KEY_ID/TEAM_ID`, posts to
`https://api.push.apple.com/3/device/{token}` with `apns-topic`, and only
returns `delivered` on HTTP 200 (with the `apns-id`). Every missing piece
(no token, no key, no topic, network failure, non-200) fails loudly with an
`apns_*` reason. No fake success is possible.

## 4. macOS notification path

Exactly one native path: backend → SUIT's `EVNotificationHelper` → `ev://`
URL scheme → EV.app → `UNUserNotificationCenter`.

```dotenv
EV_NOTIFY_MACOS_HELPER_PATH=/Applications/EV.app/Contents/MacOS/EVNotificationHelper
```

The backend keeps the helper contract (`--id --bundle-id --title --body`,
`--check-permission`). Delivery receipts are recorded in `notifications`, and
EV.app can ack them at `POST /v1/notify/{id}/receipt` (marks the row
`attention_kind=acknowledged`; acknowledged events never re-notify).

## 5. Device-routing rules

Routing is capability + reachability based, in `app/notify/routing.py`:

| Outbound job | Required capability | Example target |
| --- | --- | --- |
| `messaging.send` / `mail.send` | `messaging` | online Mac, else push-registered iPhone |
| `phone.call` / `facetime.call` | `call` | online Mac, else push-registered iPhone |
| notification / alert / digest | `attention` | best reachable attention device |

Selection order: non-revoked devices only → required capability → online >
away > unknown → push-registered preferred for remote delivery → most recent
heartbeat. When the Mac helper is unavailable, jobs route to the best
registered iPhone instead of silently failing.

## 6. Job lifecycle states

`LifeOutboundAction.status` keeps Agent 12's device-proxy outbox contract
(`queued` → `delivered` / `failed` / `cancelled`). The Agent 14 `lifecycle`
column adds the full state machine:

| State | Meaning | Set by |
| --- | --- | --- |
| `queued` | Created, no device assigned | integrations service |
| `dispatched` | Best capable device assigned, visible in its outbox | daemon tick |
| `acknowledged` | Device claims the job | `POST /v1/runtime/life-jobs/{id}/claim` |
| `executed` | Device posted evidence (`message_id`/`call_id` + timestamp) | daemon reconcile |
| `failed` | Device reported failure, cancelled, or evidence missing | device result / reconcile |

`GET /v1/runtime/life-jobs` shows the full lifecycle. Nothing is ever marked
`executed` without provider evidence.

## 7. launchd service topology

`launchd/install.sh` supervises:

| Label | Process |
| --- | --- |
| `ev.api` | uvicorn API on 127.0.0.1:8000 |
| `ev.opencode` | headless `opencode serve` (brain) |
| `ev.ears` | always-on mic/wake runtime |
| `ev.runtime` | daemon tick, DLQ, digest, routing, boot beacon |
| `ev.scheduler` | routines + live maintenance |
| `ev.worker` | RQ ingestion |
| `ev.collector` | perception collector |

All use `KeepAlive` + `ThrottleInterval`, restart after `kill -9`, and log to
`~/Library/Logs/ev/*.log` (newsyslog rotation).

## 8. Owner boot checklist

After reboot, verify EVIE is alive **without opening a terminal**:

1. Notification Center shows **"EVIE is alive"** (boot beacon) — sent once per
   runtime daemon start when `EV_NOTIFY_BOOT_BEACON=true`.
2. Otherwise, the menu-bar EV app shows API + OpenCode + Ears status.

From a terminal, the full checklist:

```zsh
./launchd/check.sh                # one-shot status board (also: make boot-check)
launchctl list | rg '^gui/.*ev\.(api|opencode|ears|runtime|scheduler|worker|collector)'
curl -s -H "Authorization: Bearer $EV_MASTER_KEY" http://127.0.0.1:8000/v1/health
curl -s -H "Authorization: Bearer $EV_MASTER_KEY" http://127.0.0.1:8000/v1/runtime/health
curl -s -H "Authorization: Bearer $EV_MASTER_KEY" http://127.0.0.1:8000/v1/devices
curl -s -H "Authorization: Bearer $EV_MASTER_KEY" http://127.0.0.1:8000/v1/runtime/notify/status
cd backend && uv run python -m app.workers.runtime_healthcheck --soak
```

`/v1/runtime/health` now includes a `device_registry` check (Mac + Phone A +
Phone B present, reachable count, push-ready count) and a `notifications`
check (backend availability + macOS permission).

## 9. Health / verification commands

```zsh
make lint && make typecheck          # PULSE paths are clean
cd backend && uv run pytest tests/test_notify.py tests/test_runtime.py \
  tests/test_device_listener.py tests/test_routines.py tests/test_actions.py -q
make soak-audit                      # 72 h missed-tick audit
uv run python -m app.notify.registry --tokens   # seed/verify fleet
```

## 10. Recovery: offline or unregistered device

- **Offline device**: jobs stay `queued` (never faked); when the device
  heartbeats again, the daemon routes it to the best currently-reachable
  device. Check `GET /v1/runtime/life-jobs?lifecycle=queued` and
  `GET /v1/devices` for `presence`.
- **Unregistered device**: run the fleet seed, then pair with
  `POST /v1/devices`; the iOS app re-uploads its APNs token on next launch.
- **Stale push token**: `DELETE /v1/devices/{id}/push-token` then re-register;
  APNs failures are recorded with `apns_*` reasons in `/v1/runtime/notifications`.

## 11. Live verification (2026-08-13)

Evidence captured against the launchd-supervised live stack:

```text
launchctl print gui/$UID/ev.{api,runtime,opencode,ears} → state = running
runtime soak audit (72 h, before restart test):
  healthy=True, interval_seconds=30, max_gap_seconds=35.5, ticks=2088
kill -9 on ev.runtime → launchd respawned (new pid), ticks resumed
post-restart cadence: 30.0–30.7 s gaps, latest tick age ≈ 15 s
GET /v1/devices → Mac, Phone A, Phone B (device_type mac/phone/phone)
GET /v1/runtime/health → device_registry: ok (registered=3, push_ready=1 after token)
POST /v1/devices/{id}/push-token → registered=True; DELETE → registered=False
GET /v1/runtime/notifications → kind=presence, attention_kind=evie_initiated,
  backend=macos ("EVIE is alive" boot beacon delivered)
```

The single 283.5 s gap in the audit window is the intentional kill -9/restart
test; supervision recovered and the 30 s cadence resumed. A clean 72 h window
was healthy before that test (max gap 35.5 s).

Still requires the owner/other agents: a real reboot test, APNs credentials,
SUIT's Xcode build, and Agent 7/11's remaining suite fixes.
