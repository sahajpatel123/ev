# Integrations & Ecosystem

**Status: v1** — adapter framework, encrypted credential vault, scoped
permissions/revocation, webhook ingestion, and a permissioned plugin runtime.
See `docs/WORK_BREAKDOWN.md` §18 for the plan mapping.

## 1. Adapter framework

Every external system is an `Adapter` in `app/integrations/adapters.py`:

- `capabilities` — the scopes it can grant (`calendar:read`, `github:act`, …).
- `default_scopes` — the least-privilege defaults.
- `min_privacy` — the privacy floor for its live channel (health is
  `sensitive`).
- `event_types` — the webhook event types it can translate.
- `actions` — permissioned actions with the scope each one requires.
- `translate_webhook(payload, headers)` — deterministic payload -> live events.
- `act(...)` — local deterministic mode, or HTTP forwarding with the vault
  token when `config.provider == "http"` and `base_url` is set.

Built-in adapters: **calendar, health, github, smart_home, messaging, search**.
New adapters are registered in the `IntegrationRegistry`; provider logic stays
behind this interface so EV core never changes when an integration is added or
replaced. The `search` adapter is the permissioned web/research surface from
plan §11.3: `search.query` runs through a configured provider or a
deterministic local mode.

## 2. Encrypted credential vault

`app/integrations/vault.py` encrypts OAuth access/refresh tokens and webhook
secrets with Fernet. The key is `EV_VAULT_KEY` (fallback: deterministic
derivation from `EV_MASTER_KEY`). Only a token fingerprint is stored in plain
DB columns; API responses, access logs, and model context never contain token
material. Integration `config` is non-secret and rejects secret-looking keys.

## 3. Scopes, privacy, revocation

- Scopes must be a subset of the adapter's capabilities.
- A scope change revokes the existing OAuth credential (re-authorization
  required), preserving least privilege.
- Revocation (`DELETE /v1/integrations/{id}`) is immediate: status -> revoked,
  ciphertext + fingerprints wiped, live channel deactivated. All action and
  webhook gates fail closed. When `config.revoke_remote: true`, the adapter's
  provider-side revocation hook is also called (best effort; local revocation
  always proceeds).
- OAuth refresh (`POST /v1/integrations/{id}/credentials/refresh`) exchanges
  the vaulted refresh token through the adapter's refresh flow and re-encrypts
  the new token; no token material is logged.

## 4. Webhooks

`POST /v1/integrations/webhook/{id}`:

1. Verifies `X-EV-Signature: sha256=<hex>` over `X-EV-Timestamp.body`.
2. Rejects timestamps outside `EV_WEBHOOK_MAX_SKEW_SECONDS` (replay window).
3. Rate-limits per integration (`EV_WEBHOOK_RATE_LIMIT` /
   `EV_WEBHOOK_WINDOW_SECONDS`).
4. Treats `X-EV-Delivery-Id` as an idempotency key: a provider retry with the
   same delivery id returns the original result instead of duplicating events
   (unique `(integration, delivery_key)` ledger row).
5. Translates the payload with the adapter and ingests it into the
   integration's live channel through `app/ev/live.py` — immutable,
   idempotent (sha256 dedupe), fail-closed privacy, provenance preserved for
   EV Sense.

The webhook secret is created/rotated via
`POST /v1/integrations/{id}/webhook-secret` and returned exactly once.

## 5. Plugins

Plugins extend EV with custom skills/commands:

- `POST /v1/plugins` submits a manifest (`ev.plugin.v1`) declaring
  `permissions` (subset of `memory:read | live:read | live:emit`) and commands
  whose handlers are the body of `def run(args, context)`.
- `POST /v1/plugins/{id}/approve` (master key only) activates it; reject,
  disable, and enable are explicit lifecycle endpoints.
- Execution runs `python -I -S` in a subprocess with AST validation that
  rejects imports, dunder access, and dangerous builtins/calls, bounded by
  `EV_PLUGIN_TIMEOUT_SECONDS` and `EV_PLUGIN_MAX_OUTPUT_BYTES`.
- `live:emit` lets a command push validated events into a `plugin:{slug}` live
  channel; every run is access-logged.

## 6. Reinstall after revocation

Revoking an integration does not burn its slug: reinstalling with the same
slug revives the row with a fresh live channel and a clean slate (credentials
were already wiped, so re-authorization and a new webhook secret are required).
The old channel and its event history stay intact for the user.

## 7. Endpoint map

| Method | Path |
| --- | --- |
| GET | `/v1/integrations/catalog` |
| GET/POST | `/v1/integrations` |
| GET/PATCH/DELETE | `/v1/integrations/{id}` / `.../scopes` |
| POST/GET | `/v1/integrations/{id}/credentials` |
| POST | `/v1/integrations/{id}/credentials/refresh` |
| POST | `/v1/integrations/{id}/webhook-secret` |
| POST | `/v1/integrations/{id}/actions` |
| GET | `/v1/integrations/{id}/events` |
| POST | `/v1/integrations/webhook/{id}` (HMAC, no bearer) |
| GET/POST | `/v1/plugins` |
| GET/POST | `/v1/plugins/{id}` · `/approve` · `/reject` · `/enable` · `/disable` |
| POST | `/v1/plugins/{id}/commands/{command}` |

## 8. Tests

`backend/tests/test_integrations.py` covers adapter catalog/validation, vault
encryption and non-exposure, scope enforcement, scope-change credential
revocation, webhook HMAC/replay/rate-limit/privacy behavior, immediate
revocation, and plugin lifecycle/sandbox rules.
