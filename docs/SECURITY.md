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
| Retrieval | `access="model"` excludes `never_send_to_model` in SQL; `sensitive` requires explicit per-item opt-in |
| Prompt assembly | Boundary test asserts absence; context builder is the only gateway to the model |
| Export | All levels included (user owns the data); export is authenticated |
| Logs | Access log stores ids, not content |

## 6. Deletion & redaction

- `DELETE /v1/events/{id}` is a **tombstone**: `tombstoned_at` + reason set; row
  preserved for audit.
- All memories derived from the tombstoned event are marked `redacted`; they are
  excluded from retrieval and prompts.
- `POST /v1/export` provides the full bundle before deletion (portability).
- Blobs referenced by a tombstoned event are scheduled for deletion after the audit
  window (configurable; default 30 days).

## 7. Backup & restore

- Daily encrypted snapshot: Postgres dump + object-store mirror + config.
- Snapshot manifest with checksums; retention policy (e.g., 7 daily, 4 weekly,
  12 monthly).
- Restore drill (M4 acceptance): wipe → restore → verify event count, memory count,
  and a sample audit trail.
- Backup destination is user-controlled (external disk, Tailscale NAS, user's own
  S3) — never a third-party default.

## 8. Model boundary contract (tested)

1. `Retriever.search(access="model")` excludes `never_send_to_model` rows.
2. `Orchestrator.build_context()` accepts only retrieved items; no code path reads
   raw events into the prompt.
3. Tests instrument the exact payload sent to the chat provider and assert:
   - No `never_send_to_model` content (by id and text).
   - Assembled context ≤ budget.
   - Tool results bounded and terminating.

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
| Alerts | Alert radar | Postgres `alerts` | 12 months | Immediate on delete |
| Research sources | Research assistant | Postgres + blobs | Indefinite | Tombstone + blob schedule |
| Projects/BOM/prints | Maker flows | Postgres | Indefinite | User delete |
| Blobs (attachments) | Uploads | MinIO/S3 | Indefinite | 30-day audit window after tombstone |
| Access log | Every action | Postgres `access_log` | 12 months | User-triggered wipe |
| Backups | Nightly job | User storage (encrypted) | 7 daily / 4 weekly / 12 monthly | Rotation |

All retention is configurable via `Settings`; export runs before any destructive
retention job.
