# Integrations & Ecosystem

**Status: v2** — adapter framework, encrypted credential vault, scoped
permissions/revocation, webhook ingestion, a permissioned plugin runtime, and
**real provider integrations**: Google Calendar (authorization-code + PKCE,
refresh-token rotation, read-only sync) and GitHub (OAuth read paths against
`api.github.com`). See `docs/WORK_BREAKDOWN.md` §18 for the plan mapping.

## 1. Why Google Calendar (and why not EventKit / Microsoft Graph)

The mission asks for a genuine authorization-code + PKCE flow with refresh-token
rotation and vault storage. Google Calendar is the chosen provider because:

- **PKCE + refresh rotation are real**: Google issues long-lived refresh tokens
  (`access_type=offline&prompt=consent`) and rotates them on every refresh, so
  "refresh-token rotation proven" is a testable, honest property.
- **Server-side read of ≥ 7 days**: the backend pulls `calendar/v3` events
  directly; no local Swift helper or TCC permission is needed.
- **Testable offline**: the real code path (authorize → callback → sync →
  refresh → revoke) runs against a mock Google provider in CI without network
  or a registered client.
- **Least privilege**: the only granted scope is
  `https://www.googleapis.com/auth/calendar.readonly` — no write scope.

macOS EventKit via a Swift helper was seriously considered (no OAuth app
registration, no cloud round trip, no secrets to leak), but it cannot satisfy
the refresh-rotation requirement (there are no tokens to rotate), adds a TCC
consent ceremony and a helper binary to maintain, and cannot be exercised in
offline CI. Microsoft Graph was rejected for the same OAuth-registration
overhead with no advantage over Google for a personal calendar. If the human
prefers a cloud-free path later, EventKit can be added as a second adapter
without touching the vault or webhook layers.

## 2. Adapter framework

Every external system is an `Adapter` in `app/integrations/adapters.py`:

- `capabilities` — the scopes it can grant (`calendar:read`, `github:act`, …).
- `default_scopes` — the least-privilege defaults.
- `min_privacy` — the privacy floor for its live channel (health is
  `sensitive`).
- `event_types` — the webhook event types it can translate.
- `actions` — permissioned actions with the scope each one requires.
- `translate_webhook(payload, headers)` — deterministic payload -> live events.
- `act(...)` — local deterministic double, generic HTTP passthrough, or a
  **real provider call** (Google Calendar / GitHub).
- `sync(...)` — provider pull into normalized live events + derived signals.
- `refresh_token(...)` / `revoke_remote(...)` — real provider OAuth refresh and
  revocation for provider-backed adapters.

Built-in adapters: **calendar, health, github, smart_home, messaging, search**.
New adapters are registered in the `IntegrationRegistry`; provider logic stays
behind this interface so EV core never changes when an integration is added or
replaced.

### 2.1 Provider modes

`config.provider` selects the runtime behavior:

| Value | Behavior |
| --- | --- |
| *(unset)* / `local` | Deterministic offline double: `{"ok": true, "mode": "local"}`. No network, no secrets. |
| `http` | Legacy generic passthrough to `{base_url}/actions/{action}` (tests/dev shims). |
| `google` | Real Google Calendar OAuth + `calendar/v3` reads (calendar adapter). |
| `github` | Real GitHub OAuth + `api.github.com` reads/writes (github adapter). |

## 3. Encrypted credential vault

`app/integrations/vault.py` encrypts OAuth access/refresh tokens, webhook
secrets, and transient PKCE state with Fernet. The key is `EV_VAULT_KEY`, which
is **required** (min 16 chars) and never derived from `EV_MASTER_KEY`; the app
fails closed at startup when it is missing. Only a token fingerprint is stored
in plain DB columns; API responses, access logs, and model context never
contain token material. Integration `config` is non-secret and rejects
secret-looking keys (client ids/secrets belong in environment variables, see
§5.2).

## 4. Real OAuth calendar (Google) — the human clicks this once

### 4.1 One-time setup in Google Cloud Console

1. Go to <https://console.cloud.google.com/apis/credentials> (or the project's
   **APIs & Services → Credentials** page).
2. Click **Create Credentials → OAuth client ID**.
3. Application type: **Web application**.
4. Authorized redirect URI: the exact value you will set as
   `EV_GOOGLE_OAUTH_REDIRECT_URI`. The default is
   `http://127.0.0.1:8765/v1/integrations/oauth/callback` — use the same string
   in both places. There is **no per-integration path**; the state parameter
   binds the callback to the integration.
5. Copy the **Client ID** and **Client secret**.
6. Enable the **Google Calendar API** for the project. For Gmail envelope
   ingest, also enable the **Gmail API**. Mail uses a separate OAuth consent
   (`gmail.readonly`) with the same client id/secret; those scopes are **not**
   added to the calendar provider, so an existing calendar grant is not forced
   to re-consent.

No calendar-write scope is requested: only `calendar.readonly` (plus `openid`
and `email` for account identification). Gmail list is metadata-only
(Subject/From/Date); send stays on the headless Mail.app helper.

### 4.2 Where the secret goes

Put the client id/secret/redirect into the server environment (never into
integration config, never into `docs`):

```bash
export EV_GOOGLE_OAUTH_CLIENT_ID=...apps.googleusercontent.com
export EV_GOOGLE_OAUTH_CLIENT_SECRET=GOCSPX-...
export EV_GOOGLE_OAUTH_REDIRECT_URI=http://127.0.0.1:8765/v1/integrations/oauth/callback
```

These are **not** stored in the vault: they are EV's client credentials, while
the vault holds the human's access/refresh tokens. `.env.example` documents
them; empty values are the offline CI double and authorize fails closed with a
clear "OAuth provider is not configured" message.

### 4.3 The one-click flow

```bash
# 1. Install the calendar adapter (read-only).
curl -X POST http://127.0.0.1:8000/v1/integrations \
  -H "Authorization: Bearer $EV_MASTER_KEY" \
  -H "Content-Type: application/json" \
  -d '{"adapter":"calendar","name":"Google Calendar","scopes":["calendar:read"],"config":{"provider":"google"}}'

# 2. Start authorization. The backend generates PKCE state + code_verifier
#    (vault-encrypted) and returns a Google consent URL.
curl "http://127.0.0.1:8000/v1/integrations/oauth/authorize?integration_id=<id>" \
  -H "Authorization: Bearer $EV_MASTER_KEY"

# 3. Open the returned authorize_url, approve, and get redirected back to
#    /v1/integrations/oauth/callback. The backend exchanges the code (PKCE),
#    stores access + refresh tokens in the vault, and deletes the state.

# 4. Check status and pull 7+ days of real events.
curl http://127.0.0.1:8000/v1/integrations/<id>/oauth/status \
  -H "Authorization: Bearer $EV_MASTER_KEY"
curl -X POST http://127.0.0.1:8000/v1/integrations/<id>/sync \
  -H "Authorization: Bearer $EV_MASTER_KEY"
```

`POST /v1/integrations/{id}/sync` pulls the calendar (default
`EV_CALENDAR_SYNC_DAYS` = 14; override with `?days=N`, 1..90) into the
integration's live channel as `calendar.event.updated`
events, deduplicated by provider `event_id`, and returns derived signals.
`GET /v1/integrations/{id}/calendar/signals` re-derives the same signals from
stored live events with no provider round trip:

- `next_event` — summary, start/end, location, participants, meeting links.
- `leave_by` — start minus 30 minutes (estimated) for future events; the end
  time for an event already in progress.
- `today` / `day_density` — per-day event count and busy minutes over the sync
  horizon (free/transparent blocks are excluded from density).
- `deadline_proximity` — 0..1 over a 48-hour decay window before the next event.
- `quiet_hours` — the current quiet-hours truth from `EV_QUIET_HOURS_*`.
- `participants` — normalized attendee list (name/email/event count) for
  entity linking by the memory layer.

### 4.4 Expiry, refresh rotation, and re-authorization

- Every action/sync automatically refreshes when the stored access token is
  expired, and retries once after a provider 401. The refresh response's new
  refresh token **replaces** the old one in the vault (rotation).
- If the provider rejects the refresh (`invalid_grant`), the credential is
  marked `reauth_required` and the API returns **401** with a clean prompt:
  run `GET /v1/integrations/oauth/authorize?integration_id=<id>` again. No
  token material ever appears in the error.
- `GET /v1/integrations/{id}/oauth/status` reports `authorized`, `expired`,
  and `reauth_required` so the morning brief/card can show the prompt.
- `DELETE /v1/integrations/{id}` revokes the Google token at the provider
  (when `config.revoke_remote` is set), wipes vault ciphertext, and
  deactivates the live channel immediately.

## 5. GitHub

### 5.1 Setup (GitHub App web flow, expiring user tokens)

1. Register a **GitHub App** (or OAuth app) at
   <https://github.com/settings/apps>. Webhook is optional (EV's webhook
   ingress is provider-agnostic); enable **Expire user authorization tokens**
   so refresh tokens are issued.
2. Callback URL: set `EV_GITHUB_OAUTH_REDIRECT_URI` to the same
   `/v1/integrations/oauth/callback` URL used for Google.
3. Copy **Client ID** and **Client secret** into
   `EV_GITHUB_OAUTH_CLIENT_ID` / `EV_GITHUB_OAUTH_CLIENT_SECRET`.
4. The default OAuth scope is `repo` (needed for private-repo issues/PR reads).
   **Confirm this scope with the human**; a finer-grained alternative is to
   paste a fine-grained PAT via
   `POST /v1/integrations/{id}/credentials` (manual flow, no refresh/revoke).

### 5.2 Real read paths

With `config.provider: "github"`, `act` calls `api.github.com` directly:

- `github.list_issues` → `GET /repos/{owner}/{repo}/issues` (open, updated
  first, capped at 100/page).
- `github.comment_pr` → `POST /repos/{owner}/{repo}/issues/{number}/comments`.

The existing webhook parsing (CI failure only on `completed` + `failure`,
issue/PR events) is unchanged and still guards HMAC, replay, rate limit, and
idempotency.

## 6. Health (HealthKit) — payload contract for Agent 18

Health data is server-side ingested through the existing signed webhook
ingress and stored in the integration's live channel with a `sensitive`
privacy floor (enforced by the adapter and by live-event escalation). It is
**never sent to a model without explicit permission** — model-facing live
slices exclude `sensitive` and `never_send_to_model` events.

Agent 18 (SUIT, HealthKit bridge) should POST to
`/v1/integrations/webhook/{health_integration_id}` with:

```http
X-EV-Signature: sha256=<hex hmac-sha256 over "<timestamp>.<json body>">
X-EV-Timestamp: <unix seconds>
Content-Type: application/json
```

Body (batch — preferred for HealthKit's periodic multi-metric delivery):

```json
{
  "metrics": {
    "heart_rate": 72,
    "hrv": 54.2,
    "sleep_hours": 7.5,
    "steps": 8123,
    "readiness": 0.82
  },
  "units": {
    "heart_rate": "bpm",
    "hrv": "ms",
    "sleep_hours": "h",
    "steps": "count",
    "readiness": "0..1"
  }
}
```

Single-metric bodies (`{"metric": "steps", "value": 8123, "unit": "count"}`)
remain supported. The allowlist is **strict**: only
`heart_rate | hrv | sleep_hours | steps | readiness`; values must be real
numbers (`bool` is rejected); unknown metrics are never stored. The webhook
secret is created/rotated via `POST /v1/integrations/{id}/webhook-secret` and
returned exactly once.

## 7. Scopes, privacy, revocation

- Scopes must be a subset of the adapter's capabilities.
- A scope change revokes the existing OAuth credential (re-authorization
  required), preserving least privilege.
- Revocation (`DELETE /v1/integrations/{id}`) is immediate: status -> revoked,
  ciphertext + fingerprints wiped, live channel deactivated. All action and
  webhook gates fail closed. When `config.revoke_remote: true`, the adapter's
  provider-side revocation hook is also called (best effort; local revocation
  always proceeds).
- OAuth refresh (`POST /v1/integrations/{id}/credentials/refresh` or automatic
  during action/sync) exchanges the vaulted refresh token through the real
  provider and re-encrypts the new token; no token material is logged.
- Vault key rotation (`POST /v1/integrations/vault/rotate`, master key only)
  re-encrypts every credential under a new `EV_VAULT_KEY` in place.

## 8. Webhooks

`POST /v1/integrations/webhook/{id}`:

1. Verifies `X-EV-Signature: sha256=<hex>` over `X-EV-Timestamp.body`.
2. Rejects timestamps outside `EV_WEBHOOK_MAX_SKEW_SECONDS` (replay window).
3. Rate-limits per integration (`EV_WEBHOOK_RATE_LIMIT` /
   `EV_WEBHOOK_WINDOW_SECONDS`).
4. Caps payload size at `EV_WEBHOOK_MAX_BODY_BYTES` (default 1 MiB).
5. Treats `X-EV-Delivery-Id` as an idempotency key (or content fingerprint
   when absent).
6. Translates the payload with the adapter and ingests it into the
   integration's live channel through `app/ev/live.py` — immutable,
   idempotent, fail-closed privacy, provenance preserved.

## 9. Secrets discipline (proven by test)

`tests/test_oauth_calendar.py` runs a full real-mode lifecycle against a mock
provider (authorize → PKCE callback → sync → action → refresh rotation →
reauth failure → revoke) and then **greps every log table** — `access_log`,
`model_calls`, `webhook_deliveries`, and `live_events` payloads — plus API
error bodies, asserting none contain access tokens, refresh tokens, or client
secrets. Vault ciphertext is additionally asserted to never contain plaintext
tokens. This is the acceptance proof for "0 secrets found after a full sync".

## 10. Plugins

Plugins extend EV with custom skills/commands:

- `POST /v1/plugins` submits a manifest (`ev.plugin.v1`) declaring
  `permissions` (subset of `memory:read | live:read | live:emit`) and commands
  whose handlers are the body of `def run(args, context)`.
- `POST /v1/plugins/{id}/approve` (master key only) activates it; reject,
  disable, and enable are explicit lifecycle endpoints.
- Execution runs `python -I -S` in a subprocess with AST validation that
  rejects imports, dunder access, and dangerous builtins/calls, bounded by
  `EV_PLUGIN_TIMEOUT_SECONDS` and `EV_PLUGIN_MAX_OUTPUT_BYTES`.

## 11. Reinstall after revocation

Revoking an integration does not burn its slug: reinstalling with the same
slug revives the row with a fresh live channel and a clean slate (credentials
were already wiped, so re-authorization and a new webhook secret are required).

## 12. Endpoint map

| Method | Path |
| --- | --- |
| GET | `/v1/integrations/catalog` |
| GET/POST | `/v1/integrations` |
| GET/PATCH/DELETE | `/v1/integrations/{id}` / `.../scopes` |
| POST/GET | `/v1/integrations/{id}/credentials` |
| POST | `/v1/integrations/{id}/credentials/refresh` |
| GET | `/v1/integrations/oauth/authorize?integration_id={id}` |
| GET/POST | `/v1/integrations/oauth/callback?code=…&state=…` |
| GET | `/v1/integrations/{id}/oauth/status` |
| POST | `/v1/integrations/{id}/sync?days=N` (default `EV_CALENDAR_SYNC_DAYS`) |
| GET | `/v1/integrations/{id}/calendar/signals` |
| GET | `/v1/integrations/{id}/life/policy?action=…&recipient=…` |
| GET | `/v1/integrations/{id}/life/outbox` (device-authenticated poll) |
| POST | `/v1/integrations/{id}/life/device-results` (device-authenticated) |
| POST | `/v1/integrations/vault/rotate` (master key only) |
| POST | `/v1/integrations/{id}/webhook-secret` |
| POST | `/v1/integrations/{id}/actions` |
| GET | `/v1/integrations/{id}/events` |
| POST | `/v1/integrations/webhook/{id}` (HMAC, no bearer) |
| GET/POST | `/v1/plugins` |
| GET/POST | `/v1/plugins/{id}` · `/approve` · `/reject` · `/enable` · `/disable` |
| POST | `/v1/plugins/{id}/commands/{command}` |

## 13. Tests

```bash
cd backend
uv run pytest tests/test_integrations.py tests/test_oauth_calendar.py -q
uv run ruff check app clients tests
uv run mypy app clients
```

`tests/test_integrations.py` covers adapter catalog/validation, vault
encryption and non-exposure, scope enforcement, scope-change credential
revocation, webhook HMAC/replay/rate-limit/privacy behavior, immediate
revocation, and plugin lifecycle/sandbox rules. `tests/test_oauth_calendar.py`
covers the full Google OAuth lifecycle (PKCE, vault, sync idempotency,
signals, refresh rotation, clean re-auth prompt, provider revoke, health batch
allowlist, secret-leak grep) and GitHub real read paths plus OAuth refresh
rotation and provider revocation.

## 14. Dependency notes for other agents

- **Agent 17 (WORKBENCH, surface UI)**: `ev card` / morning brief can now read
  real density from `GET /v1/integrations/{id}/calendar/signals` and
  `GET /v1/integrations/{id}/oauth/status` (show a "re-authorize" action when
  `reauth_required`). No new model call is needed; signals are already
  compact and token-bounded. Exact wiring points: `app/ev/hud.py::status_card`
  (append a calendar-density line from `signals["today"]` /
  `signals["next_event"]`) and `app/api/companion.py::hud_card` (fetch the
  signals for the configured calendar integration before assembling the card).
- **Agent 18 (SUIT, HealthKit bridge)**: use the payload contract in §6
  (signed webhook, batch `metrics` map, strict allowlist). The health webhook
  secret comes from `POST /v1/integrations/{health_id}/webhook-secret`.
- **Agent 7 (ROSTER)**: calendar `participants` (name/email) are emitted in
  signals and event payloads for entity linking; attendees are the natural
  first-class "people" source.
- **Agent 20 (LAUNCH, daemon/scheduler)**: schedule
  `POST /v1/integrations/{calendar_id}/sync` (master/reverified actor) at least
  daily and before morning-brief time so `calendar/signals` always reflects
  current density. The endpoint is idempotent (provider `event_id` dedupe), so
  an extra sync is harmless.
- **Conductor**: new endpoints are additive only; no existing operation or
  response schema was changed.

## 15. WAVE LIFE — Apple life bridges (messaging / contacts / phone / mail)

**Status: v3 (WAVE LIFE)** — real provider modes for Messages, Contacts,
Phone, FaceTime, and Mail are implemented behind `provider=macos_life`
(EVLifeHelper) and `provider=device_proxy` (iPhone actuator queue). The Swift
helper is implemented by Agent 18 at `macos/Sources/EVLifeHelper/main.swift`;
this backend is aligned to its real contract (§15.2).

### 15.1 Owner setup (once SUIT ships the helper)

1. Build/install the EVLifeHelper binary (Agent 18): `cd macos && swift build
   -c release` ships `.build/release/EVLifeHelper` (also inside EV.app as
   `Contents/MacOS/EVLifeHelper`).
2. Grant TCC permissions in **System Settings → Privacy & Security**:
   **Contacts**, **Full Disk Access** (Messages DB), **FaceTime/Call
   History**, and **Automation** as needed. EVLifeHelper exits **3** on
   permission denial; EV surfaces that as a loud 403 with the exact setting
   to open — never as success.
3. Set the helper path and provider:

   ```bash
   export EV_LIFE_HELPER_PATH=/Applications/EV.app/Contents/MacOS/EVLifeHelper
   export EV_MESSAGING_PROVIDER=macos_life
   ```

4. Install the adapters (scopes are the standing authority):

   ```bash
   curl -X POST http://127.0.0.1:8000/v1/integrations \
     -H "Authorization: Bearer $EV_MASTER_KEY" -H "Content-Type: application/json" \
     -d '{"adapter":"messaging","name":"Messages","scopes":["messaging:read","messaging:act"],"config":{"provider":"macos_life"}}'
   curl -X POST http://127.0.0.1:8000/v1/integrations \
     -H "Authorization: Bearer $EV_MASTER_KEY" -H "Content-Type: application/json" \
     -d '{"adapter":"contacts","name":"Contacts","scopes":["contacts:read"],"config":{"provider":"macos_life"}}'
   curl -X POST http://127.0.0.1:8000/v1/integrations \
     -H "Authorization: Bearer $EV_MASTER_KEY" -H "Content-Type: application/json" \
     -d '{"adapter":"phone","name":"Phone","scopes":["phone:act"],"config":{"provider":"macos_life","contact_allowlist":"starred"}}'
   ```

5. Say **"EVIE, text Mom I'm late"**. The runtime action dispatches to
   `POST /v1/integrations/{messaging_id}/actions` with `messaging.send`; EV
   executes EVLifeHelper and only reports success when the helper returns
   delivery evidence (`message_id` + `sent_at`). The proof of send is that
   evidence in the action result.

### 15.2 EVLifeHelper contract v1 (implemented by Agent 18, backend-aligned)

CLI: `EVLifeHelper <command> [--flag value ...]` — flag-style arguments,
exactly one JSON object on stdout. stderr is human diagnostics only.

Commands (the backend maps its actions onto these): `contacts.list`,
`contacts.resolve --query`, `messages.list [--limit N]`,
`messages.send --to --text`, `mail.list [--limit N]`,
`mail.send --to --subject --body`, `call.place --destination
[--kind tel|facetime]`, `call.check`, `apps.frontmost`, `apps.activate`.
Adapter mapping: `phone.call` → `call.place --kind tel`, `facetime.call` →
`call.place --kind facetime`.

Success envelope:

```json
{
  "ok": true,
  "data": {"to": "Mom", "sent": true}
}
```

Delivery evidence: `messages.send` / `mail.send` must set `data.sent == true`;
`call.place` must set `data.opened == true`. `data.dry_run == true` is
**not** delivery. The backend wraps the confirmation as
`delivery: {"confirmed": true, "evidence": {"provider": "EVLifeHelper",
"confirmed_by": "sent|opened", ...}}`; without it the operation fails
(`missing_delivery_evidence`) — never a silent success.

Error envelope:

```json
{
  "ok": false,
  "error": {
    "code": "permission_denied|not_available|bad_arguments|failed",
    "message": "human-readable"
  }
}
```

Exit codes: `0` ok · `1` generic failure · `3` **permission denied** ·
`4` not available · `5` bad arguments.

- **Permission denied is never success**: exit 3 (or
  `error.code=permission_denied`) maps to HTTP 403 with an actionable TCC
  message; the action result never contains `ok: true`.
- Timeouts (backend kills after `EV_LIFE_HELPER_TIMEOUT_SECONDS`) and
  oversized stdout are loud failures.

Real-provider evidence (2026-08-12, this host, built binary
`macos/.build/arm64-apple-macosx/release/EVLifeHelper`):

- `messages.list --limit 2` returned real Messages DB rows (handle, text,
  ISO date) through the backend runner — real data, not a double.
- `contacts.resolve` without the Contacts TCC grant produced exit 3, which the
  backend surfaced as a loud permission-denied failure.
- `messages.send` surfaced the helper's current AppleScript error
  (`Expected class name but found identifier.`) as a loud 502 — no fake
  success. **DEPENDENCY NOTE (Agent 18)**: fix the `buddy` targeting in
  `macos/Sources/EVLifeHelper/main.swift` `messages.send`; the backend side is
  correct and needs no change.

### 15.3 Standing owner authority

Granting `messaging:act` / `phone:act` / `contacts:read` is the standing
authority. Once granted, known contacts are pre-authorized per
`EV_LIFE_CONTACT_ALLOWLIST`:

- `all` — every known (resolved) contact is pre-authorized.
- `starred` — only starred contacts are pre-authorized.
- `any` — every recipient is pre-authorized.

Recipients outside the allowlist require `confirm: true` on the action (or
runtime approval) when `EV_LIFE_CONFIRM_UNKNOWN=true`; otherwise they are
denied. `EV_LIFE_AUTONOMY=full` is the owner's explicit opt-out of per-action
confirmation inside granted scopes. Per-integration `config` overrides:
`contact_allowlist`, `autonomy`, `confirm_unknown`, `helper_path`.
`GET /v1/integrations/{id}/life/policy?action=…&recipient=…` returns the
decision without executing anything.

### 15.4 iPhone device proxy (`provider=device_proxy`)

API-ready even though SUIT's iOS app is unbuilt:

- Outbound: install the `device_proxy` adapter; `messaging.send` / `phone.call`
  / `facetime.call` through `POST /v1/integrations/{id}/actions` create a
  `life_outbound_actions` row with `status=queued` and return
  `{"queued": true, "delivery": {"confirmed": false, "status": "queued"}}`.
  Nothing is reported delivered without evidence.
- Poll: a registered iPhone calls
  `GET /v1/integrations/{id}/life/outbox` with its device token and receives
  its queued actions (unassigned rows are visible to any registered device of
  that integration).
- Results: `POST /v1/integrations/{id}/life/device-results` with
  `{"queue_id": "…", "status": "delivered", "evidence": {"message_id": "…",
  "sent_at": "…"}}` marks the row delivered (evidence required; otherwise
  502 `missing_delivery_evidence`). `failed` marks it failed. A device can
  also post inbound `{"message": {...}}` payloads, which are ingested into the
  integration's live channel as `message.received`.

### 15.5 Runtime / notify wiring (dependency note)

`send_message` must reach the real backend path when
`EV_MESSAGING_PROVIDER=macos_life`:

- **Agent 14 (PULSE)**: in `app/notify/service.py::dispatch_action`, when
  `action.action_type == "send_message"` and
  `settings.messaging_provider == "macos_life"` (or `"device_proxy"`), call
  `POST /v1/integrations/{messaging_id}/actions` with `messaging.send` and
  `{"to", "text"}` instead of converting the message into a notification.
  Keep the notification ledger row only as the action receipt.
- **Agent 10 (CORTEX)**: `app/ev/actions.py` `send_message` may keep
  `requires_approval: true` for unknown recipients; the integrations
  `life/policy` endpoint provides the standing-authority decision so the
  approval flow can skip known contacts.

### 15.6 Verification

```bash
cd backend
uv run pytest tests/test_life_bridges.py tests/test_integrations.py tests/test_oauth_calendar.py -q
uv run ruff check app clients tests
uv run mypy app clients
```

Offline tests cover: mock-helper JSON parsing, exit-code → error mapping,
permission-denied loudness, missing-delivery-evidence rejection, no local
fallback when `macos_life` is configured, standing-authority matrix, and the
device-proxy queue/outbox/evidence lifecycle. The real helper path is tested
when `EV_LIFE_HELPER_PATH` is configured (skipped otherwise).
