# EV — Security & Privacy Plan ("Secret Identity")

**Version 1.0** — threat model, trust boundaries, auth, encryption, privacy levels,
deletion, backup, and the model boundary contract.

## 1. Trust boundaries

```text
┌──────────── User devices ────────────┐
│ iOS · Watch · Mac · web · CLI        │  (trusted, authenticated)
└───────────────┬──────────────────────┘
                │ TLS (Tailscale / LAN / Caddy)
┌───────────────▼──────────────────────┐
│ EV home stack (Mac, Docker)          │  (trusted host)
│  API · Postgres · Redis · MinIO      │
└───────────────┬──────────────────────┘
                │ HTTPS, only permitted context
┌───────────────▼──────────────────────┐
│ Model providers (DeepSeek, embeds)   │  (UNTRUSTED for storage)
└──────────────────────────────────────┘
```

**Core rule:** model providers are treated as untrusted. Only assembled, permitted
context leaves the machine; raw memory never does.

## 2. Assets & threats

| Asset | Threats | Primary mitigations |
| --- | --- | --- |
| Raw events + memories | Theft, tampering, loss | At-rest encryption, SHA-256 integrity, encrypted backups, local-first |
| Health data | Exposure, unintended model transmission | `sensitive`/`never_send_to_model` levels, on-device flags, export/delete |
| Model context | Prompt leakage | Retrieval boundary filter + boundary tests |
| Master key / device tokens | Credential theft | Random key, keychain storage, device registration, revocation |
| Blobs (images/files) | Theft | Object-store encryption, access-gated download |
| Backups | Loss/theft | Encrypted, restore drill, off-machine copy via user's own storage |

## 3. Authentication & devices

- Single-user master key (`EV_MASTER_KEY`): required on every API call
  (`Authorization: Bearer …`), constant-time comparison.
- Device registration: first pairing exchanges the master key for a device token
  (stored hashed in `devices.token_hash`); revocation is immediate.
- Every request logs `actor`, endpoint, resource, and request id in `access_log`.

## 4. Encryption

- **In transit:** TLS (Caddy/nginx termination) or Tailscale encrypted mesh; no
  plaintext API traffic outside localhost.
- **At rest:** PostgreSQL + MinIO volumes on encrypted disk (macOS FileVault on the
  host); application-level encryption for backups and sensitive JSONB fields where
  feasible.
- **Keys:** master key in host keychain/env; backup encryption key user-held;
  key rotation procedure documented.

## 5. Privacy levels & enforcement points

`private | normal | sensitive | never_send_to_model`

| Enforcement point | Behavior |
| --- | --- |
| Ingestion | Level stored on event + propagated to derived memories |
| Storage | Level indexed; redaction cascade on tombstone |
| Retrieval | `access="model"` excludes `never_send_to_model` in SQL; `sensitive` is excluded unless explicitly opted in per item |
| Prompt assembly | Boundary test asserts absence; context builder is the only gateway to the model |
| Export | All levels included (user owns the data); export is authenticated |
| Logs | Access log stores ids, not content |

**Enforced in code:** retrieval, conversation history, the model-safe rollup,
user state, and the live-data slice all exclude `never_send_to_model` (and
`sensitive` without opt-in) before anything reaches the provider. The gateway
additionally runs a deterministic payload guard on every call: forbidden
markers block the call before the provider is invoked, and credential-like
content is redacted at the boundary.

## 6. Deletion & redaction

- `DELETE /v1/events/{id}` is a **tombstone**: `tombstoned_at` + reason set; row
  preserved for audit.
- All memories derived from the tombstoned event are marked `redacted`; they are
  excluded from retrieval and prompts.
- `POST /v1/export` provides the full bundle before deletion (portability):
  events, memories, entities, relationships, conflicts, attachment metadata,
  registered devices, and the access log.
- Blobs referenced by a tombstoned event are scheduled for deletion after the audit
  window (configurable via `EV_TOMBSTONE_BLOB_RETENTION_DAYS`, default 30 days);
  `POST /v1/maintenance/purge-tombstoned-blobs` performs the purge while keeping
  the tombstoned event row for audit.

## 7. Backup & restore

- Daily encrypted snapshot: Postgres dump + object-store mirror + config.
- Snapshot manifest with checksums; retention policy (e.g., 7 daily, 4 weekly,
  12 monthly).
- Restore drill (M4 acceptance): wipe → restore → verify event count, memory count,
  and a sample audit trail.
- Backup destination is user-controlled (external disk, Tailscale NAS, user's own
  S3) — never a third-party default.

**Implemented (v1):** `POST /v1/backup` writes an authenticated-encrypted
`ev.backup.v1` bundle (scrypt-derived Fernet key from a user-held passphrase,
independent of `EV_MASTER_KEY`) containing the immutable event log, attachment
metadata, registered devices, the access log, and the derived-memory snapshot.
`POST /v1/backup/verify` decrypts and checksum-verifies a bundle without
touching the database. `POST /v1/backup/restore` supports `merge` (deduplicated
event-sourced restore + rebuild) and `wipe` (confirmed destructive drill that
clears the personal-data layer, restores the bundle, and regenerates derived
memory). All three endpoints require the master key. Tests cover the full
wipe→restore→verify drill, tamper detection, wrong-passphrase rejection, and
device-token denial. `POST /v1/maintenance/prune-backups` enforces retention
(keep newest N, default 7), and `python -m app.scripts.backup_snapshot` creates
an encrypted snapshot with the same passphrase rules for cron scheduling.

## 8. Model boundary contract (tested)

1. `Retriever.search(access="model")` excludes `never_send_to_model` rows.
2. `sensitive` rows are excluded unless explicitly opted in per item.
3. `Orchestrator.build_context()` accepts only retrieved items; no code path reads
   raw events into the prompt.
4. Tests instrument the exact payload sent to the chat provider and assert:
   - No `never_send_to_model` content (by id and text).
   - Assembled context ≤ budget.
   - Tool results bounded and terminating.
5. The gateway payload guard blocks any provider-bound message or envelope
   string carrying a `never_send_to_model` marker and redacts credentials
   before the provider call; blocked calls are recorded as `blocked`, never
   sent.

## 8a. Privilege separation

- Device tokens authenticate a registered device and may read/write ordinary
  data, but **cannot** create or revoke devices, list devices, or export the
  full data bundle. Those surfaces require the master key (`require_master`).
- Biometric voice deletion/export follow the same master-key ownership rule.

## 9. App security

- iOS: tokens in Keychain, biometric-gated unlock option, no plaintext cache of
  sensitive memories; screenshots blocked for sensitive screens (configurable).
- Watch: minimal caching; quick-capture payloads encrypted at rest.
- Web: no third-party scripts; CSP headers; session token in HttpOnly cookie or
  memory (not localStorage for master key).

## 10. Operational hygiene

- Auto-updates with signed images; supply-chain pinning (lockfiles, image digests).
- Dependency scanning in CI; secrets never in git (`.env` ignored).
- Monitoring: API/worker logs, error rates, gateway latency; "EV checkup" endpoint.
- Single-user incident plan: revoke devices → rotate keys → restore from backup →
  audit access log for anomalies.

## 11. What we deliberately do NOT do

- No OS-level ambient capture without explicit per-source permission.
- No cloud SaaS storage of raw memory.
- No marketing/telemetry of personal data.
- No covert autonomy: any proactive action is inspectable and reversible.

## 12. Data flows & retention

| Data class | Collection | Storage | Default retention | Deletion |
| --- | --- | --- | --- | --- |
| Events | Capture APIs | Postgres `events` | Indefinite (immutable) | Tombstone; row retained for audit |
| Memories | Extraction pipeline | Postgres `memories` | Indefinite (versioned) | Redacted when source tombstoned |
| Health snapshots | HealthKit import | Postgres `health_snapshots` | 24 months, then summarized | Immediate on revoke/delete |
| Gear telemetry | Client telemetry | Postgres `gear_telemetry` | 90 days | Immediate on revoke/delete |
| Live events | Permissioned collectors | Postgres `live_events` | `EV_LIVE_EVENT_RETENTION_DAYS` (default 90) | Retention job (`POST /v1/live/retention`); only consumed events past the window, latest + provenance-linked events always kept |
| Alerts | Alert radar | Postgres `alerts` | 12 months | Immediate on delete |
| Research sources | Research assistant | Postgres + blobs | Indefinite | Tombstone + blob schedule |
| Projects/BOM/prints | Maker flows | Postgres | Indefinite | User delete |
| Blobs (attachments) | Uploads | MinIO/S3 | Indefinite | 30-day audit window after tombstone |
| Access log | Every action | Postgres `access_log` | 12 months | User-triggered wipe |
| Backups | Nightly job | User storage (encrypted) | 7 daily / 4 weekly / 12 monthly | Rotation |

All retention is configurable via `Settings`; export runs before any destructive
retention job.

## 13. Integrations & ecosystem security

- **Adapter isolation:** every integration is its own permissioned unit: scopes,
  privacy level, bound live channel, credentials, webhook secret, and audit
  trail. Scopes must be a non-empty subset of the adapter's declared
  capabilities; unknown or admin scopes are rejected at install.
- **Credential vault:** OAuth access/refresh tokens and webhook secrets are
  Fernet-encrypted at rest (`EV_VAULT_KEY`, or derived from the master key when
  unset). Only a SHA-256 fingerprint is stored for verification. Plaintext
  exists only in memory during an action/webhook call; integration config
  rejects secret-looking keys so credentials cannot be smuggled into non-secret
  fields, logs, prompts, or model context.
- **Immediate revocation:** `DELETE /v1/integrations/{id}` flips status to
  revoked, wipes credential ciphertext and fingerprints, and deactivates the
  bound live channel. Every gate (actions, webhooks, credentials) fails closed
  afterward; previously granted tokens are discarded at the vault, and
  provider-issued tokens should additionally be revoked at the provider. When
  `config.revoke_remote: true`, the adapter calls the provider revocation
  endpoint with the vault token (best effort; the local wipe always proceeds
  and the outcome is access-logged).
- **OAuth refresh:** refresh tokens are vault-encrypted; `POST
  /v1/integrations/{id}/credentials/refresh` exchanges them through the
  adapter's refresh flow, re-encrypts the new access token, and logs only
  metadata (never token material).
- **Webhook integrity:** ingress requires `X-EV-Signature: sha256=<hex>` over
  `X-EV-Timestamp.body`, rejects timestamps outside the skew window, and
  rate-limits per integration, with a hard body-size cap
  (`EV_WEBHOOK_MAX_BODY_BYTES`). Verified payloads enter the immutable
  live-event pipeline with idempotent dedupe and fail-closed privacy.
  `X-EV-Delivery-Id` is a database-level idempotency key so provider retries
  return the original result instead of creating duplicate events.
- **Plugin sandbox:** plugins are inert until a master-key approval; commands
  run in an isolated subprocess (`python -I -S`) with AST-level rejection of
  imports, dunders, filesystem/network builtins, and dangerous calls. Plugins
  see only the context their approved permissions allow (`memory:read`,
  `live:read`, `live:emit`), and every run is access-logged.
- **Auditability:** installs, credential storage, scope changes, revocation,
  webhook ingestion, and plugin runs are all recorded in `access_log` with
  resource ids and metadata — never with token material.
