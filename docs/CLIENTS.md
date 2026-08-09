# EV — Client & Device Architecture

**Version 1.0** — the "suit" (iPhone/Watch) and "workbench" (Mac/web/CLI) surfaces,
their shared sync protocol, and HUD rendering targets.

## 1. Shared principles

- **One backend, one memory.** Clients are thin, authenticated viewers/capturers.
- **Capture-first.** Everything a client does becomes an event; derived memory lives
  server-side.
- **Offline-first capture.** Events queue locally and sync; no data is lost.
- **Append-only sync.** Clients never edit events; they pull incremental state and
  push new events.
- **HUD-ready.** Time-sensitive outputs render from `ev.hud.*` JSON schemas, not
  bespoke UI payloads.

## 2. iOS app (SwiftUI, M2)

### 2.1 Module map

```text
EVApp
├── Capture       (voice, camera, share, notes, quick capture)
├── Chat          (streaming chat + provenance chips)
├── Memory        (browser, timeline, audit, typed lists)
├── Today         (dashboard: briefings, alerts, health, gear)
├── Research      (sessions, sources, citations)
├── Maker         (projects, BOM, print jobs)
├── Settings      (profile, attention, privacy, devices, voice)
└── WatchCompanion (quick capture + HUD cards)
```

### 2.2 Stack

- SwiftUI + MVVM; async/await; URLSession streaming for SSE.
- Shared client package: `ios/EVClient` (API client, `ev.hud.card.v1` model +
  compact renderer, offline capture queue with idempotency keys) used by both
  iPhone and Watch targets; headless validation via `swift run EVClientCheck`.
- Local store: SQLite/Core Data mirror for offline queue + cached browse.
- Keychain: master key/device token; biometric unlock option.
- HealthKit: read HR/HRV/sleep/activity with per-metric permission; no write-back.
- Notifications: local scheduling for offline alerts; APNs via Tailscale relay for
  server-triggered alerts (M5).
- Share extension: text/images/files → `POST /v1/events` or offline queue.
- Camera/photo picker; voice via on-device Speech framework (or Whisper later).
- Privacy UI: per-source permission screen, revocable switches, screenshot blocking
  for sensitive content.

### 2.3 Offline queue & sync

1. Capture writes to local queue (`pending_events` with `Idempotency-Key`).
2. On connectivity, POST each pending event; on 201 mark synced; on 409 drop
   duplicate; on 422 quarantine for user review.
3. Pull: `GET /v1/timeline?cursor=<last_seen>` → merge into local mirror.
4. Conflict-freedom: events are append-only; derived state always comes from server.

## 3. Watch app (M2/M5)

- Quick capture: 3-tap voice note or dictation → event.
- Today card: next event, readiness, alerts (compact `ev.hud.card.v1`).
- Tactical quick card (< 800 ms): precomputed briefing rendered as complication or
  glance.
- Minimal cache; sensitive payloads not persisted beyond the session.

## 4. Mac / web workbench (M2)

- Web client: FastAPI-served static SPA (no third-party scripts, CSP headers) —
  dashboard, chat, memory browser, audit, settings.
- Mac desktop (optional later): Tauri/Electron wrapper around the same SPA or a
  native SwiftUI client; system tray quick capture.
- CLI (`clients/cli`): `ev capture`, `ev ask`, `ev timeline`, `ev memories`,
  `ev audit`, `ev correct`, `ev forget`, `ev restore`, `ev card`, `ev doctor`,
  `ev checkup`, `ev export`, `ev queue`, `ev sync` — scriptable and
  headless-friendly (see §4.1).

### 4.1 CLI reference

Environment: `EV_API_URL` (default `http://127.0.0.1:8000`), `EV_API_KEY`
(master or registered-device token), and `EV_CLI_QUEUE_DIR` (default
`~/.ev/queue`) for the offline capture queue.

```text
ev capture "remember this" | ev capture -       # text or stdin -> event
ev ask "what did I decide about X?"
ev timeline --limit 20                           # recent events + cursor
ev memories --type decision --search sqlite
ev audit <memory_id>                             # why does EV know this?
ev correct <memory_id> "fixed text" --reason ...
ev forget <memory_id> | ev restore <memory_id>
ev card                                          # ev.hud.card.v1 text render
ev quickcard "Renegotiation with X"               # ev.hud.quickcard.v1 (<800 ms)
ev doctor | ev checkup                           # health / full calibration
ev export --output ev-bundle.json
ev import ev-bundle.json --mode merge            # event-sourced restore/merge
ev onboarding "first memory" ...                 # guided first memories + audit
ev queue | ev sync                               # offline captures -> server
```

`ev capture` writes to the local queue when the server is unreachable; every
queued capture carries an `Idempotency-Key`. `ev sync` replays the queue: 201
marks synced, 409 drops the duplicate, 422 quarantines the record, and a
connectivity failure leaves the queue intact for the next attempt.

### 4.2 Web workbench

Served at `/app` by the FastAPI app (no third-party scripts, strict CSP,
`X-Frame-Options: DENY`). One self-contained page: HUD card
(`ev.hud.card.v1`), capture (with idempotency keys), chat with provenance
chips, memory browser + audit drill-down, and timeline. The API URL/key are
kept in `localStorage`; same-origin API is the default, so `connect-src 'self'`
holds. Captures made while offline are queued in `localStorage`
(`ev.offlineQueue`) with idempotency keys and replayed by "Sync queue": 201
synced, 409 dropped, 422 quarantined (`ev.quarantine`) — the same contract as
the CLI `ev sync`. A "Getting started" panel walks the first memories and then
shows the first audit trail (UX onboarding steps 3–4), matching the CLI
`ev onboarding` flow.

## 5. HUD rendering targets (M5)

| Target | Renderer | Schema |
| --- | --- | --- |
| Watch complication/glance | WatchKit/SwiftUI | `ev.hud.card.v1` |
| Lock Screen widget | WidgetKit | `ev.hud.card.v1` |
| Mac dashboard | Web/desktop SPA | `ev.hud.card.v1` / `ev.hud.briefing.v1` |
| Voice one-liner | TTS | `ev.hud.briefing.v1.summary` |
| AR overlay (future) | ARKit overlay | `ev.hud.briefing.v1` (same data) |

One JSON → many renderers; schema validation runs in CI.

## 6. Sync protocol (v1)

```text
Client ──► POST /v1/events {Idempotency-Key, event}
Client ◄── 201 EventOut | 409 duplicate | 422 validation
Client ──► GET /v1/timeline?cursor=<last_event_occurred_at>
Client ◄── {events, next_cursor}
```

- Cursor = last-seen `occurred_at`; server returns strict > cursor.
- Poll interval: active app 30 s; background via background fetch/APNs signal.
- Clock skew: clients send `occurred_at`; server clamps future timestamps to
  `ingested_at` and records original in metadata.
- Idempotency: `POST /v1/events` accepts an `Idempotency-Key` header and returns
  409 with the existing event on replay; the key hash is stored on the event
  row, making offline replay safe (`ev sync`).

## 7. Notifications (M5)

- Priority tiers: urgent / useful / background.
- Quiet hours enforced server-side (config) and client-side (local queue).
- APNs via Tailscale relay: server pushes only alert ids; clients fetch details (no
  sensitive content in push payloads).
- Watch haptics for urgent tiers only.

## 8. Client test matrix

| Flow | iOS | Watch | Web | CLI |
| --- | --- | --- | --- | --- |
| Capture → timeline | ✓ | ✓ | ✓ | ✓ |
| Chat + provenance | ✓ | glance | ✓ | ✓ |
| Memory browse/audit | ✓ | — | ✓ | ✓ |
| Offline queue sync | ✓ | ✓ | — | ✓ |
| Tactical card | ✓ | ✓ | ✓ | — |
| Health read (M5) | ✓ | ✓ | — | — |
| Maker flows (M5) | ✓ | — | ✓ | ✓ |
