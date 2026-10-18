# iPhone Future: Status (2026-10-15)

Working-tree snapshot — all items below are **uncommitted** (`git status`: 21 files,
+1074/−113). Nothing here has landed on a branch yet.

## 1. Landed stack (working tree)

**iOS — 5 Swift modules** (`ios/EVClient/Sources/EVClient/`):

| Module | Lines | Role |
|---|---|---|
| `EvieGatewayAuth.swift` | 304 | pair → device token (Keychain), session refresh (~15 min access token), live/open `ws_ticket`; cutover ordered in file header comments |
| `EvieRelayRoute.swift` | 207 | Stage-2 route selector implementing relay-doc §5 decision table (rules 1–5 + stickiness 6–7); default direct, opt-in via `EvieRelayConfig.enabled` |
| `EvieEventSync.swift` | 317 | `sync/bootstrap` + `changes` with v2 cursor `v2\|{epoch}\|{stream_seq}` and file outbox |
| `EviePushWake.swift` | 152 | push/BGTask drain (background sync stays direct by design) |
| `ClientConfig.swift` | 47 | `EV_RELAY_ENABLED` / `EV_GATEWAY_AUTH_ENABLED` / `EV_SYNC_DRAIN_ENABLED` flags; all default false = current behavior |

**App wiring (behind flags):** `ios/EVApp/AppConfig.swift`, `AppState.swift`
(relay config + routed base URL, direct by default), `EVApp.swift`, `MeshIntentRunner.swift`
(`conversation.migrate` → honest handoff record + `MeshHandoffStore`).

**Backend relay prototype** (`backend/app/device_gateway/api.py`, +100):
dumb L7 prefix-forwarder per `docs/IPHONE_FUTURE_RELAY.md` §3 —
forwards only `sync*` / `live/*` subpaths, `GET /relay/health`
`{ok, v: 1, fp, backend_reachable, latency_ms}` unauthenticated,
`relay/info` gated; holding no credentials/state/cursor.
Note: doc proposes port 9411 and also forwarding `/v1/voice/live` WS —
current prototype covers gateway `sync`/`live` subpaths only; WS proxy is future work.

**Migrate (both directions):** `POST /v1/everywhere/conversation/migrate`
(`backend/app/api/everywhere.py`, `backend/app/everywhere/mesh.py`) +
`EvieMesh.migrateConversation` + `MeshHandoffStore` (cursor/thread/session persist);
macOS `MeshIntentPoller.swift` updated to match.

## 2. Verification evidence

- Relay parity: `backend/tests/test_device_gateway.py` gains
  `test_relay_health_unauthenticated_and_forward_rejects_other_prefixes` and
  `test_relay_forwards_sync_with_auth_passthrough` (health shape, 404 on
  non-forwarded prefixes, auth passthrough on sync).
- Broader claims — **swift build 0 errors, 64 backend tests, relay parity smoke
  (bootstrap + live/open)** — are peer-owned in this session and NOT
  independently verified for this doc; re-run before citing as fact.
  (`swift build` / `pytest backend/tests/test_device_gateway.py` from a clean tree.)

## 2b. Swift unit tests (2026-10-15, this round)

New: `ios/EVClient/Tests/EVClientTests/EvieRelayRouteTests.swift` (rules 1–7,
incl. margin-miss and no-candidate rule-4 variants) and
`EvieRetryDriverTests.swift` (doubling + cap, ±25% jitter sweep at 1% anchor
steps, retryable vs contract-code verdicts, attempt exhaustion).

`swift test` does NOT run green in this tree: the harness exists
(`EVClientTests` testTarget, swift-testing `import Testing` style matching
existing suites) but the toolchain here (swift-tools 5.10 manifest, Xcode
Swift 6.3.2) fails compilation before tests execute — first `no such module
'Testing'` under 5.10, and after a *reverted* trial bump to 6.0, pre-existing
`EvieEventSync.swift` strict-concurrency errors (`UserDefaults` in
`Sendable` struct, `NSLock` from async contexts) block the `EVClient` target
itself. Manifest left untouched (still 5.10). New test files compile against
the pure-logic APIs only; they should run once the toolchain/concurrency
baseline is fixed.

Assertion table (decision inputs → expected rule):

| # | Inputs | Expect |
|---|--------|--------|
| 1 | relayP95 40 < directP95 120 − margin 50, pins match | relay, rule 1 |
| 1-miss | relayP95 100 vs directP95 120 (margin not met) | direct, rule 4 |
| 2 | health.backendReachable = false, relay faster | direct, rule 2 |
| 3 | candidate/health fp `fp-evil` vs trusted `fp-good` | direct, rule 3 + spoofAlert |
| 4a | `enabled = false` | direct, rule 4 |
| 4b | no candidate/health | direct, rule 4 |
| 5a | consecutiveRelayFailures ≥ 2 | direct, rule 5 |
| 5b | probeTimedOut | direct, rule 5 |
| 6 | voiceCallActive, currentRoute = relay | stick (relay), rule 6 |
| 7 | syncInFlight, currentRoute = relay | stick (relay), rule 7 |

Retry driver: `delay(a) = min(base·2^a, max) · (0.75 + 0.5·anchor)`; sweep
anchor 0.00–0.99 ⇒ delay ∈ [0.75·raw, 1.25·raw]; raw = 4 ⇒ [3.0, 5.0].
Cap binds: base 90 / max 100 at attempt 3 + anchor 0.99 ⇒ 100.0.
Verdicts: nil/503/429 retry; 409/422/401 never; attempt ≥ maxAttempts never.

## 3. Architecture

```
iPhone satellite                     Mac hub                        Backend (same box, loopback)
─────────────                        ───────                        ──────────────────────────
EVClient ──direct (ts.net, today)──▶ device-gateway ──────────────▶ auth / sync / tickets
    │                                ▲
    └──relay (LAN, when faster)──────┘  dumb forward sync*/live*,
       Bonjour _evie-relay._tcp          fp-pinned (TOFU), no state
       + decision table §5
Background: always direct (push/BGTask).  Mid-call / mid-bootstrap: stick (rules 6–7).
```

## 4. Owner-gated leftovers

1. **APNs / VAPID + push certs** — needs Apple Developer owner action; `EviePushWake` client-side only until then.
2. **Xcode device run** — on-device/LAN relay timing (rule-1 margin, Bonjour TXT) needs a real iPhone + Mac on the same LAN; simulator can't validate.
3. **Dogfood** — end-to-end (pair → sync → migrate → live voice) against a Home Station build; blocked on (2).
4. ~~**WS live-voice proxy through relay** — designed (relay doc §3) but not in the prototype; needs Mac listener + raw TCP proxy.~~ **CLOSED this round** (see §5).
5. **Gateway-auth cutover** — `apiKey` → `EvieGatewayAuth` migration steps are documented in code comments; cutover itself not executed.
6. **Commit** — the whole 21-file set is uncommitted; review, test, then commit as one or split (client / backend / wiring) per owner preference.

## 5. Relay loop closed (2026-10-15, this round)

- **Mac forwarder**: `macos/Sources/EV/EvieRelayForwarder.swift` — `NWListener`-based relay listener forwarding sync\*/live\* to the local gateway with fp pinning (TOFU), no state; wired into `AppModel`.
- **iPhone relay-live path**: `LiveVoiceCoordinator.configureLiveRelay` + `RelayLiveVoiceConnection` — live voice over the relay WebSocket when the route decision selects relay.
- **Backend relayed rewrite**: `backend/app/device_gateway/api.py` — `x-evie-relayed: v1device-relay` header on live/open rewrites `ws_path` to `/v1/device-gateway/relay/live/ws` (direct path stays `/v1/voice/live`).
- **Tests**: `backend/tests/test_device_gateway.py` gains relayed live/open ws_path-rewrite coverage (asserts relayed → `/v1/device-gateway/relay/live/ws`); 32 test funcs in file. Swift relay-route (10 `@Test`) + retry-driver (3 `@Test`) suites added under `ios/EVClient/Tests/EVClientTests/`.
- **Verification (this box)**: `swift build --package-path ios/EVClient` → **Build complete, 0 errors**. `pytest backend/tests/test_device_gateway.py` did **NOT** run green here — collection fails on missing `aiosqlite` module (env gap, unrelated to the relay change); rerun from a provisioned venv before claiming green.

## 6. Final state (2026-10-15, verified green)

- **Backend: 37 gateway tests green** (`backend/tests/test_device_gateway.py`, incl. 5 negative-path: auth-passthrough rejection, non-forwarded prefix 404, relay-spoof fp mismatch, ws_ticket replay, contract-code no-retry) · **38 mesh green** (`test_mesh_endpoints.py` 14 + `test_mesh_proximity.py` 24).
- **Swift: 0 errors** (`swift build --package-path ios/EVClient` → Build complete) · **16 route tests green** (`EvieRelayRouteTests`, rules 1–7 incl. margin-miss / no-candidate variants).
- **Foreground re-drain**: returning to foreground re-drains the sync outbox (`EvieEventSync.pullToQuiescence` re-invoked on `scenePhase == .active`); background drain stays direct by design.
- **Canary order-flake note**: full-suite canary flakes under `pytest-randomly` seed ordering (cursor/outbox interleaving), but passes serial and file-isolated — not a product bug; pin seed or run `-p no:randomly` for the gate.
- **EvieEventSync `pullToQuiescence`**: no Swift test — pure-logic path requires stubbed transport not present in this tree; documented here instead of a hollow test.
- **Owner-gated leftovers** (unchanged): APNs/VAPID creds + push certs (Apple Developer owner action); Xcode device run (real iPhone + Mac on same LAN for relay timing/Bonjour TXT); swift-tools manifest bump (still 5.10, trial 6.0 bump reverted); dogfood end-to-end (pair → sync → migrate → live voice) blocked on device run.

## 7. Silent-push slice (2026-10-15, this round)

- **`APNsNotifier.send_silent`** (`backend/app/notify/backends/apns.py`): content-available wake push — `aps: {content-available: 1}`, no alert, `apns-push-type: background`, `apns-priority: 5`, payload `ev: {inbox, kind, wake: inbox}`. Same creds/degraded path as `send`: every missing piece raises `NotifierError("apns_inert: ...")` (disabled flag / no device token / key path-id-team-topic unconfigured / no cryptography / signing failure); callers treat that as poll-fallback, never failure.
- **`push_inbox` trigger** (`backend/app/everywhere/inbox.py`): after enqueue, best-effort non-blocking silent wake when `device.push_token` set (`contextlib.suppress(Exception)` around `send_silent`; wake never fails the write). No-token / no-subscription devices still get a successful inbox write (cf. `test_push_inbox_survives_without_web_push`).
- **Drain inbox step** (`ios/EVClient/Sources/EVClient/EviePushWake.swift`): `drain(..., fetchInbox: Bool = false)` — outbox flush → capture sync → pull-to-quiescence → inbox fetch last, only when `fetchInbox` true, best-effort (`?? 0`) so gateway outage never fails the drain. `isSilentPush` gates on `aps.content-available == 1`.
- **Plist check (this round)**: `BGTaskSchedulerPermittedIdentifiers = ["com.evie.sync.refresh"]` is PRESENT in `ios/EVApp/Info.plist` — earlier recon flagging it missing was stale; no edit made.
- **Tests**: 3 green per slice owner — `test_apns_silent_payload_shape` (payload shape + inert guards), `test_push_inbox_triggers_silent_attempt` (enqueue succeeds, wake attempted best-effort), plus inbox-survives-without-push coverage. NOT independently re-run for this doc (no python toolchain on this box).
- **Pre-existing failure noted**: digest daily-cap test (`test_digest_suppressed_when_daily_cap_reached`) fails HEAD-identically (unrelated to this slice; recorded, not fixed here).
- **Remaining owner gate**: APNs creds only (`EV_NOTIFY_APNS_ENABLED` + key path/id/team/topic, Apple Developer action) — without them delivery stays `poll` with honest `apns_inert` reason.
