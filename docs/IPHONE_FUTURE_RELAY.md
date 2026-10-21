# iPhone Future: Mac Relay Mode

Goal: iPhone talks to Mac over LAN/Tailscale when nearby (lower latency,
works when backend WAN is flaky), direct to backend otherwise. One new doc,
no code. Grounds: static `EV_API_URL`, `*.ts.net` origin, gateway tickets.

## 0. Measured current state (2026-10)

- iPhone shell origin is static: `Info.plist EV_API_URL` defaults to
  `http://127.0.0.1:8000`; release override is the Home Station Tailscale
  origin `https://<mac>.<tailnet>.ts.net` (see `ios/EvieShell/README.md`,
  `WebCoreContainer.apiOrigin` with `evie.api_origin` UserDefaults override).
  No runtime route switching exists.
- Voice is origin-gated: only `https://*.ts.net` (and loopback) is
  voice-capable; `http://100.x` loads diagnostics but mic stays blocked
  (`BridgeSecurity.allows`, `TrustedOrigin.isVoiceCapable`).
- Auth is gateway tickets, never long-lived secrets in URLs:
  `POST /v1/device-gateway/pair` -> device token (Keychain),
  `POST /v1/device-gateway/session` refresh (~15 min access token),
  `POST /v1/device-gateway/live/open` -> one-time `ws_ticket` for
  `WS /v1/voice/live?session_id=&ticket=` (`EvieGatewayAuth.swift`).
- Sync path is `GET /v1/device-gateway/sync/bootstrap|changes` with v2
  cursor `v2|{epoch}|{stream_seq}` and file outbox (`EvieEventSync.swift`).

Relay must preserve all four: static default keeps working, voice-capable
origin rule unchanged, ticket auth unchanged end-to-end, sync cursor/outbox
byte-identical through the relay.

## 1. Topology

```
Direct (today, always works):   iPhone --(https, ts.net)--> backend/Mac hub
Relay (new, when faster):       iPhone --(LAN/Tailscale)--> Mac relay --(localhost)--> backend
```

Mac relay is a dumb L7 forwarder on the Mac hub process (same machine that
already serves the backend). It holds no credentials, no event state, no
cursor. Backend remains source of truth for auth, sync, tickets, live voice.

Why relay instead of just pointing the phone at the Mac directly (which is
what ts.net already does)? Because when iPhone and Mac share a LAN or tailnet
path, a relay listener on the LAN interface (or Tailscale serve) can beat
DERP-relayed ts.net paths on latency/jitter, and keeps working during
Tailscale control-plane or DERP outages. Relay is an optimization, never a
requirement.

## 2. Discovery

Two mechanisms, tried in order. Both yield a candidate relay base URL
(`http://<host>:<port>` + fingerprint) — the phone never trusts a URL alone.

1. **Bonjour (LAN).** Mac advertises `_evie-relay._tcp` with TXT:
   `v=1`, `fp=<sha256 of relay cert pin>`, `port=<p>`, `id=<stable Mac id>`.
   iPhone browses via `NWBrowser(.bonjourWithTXTRecord)` only while
   on Wi-Fi (`NWPathMonitor` expensive path, see slice 5). TTL: browse
   sessions are short-lived (started on route evaluation, cancelled after).
2. **Tailscale Whois (tailnet).** When Bonjour finds nothing and Tailscale
   is up, iPhone resolves the known Mac tailnet name
   (`https://<mac>.<tailnet>.ts.net`, already stored as api origin) to a
   relay candidate on the same host. No new identity system: the tailnet
   node name the user already paired is the identity. Optional
   `GET <api_origin>/v1/device-gateway/relay/info` (authenticated, same
   access token) returns `{ relay_port, fp }` so the phone learns the pin
   without hardcoding ports.

Both paths pin: the phone stores `fp` alongside the Mac id after first
successful direct connection (TOFU), and refuses relay candidates whose
presented cert/fp mismatches. Rotation = backend serves old+new pins during
overlap (same posture as `BridgeSecurity` transport-pinning note).

## 3. Forwarding protocol (via device-gateway)

No new auth protocol. Relay speaks a minimal prefix-forward:

- Relay listens on one port (suggestion: `9411`, loopback + LAN/tailnet bind
  configurable). Plain HTTP with TLS via the same cert whose pin is in the
  TXT/`relay/info` record (self-signed TOFU pin, or Tailscale Serve TLS —
  either way the phone checks `fp`).
- Relay forwards only paths under `/v1/device-gateway/` and
  `/v1/voice/live` (websocket). Everything else -> `404`. Method, query,
  headers (minus hop-by-hop), and body pass through byte-identical;
  `Authorization` bearer and ticket query params pass through untouched —
  the relay cannot mint, refresh, or validate anything.
- Health: `GET /v1/device-gateway/relay/health` answered by the relay
  itself: `{ ok, v: 1, fp, backend_reachable }`. `backend_reachable` is a
  cached localhost probe (`GET /v1/device-gateway/health` on loopback,
  1s timeout, 5s cache). Phone uses this for route scoring, not liveness
  of user data.
- Live voice over relay: `POST .../live/open` goes through the relay like
  any gateway call (returns `ws_ticket` bound to backend session); the
  subsequent `WS /v1/voice/live?session_id=&ticket=` upgrade is proxied
  TCP-bidirectionally by the relay. Ticket stays single-use, short-lived;
  relay logs nothing about audio frames.
- Timeouts: relay adds none beyond backend's; phone's existing per-call
  timeouts (sync/bootstrap longer, inbox short) apply unchanged. Max body
  cap (suggestion 8 MiB, reject `413`) so a malicious LAN peer can't turn
  the Mac into an amplification proxy.

```
iPhone                          Mac relay                    backend (loopback)
  | POST /v1/device-gateway/session |                                |
  |---- Authorization: Bearer dev-->| ---- same bytes -------------> |
  |                                 | <--- same response ----------- |
  |<--- 200 {access_token} ---------|                                |
  | WS /v1/voice/live?...ticket=... |                                |
  |==== upgrade ===================>| ==== raw proxy ==============> |
```

## 4. Auth

Unchanged end-to-end (tickets design from `EvieGatewayAuth.swift`):

- Pair, session refresh, and live ticket issuance happen on the backend;
  relay sees opaque bearers/tickets and forwards them.
- Long-lived tokens never in URLs (existing rule); relay MUST NOT log
  `Authorization` headers or ticket query values.
- Relay health endpoint is unauthenticated by design (load-balancer style);
  it exposes only `{ok, v, fp, backend_reachable}` — no user data.
- `relay/info` (port/fp discovery over ts.net) requires the normal access
  token, so a random tailnet peer can't enumerate relay config.

## 5. Fallback decision table

Route evaluation runs on: app foreground, `NWPathMonitor` change (slice 5
driver), relay/health failure, or every N minutes (suggestion 5, only while
unlocked). Scoring is latency + reachability, not signal bars.

| # | Condition | Route | Rationale |
|---|-----------|-------|-----------|
| 1 | Relay health `ok` AND `backend_reachable=true` AND relay p95 < direct p95 − margin (sugg. 50 ms, 3 probes) | **Relay** | Measurably faster, backend confirmed |
| 2 | Relay reachable but `backend_reachable=false` | **Direct** | Mac up, backend down — relay adds nothing |
| 3 | Relay cert/fp mismatch | **Direct** + alert once | Possible spoof; never fall forward |
| 4 | Bonjour found nothing, tailnet down | **Direct** | No candidate; status quo |
| 5 | Relay probe times out (>2 s) or 5xx twice in a row | **Direct**, backoff relay probes (30 s → 5 min) | Flaky LAN shouldn't flap UX |
| 6 | Voice call active | **Stick** (no mid-call switch) | Re-ticketing mid-call drops audio |
| 7 | Sync bootstrap or outbox drain in flight | **Stick** until done | Cursor/outbox are per-connection safe but retries waste bytes |

Stickiness: once a route is chosen, keep it for the session unless rule 2/3/5
fires. Relay is never required: fresh install with no Mac nearby works
exactly like today.

## 6. Latency / energy rationale

- **Latency win:** on LAN, RTT is typically 2–10 ms vs 30–150 ms over
  DERP-relayed ts.net or WAN. Sync bootstrap (1 big pull) barely notices;
  interactive calls (inbox poll, queue append, live-open handshake) do.
  The 50 ms margin rule means relay engages only when the win is real.
- **Energy cost:** Bonjour browse + relay probes cost radio/CPU. Mitigations:
  browse only on Wi-Fi and only during route evaluation (not continuous);
  probes are 3 tiny health GETs; evaluation at most every 5 min while
  unlocked; all timers suspend in background (BGTaskScheduler drain in
  slice 4 covers background sync — relay never wakes the phone, pushes do).
- **Net:** relay pays for itself when the user is home (phone + Mac on same
  LAN/tailnet most of the day); away from home the phone converges to
  direct within one failed probe and stays there with backoff.

## 7. Staged rollout

1. **Stage 0 — doc only (this file).** No code. Agree on port, TXT keys,
   health shape, decision table.
2. **Stage 1 — Mac relay prototype.** Localhost forwarder for the two
   prefixes + health endpoint; manual test with `curl` from LAN.
3. **Stage 2 — iPhone route evaluation (foreground only).** Discovery +
   decision table + `fp` TOFU store; relay used only when rule 1 holds;
   stickiness rules 6–7. No background changes.
4. **Stage 3 — harden.** Backoff, fp rotation overlap, body cap, no-authlog
   audit, latency telemetry (relay vs direct p50/p95 counters only).
5. **Stage 4 — background interplay.** Confirm slice-4 push/BGTask drain
   ignores relay (background sync always direct — simpler, radio-cheap);
   relay remains a foreground optimization.

Non-goals: Mac-to-Mac mesh, relay auth of its own, caching/queueing in the
relay, any change to gateway ticket or sync-cursor semantics.
