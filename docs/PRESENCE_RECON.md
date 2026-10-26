# PRESENCE RECON — Evie native macOS face (2026-10-26)

Worktree: `../evie-presence`, branch `evie-presence`, based on **main @
`bf0412c`**. `evie-acceptance` has no commits beyond main (it was merged via
`689bf70`), so main is the newest base and already contains the fabric work.

P1–P7 bind this whole effort: write scope is `macos/`, `docs/PRESENCE_*.md`,
`docs/contracts/PRESENCE_EVENTS.md`; no backend/ios/launchd/Makefile/contract
edits; no live effects (no install, no signing change, no launchd, no :8000
calls, no GUI windows, no event tap on the real system, no mic); no model
spend; headless tests only; no merge/deploy/push.

## 1. What exists in macos/ (reuse)

- **App shell**: `Sources/EV/EVApp.swift` — accessory (`LSUIElement`) menu-bar
  app. `AppModel` owns API client, queue, hotkeys, mic, live session;
  `MenuBarView` (525 lines) is a thin projection. `main.swift` entry.
- **Talk hotkey**: `GlobalHotkey.swift` — `NSEvent` global key-down monitor
  (needs Accessibility), currently Cmd+Shift+E (`keyCode 14`) toggling audio
  control; panic hotkey Ctrl+Cmd+Opt+P. The D2 gesture engine coexists with
  these and falls back to them.
- **Privileged bridge**: `BridgeSocketServer.swift` — unix-socket JSON server
  at `~/.ev/bridge.sock`, token `~/.ev/bridge-token`, uid-checked. Backend →
  app direction for permission-gated ops (screenshot, JXA, pasteboard,
  reminders/calendars). Presence does NOT reuse this socket for the event
  stream (it is request/response per-connection, one JSON line each way).
- **HUD precedent**: `PresenceOverlay.swift` — `PresenceController` shows
  non-activating (`[.borderless, .nonactivatingPanel]`), all-Spaces
  (`canJoinAllSpaces`), transparent `NSPanel`s with `NSHostingView` content,
  TTL auto-dismiss. D3 reuses this panel recipe. `VoiceOrbOverlay` (orb,
  currently kill-switched off) shows the launch-ordering hazard: accessory
  apps discard windows made before `applicationDidFinishLaunching`.
- **Permissions**: `PermissionCenter.swift` — 14 `PermissionKind`s including
  `.inputMonitoring` (`tccutil reset ListenEvent`), prompt-free status probes.
  D7 reuses `PermissionCenter.statuses()`.
- **Auth (LOCKED, do not touch)**: `EVAuth/APIAuthKey.swift` + `AppConfig` +
  `DeviceCredentialStore` + `scripts/package.sh sync_api_env`. Normal launch
  uses the Keychain device token (`com.ev.suit.device-auth`), Bearer on every
  request. Presence reuses `EVAPIClient(baseURL:token:)` unchanged.
- **Client transports (ios/EVClient, read-only dependency)**:
  - `EVAPIClient` — async HTTP, Bearer auth, `voiceSession` (90/180s
    timeouts, no waitsForConnectivity) for RPC; `liveSocketSession` (7-day
    resource timeout, waitsForConnectivity) for the live socket.
  - `Streaming.swift` — SSE parsing over `session.bytes` for `POST /v1/chat`
    (`askStream`) and `POST /v1/voice/utterance/stream`, with an explicit
    rule: streams that are POST turns must NEVER silently reconnect (a retry
    would re-execute the turn).
  - `LiveVoice.swift` — full-duplex `WS /v1/voice/live` with ping watchdog
    and generation guards.
  - `MeshIntentPoller` (macOS side) — 10s poll with exp backoff to 120s.
- **Build/sign**: `scripts/package.sh` builds `EV.app` without Xcode
  (`swift build -c release`, stamps `BuildInfo.swift`); `scripts/signing.sh`
  maintains the stable self-signed identity **"EV Code Signing"** so TCC
  grants survive rebuilds. Presence adds no new signing requirements.
- **Tests**: no XCTest target (Command Line Tools only). The repo pattern is
  an executable checks target — `EVMicTalkTests` (`Tests/EVTests`, `@main`
  runner, `check(name, ok, detail)` callbacks, nonzero exit on failure).
  Presence tests follow that pattern (`PresenceChecks`).

## 2. Backend APIs (read-only recon — exist today)

Base `http://127.0.0.1:8000`, Bearer device token or master key.

| API | Route | Auth | Notes |
| --- | --- | --- | --- |
| Fabric status | `GET /v1/fabric/status` | actor | `{fabric_enabled, contract "evie-fabric/1", lanes[12], panic, recent_turns[{turn_id, entrypoint, spans}]}` |
| Pending approvals | `GET /v1/fabric/approvals/pending?limit=` | actor | `{ok, count, approvals[ApprovedActionOut]}`, fabric lane types only |
| Approve | `POST /v1/fabric/approvals/{id}/approve` | master **or** device + `X-EV-Reverify` (purpose `runtime.action`) **or** WebAuthn | delegates to runtime approve; 409 on double-decide, 404 on stale/foreign id |
| Deny | `POST /v1/fabric/approvals/{id}/deny` | owner trust (no reverify) | same 404/409 semantics |
| Cancel | `POST /v1/fabric/cancel {turn_id?}` | actor | cancel one turn or all |
| Undo list | `GET /v1/fabric/undo` | actor | file snapshot/trash sidecars `{id "root::name", root, rel, kind, at}` |
| Undo restore | `POST /v1/fabric/undo/restore {id}` | actor | 409 on failure; returns receipt + `restored_at` |
| Panic state | `GET /v1/fabric/panic` | actor | `{ok, panicked, at, reason}` |
| Panic engage/clear | `POST /v1/fabric/panic`, `POST /v1/fabric/panic/clear` | **master only** | app reads state; never engages |
| Exec | `POST /v1/fabric/exec {tool, args}` | **master only** | 12 lane tools; effects park for approval |
| Runtime actions list | `GET /v1/runtime/actions?status=&limit=` | actor | full `ApprovedActionOut` rows incl `executed/failed/rolled_back` |
| Runtime approve/deny | `POST /v1/runtime/actions/{id}/approve\|deny` | same as fabric pair | canonical authority; fabric pair delegates here |
| Reverify issue | `POST /v1/identity/reverification {purpose, voiceSessionId?}` | actor | purpose-bound one-shot token for the approve header |
| Timeline | `GET /v1/timeline?limit=&since=&until=&source=&event_type=` | actor | event-store rows, cursor paging |
| Memory audit | `GET /v1/audit/{memory_id}` | actor | per-memory provenance (not a turn log) |

Ticket model (`ApprovedAction` / `ApprovedActionOut`): `id, action_type,
title, payload{tool args + __pol__:{name,target,risk_class,ttl_seconds,
expires_at,issued_at,channel,live_session_id,request_id,args_fingerprint}},
status, requires_approval, requested_by, device_id, session_id,
approved/denied/executed_at+by, result, error, rolled_back_*, created_at,
updated_at`. Risk classes `R0..R4, forbidden`; TTLs R3=120s, R4=60s, else
decision TTL or 120s. **Spoken "yes" never approves sends/deletes/SQL
DML/DDL/money/credential/first-contact** (CLAUDE.md R7, `fabric/policy.py`
`HIGH_RISK`); physical confirm = Return/click on the HUD card, typed reply,
paired phone, or decision API with the independent factor.

Receipt shape (`fabric/policy.py:_receipt`): `{ok, verified, contract,
evidence, error, undo}` — `verified` defaults false and is set only after
reading the world back. Cancel/panic/trace helpers live in
`app/fabric/{cancel,panic,trace}.py`; `trace.recent_entries()` feeds status.

## 3. Missing — CLI 1 provides (marked "provided by CLI 1" in D1)

1. **Presence event stream**: no backend→app turn stream exists today.
   `fabric/stream.py` is sentence chunking, not SSE; the two SSE surfaces are
   client-initiated POST turns; `WS /v1/voice/live` is the voice duplex.
   Needed: `GET /v1/fabric/presence/stream` (SSE) + replay cursor.
2. **Rich confirm detail**: `public_approval()` exposes only a bounded
   `arguments_preview` (6 keys × 120 chars) — not enough for "show exactly
   what will happen" (full message text, diff). Needed: full, redacted
   confirm payload on the ticket read path or inside `turn.needs_confirm`.
3. **Turn receipt/audit list**: `GET /v1/runtime/actions` covers approved
   actions; there is no "what Evie did today" (all turns, incl. auto) list
   with human labels + verified badges + undo handles. Needed: a turn/receipt
   feed, or the presence stream's `turn.completed` records must be durable
   enough to rebuild it.
4. **Undo beyond files**: undo list/restore covers file snapshots + trash
   only. Non-file undo handles arrive inside receipts' `undo` field where
   supported; the timeline renders Undo only where a handle exists.
5. **Ambient `looking_at`**: no backend source; presence-side via
   `NSWorkspace.frontmostApplication` + window title (no new permission), so
   this one is ours, not CLI 1's. The contract carries it as an app-local
   event for HUD display.

## 4. Transport decision (for D1)

**SSE `GET` on the existing authenticated channel** (`EVAPIClient`-style
request, Bearer device token, `Accept: text/event-stream`, `Last-Event-ID`
resume), parsed with the `Streaming.swift` pattern. Justification:

- The app already parses SSE and already holds long-lived connections
  (`liveSocketSession` precedent); no new dependency, no new auth.
- A `GET` stream is safe to auto-reconnect, unlike the POST-turn streams
  whose no-silent-reconnect rule exists precisely because retry re-executes.
- Server→client only, which is all presence needs (app→backend Confirm/Undo/
  Cancel stay normal RPC). WebSocket would add duplex machinery for no
  duplex need.
- `Last-Event-ID` + a bounded server ring gives at-least-once replay without
  a second endpoint; the client dedupes by event id.

## 5. Plan

- D1 contract first (this recon's §2–§4 are its input), commit within hour 1.
- D2 gesture engine as a new pure-Swift target `PresenceGesture` (zero
  AppKit) + `PresenceChecks` executable tests; CGEventTap adapter behind a
  protocol, default-off setting, existing hotkey untouched.
- D3/D4/D5 as SwiftUI views + controllers in the `EV` target (new files),
  driven by a `PresenceTransport` protocol with a fixture-replay
  implementation (D6); offscreen PNG snapshots under
  `docs/presence-snapshots/`.
- D7 `presence-check` flows as a new executable target + doc.
- Finish: reviewer subagent, `PRESENCE_MERGE_READINESS.md`, checkpoint.
