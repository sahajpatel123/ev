**Primary iPhone product (locked):** Safari PWA at `/evie/` over Tailscale. See [`docs/IPHONE_PRODUCT.md`](IPHONE_PRODUCT.md). No Xcode required. `EVApp`/Watch/Share remain a later native track.

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
  compact renderer, attachment upload for photos/files, offline capture queue
  with idempotency keys, voice session flow wake → verify → utterance) used by
  both iPhone and Watch targets; headless validation via
  `swift run EVClientCheck`.
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
  `ev checkup`, `ev export`, `ev import`, `ev onboarding`, `ev queue`,
  `ev sync`, `ev identity`, `ev voice`, `ev notify`, `ev model`, `ev people`,
  `ev train`, `ev eval` — scriptable and headless-friendly (see §4.1).

### 4.1 CLI reference

Environment: `EV_API_URL` (default `http://127.0.0.1:8000`), `EV_API_KEY`
(master or registered-device token), and `EV_CLI_QUEUE_DIR` (default
`~/.ev/queue`) for the offline capture queue.

```text
ev capture "remember this" | ev capture -       # text or stdin -> event
ev attach note.pdf                              # file/share -> attachment event
ev ask "what did I decide about X?"
ev timeline --limit 20                           # recent events + cursor
ev memories --type decision --search sqlite
ev audit <memory_id>                             # why does EV know this?
ev correct <memory_id> "fixed text" --reason ...
ev forget <memory_id> | ev restore <memory_id>
ev card                                          # ev.hud.card.v1 text render
ev quickcard "Renegotiation with X"               # ev.hud.quickcard.v1 (<800 ms)
ev voice-enroll sample*.wav --liveness live       # owner voiceprint enrollment
ev voice-verify sample*.wav                       # owner voiceprint verification
ev voice wake | verify | listen | status | end   # streaming voice sessions
ev notify test --title ... --body ...             # delivery test + receipts
ev notify history | ev notify status
ev model list | ev model pull NAME | ev model prune [--all] [--dry-run] | ev model stats
ev people enroll NAME photo*.jpg | ev people list | ev people forget <entity>
ev people correct <recognition_id> "Corrected name"
ev train dry-run --corpus-version N | ev train status | ev train rollback <id>
ev eval retrieval [--rerank] | ev eval asr [--audio file --expected "..."]
ev consent grant voice_enrollment                 # training-track consent
ev routines | ev ops | ev filter-report           # automation/ops/filter surfaces
ev doctor | ev checkup                           # health / full calibration
ev export --output ev-bundle.json
ev import ev-bundle.json --mode merge            # event-sourced restore/merge
ev onboarding "first memory" ...                 # guided first memories + audit
ev queue | ev sync                               # offline captures -> server
ev identity status                               # owner binding + trust state
ev identity owner --name "Alex"                  # establish owner (master key)
ev identity passkey add/list/remove              # WebAuthn credential ids
ev identity recovery --code ... --device-name ...# redeem one-time code (no key)
ev identity verify --purpose runtime.action ...  # re-verification for sensitive actions
```

`ev capture` writes to the local queue when the server is unreachable; every
queued capture carries an `Idempotency-Key`. `ev sync` replays the queue: 201
marks synced, 409 drops the duplicate, 422 quarantines the record, and a
connectivity failure leaves the queue intact for the next attempt. `ev attach`
queues file captures the same way (kind `attachment`, with the local file
path); `ev sync` uploads them as multipart attachments and quarantines records
whose file has disappeared.

### 4.1.1 Streaming

`ev ask` streams tokens by default (`--no-stream` opts back into the buffered
response). The CLI renders `delta` events as they arrive, prints the
output-filtered `refined` answer, and then lists provenance. `ev voice listen`
streams a live voice session: `partial` ASR hypotheses appear as they are
produced, followed by `final_transcript`, the grounded `reply`, and the TTS
`audio_ref`. Mid-stream errors are reported and exit non-zero; Ctrl-C cancels
cleanly. The same SSE parser is used by the web workbench, so both surfaces
render Agent 10's token stream and Agent 4's partial ASR transcripts.

### 4.1.2 Model, training and eval commands

- `ev model list` reads `GET /v1/gateway/models` (provider + model names).
- `ev model pull NAME` / `ev model prune` delegate to Agent 2's local
  `app.ml.cli` (checksum-verified cache operations; there is deliberately no
  HTTP endpoint for weight downloads).
- `ev train dry-run/status/rollback` wrap the adapter endpoints
  (`/v1/training/adapter/...`).
- `ev eval retrieval` runs Agent 8's `eval.retrieval.cli` harness (synthetic
  corpus by default; `--questions --database-url` for a live corpus).
- `ev eval asr` probes the configured ASR factory through `get_transcriber()`
  and reports provider, transcript, confidence, degraded state, exact-match
  and word error rate against `--expected`. With the dev double this is a hint
  echo with confidence 0.0 and is labeled as such; a real provider is required
  for WER-style evals.
- `ev model stats` prints Agent 2's arbiter state (ceiling, resident MB by
  tier, backend, free disk) from the local model CLI.

### 4.1.3 Day-1 onboarding script (10 actions or fewer)

Cold start → captured + asked + audited. Run exactly this, in order:

```bash
1  export EV_API_URL=http://127.0.0.1:8000
2  export EV_API_KEY=<master key>
3  ev identity owner --name "Sahaj"                 # owner ceremony + recovery codes
4  ev consent grant voice_enrollment
5  ev consent grant training_corpus
6  ev consent grant life_data_personalization
7  ev voice-enroll sample-*.wav --liveness live     # 5 samples
8  ev capture "I prefer fixed-term contracts for client work."
9  ev ask "What do I prefer for client work?"       # grounded, streaming
10 ev memories --search "fixed-term contracts" && ev audit <memory_id>
```

Steps 1–10 = ten actions. Step 3 prints the one-time recovery codes — store
them offline, never in EV. Step 10 proves the captured memory exists and is
auditable (source events + version chain). `tests/test_cli.py` automates this
script (`test_cli_day1_script_ten_actions_or_fewer`) so it stays green offline.

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
`ev onboarding` flow. The memory browser also edits in place — correct, forget,
and restore — matching the CLI `ev correct/forget/restore` versioned endpoints.
A Setup wizard steps through master-key check → training consent → one-time
recovery codes → first memories, the web mirror of the CLI onboarding flow.

The page is a full-screen dark console: a live signal ticker (`/v1/live/status`
events per 24h, alerts, focus), a Today tile rendered from the
`ev.hud.card.v1` JSON schema, health/focus/gear tiles, model provider + cost
stats, and notification history with delivery receipts
(`/v1/runtime/notifications`, `/v1/runtime/notify/status`). HUD cards
(`card/quickcard/briefing/focus/route`) are rendered generically from the
embedded `docs/schemas/*.json` definitions instead of hand-formatted.

Chat (`Ask` and the Conversation panel) streams Agent 10's SSE token stream:
progressive `delta` events render inline, the output-filtered `refined` answer
replaces them, and every provenance item becomes a clickable chip that expands
`/v1/audit/<memory_id>` in place. A Stop button cancels the stream; transport
errors keep the last question/utterance and show a Retry button instead of a
silently truncated answer.

The Voice session panel implements the browser round trip: wake (push-to-talk
style, `text_hint` for the dev wake engine), owner verification from a mic
sample, a hold-to-talk utterance streamed to `/v1/voice/utterance/stream`
(partial ASR hypotheses render live), and the grounded reply. When the reply
carries `tts.audio_ref`, the audio is fetched with the bearer token and played
through WebAudio (no `media-src blob:` CSP change needed). A "Test audio
output" button plays a locally generated 0.5s tone through the same WebAudio
decode/play path so the playback pipeline is provable even before a real TTS
provider is configured. With the dev TTS double there is no `audio_ref`; the
UI says so rather than faking playback.

People, Integrations and Routines panels are also part of the console:

- People: enroll a person from ≥5 photo crops, list enrollments, revoke or
  delete an enrollment, correct a recognition label, erase a whole person, and
  jump from a person card to their memory audit trail.
- Integrations: catalog, install (adapter + default scopes), OAuth start,
  scope edit, sync status, recent events, and revoke.
- Routines: overview, list, templates, create, run, enable/disable.

All controls are keyboard-accessible (`:focus-visible` outlines, disabled
state on unavailable voice actions, `aria-live` on streaming regions, reduced
motion respected, skip-to-console link).

The Models tile renders gateway provider/model/latency/cost today and will
switch to live arbiter residency (`resident_total_mb`, `ceiling_mb`, tier
breakdown, backend) automatically if Agent 2 exposes `arbiter` (or
`models_residency`) in `/v1/ops/metrics`; until then it states the gap
explicitly.

### 4.3 Local owner auto-connect ("First chat on this Mac")

When the workbench is served from the same origin on a loopback host
(`127.0.0.1` / `localhost` / `::1`), the SPA calls `GET /app/bootstrap` on
load and receives a **one-time workbench device token** — never the master
key. The token is used exactly like a manual device token and is rotated on
every bootstrap, so older tokens stop working. No URL or key is typed.

```text
1. Start the API:  cd backend && uv run uvicorn app.main:app
2. Open:           http://127.0.0.1:8000/app
3. Status becomes: "connected (this Mac)"   (no key pasted)
4. Ask EV:         type into Ask or Conversation and send
5. Optional CLI:   uv run ev workbench      (prints URL + auto-connect status)
6. Disconnect:     click Disconnect; Reconnect this Mac restores loopback
7. Remote hosts:   keep using the manual API URL + device token form
```

**Security properties (mechanism B — one-time connect token):**

- `GET /app/bootstrap` only succeeds when the peer is `127.0.0.1` or `::1`
  **and** the `Host` header is a loopback name; non-loopback clients get 403
  with no token, and a remote client spoofing `Host: localhost` still gets 403
  because the socket peer is the gate.
- The endpoint never returns `EV_MASTER_KEY`/`EV_API_KEY`. It mints a
  revocable `workbench-local` device row (trust level `owner` once the owner
  ceremony exists) and returns its plaintext token once; the stored hash is
  rotated on the next bootstrap.
- The browser stores only the device token in `localStorage` (the same slot
  the manual device-token path already uses). Static HTML/JS contain no
  secret. Master credentials are never baked into assets or persisted by the
  auto-connect path.
- "connected (this Mac)" is only shown after the token successfully
  authenticates against `/v1/timeline`; a failed bootstrap shows an explicit
  error, never a fake connected state.

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
