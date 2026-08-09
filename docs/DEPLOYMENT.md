# EV — Deployment & Self-Hosting Plan

**Version 1.0** — the "self-built, no Stark tech" story: how EV runs on your own
hardware, how devices connect, and how the system survives failure.

## 1. Requirements

| Component | Minimum | Recommended |
| --- | --- | --- |
| Host | Always-on Mac (Intel or Apple Silicon) | Apple Silicon, 16 GB RAM |
| Disk | 20 GB free | 100 GB+ (memories + blobs + backups) |
| Runtime | Docker Desktop or OrbStack | OrbStack (lighter) |
| Network | LAN | Tailscale account for phone access |
| Phone | iPhone (iOS 17+) | iPhone 16 Pro / SE both supported |
| Watch (M5) | watchOS 10+ | — |
| Apple Developer account | For on-device iOS builds | Free account sufficient for personal use |

## 2. Topology

```text
               ┌─────────────────────────────── Mac host ───────────────────────────────┐
iPhone/Watch ──┤ Tailscale ─► Caddy/TLS ─► API :8000 ─► Postgres/pgvector :5432         │
Mac/web/CLI ───┤                        └─► Redis :6379 ─► RQ workers                    │
               │                        └─► MinIO :9000 (attachments/blobs)              │
               │                        └─► nightly encrypted backups ─► user storage   │
               └────────────────────────────────────────────────────────────────────────┘
```

No ports are exposed to the public internet; Tailscale provides authenticated access.

## 3. Install (first run)

1. Clone/copy the repo to the Mac; `cp .env.example .env` and set:
   - `EV_MASTER_KEY` (long random value; store in the Mac keychain).
   - `EV_DEEPSEEK_API_KEY`, `EV_EMBEDDING_API_KEY` (optional; system works offline in
     echo/hash mode).
2. `make install` (backend deps) and `make compose-up` (db, redis, minio, api, worker).
3. `make migrate` (Alembic) then `make seed` (optional demo corpus).
4. `curl http://localhost:8000/v1/health` → green.
5. Install Tailscale on Mac + iPhone; allow `tailscale serve` or use the Tailscale IP
   directly with TLS via Caddy.
6. Pair devices (QR or master key) → device tokens registered.

## 4. TLS

- Option A (recommended): Tailscale HTTPS (`tailscale serve https / http://localhost:8000`).
- Option B: Caddy reverse proxy with automatic Let's Encrypt via DNS (self-hosted
  domain) or local CA.
- Never expose :8000 directly to the internet.

## 5. Backups

- Daily encrypted snapshot: Postgres dump + MinIO mirror + `.env` (secrets) into a
  user-controlled destination (external disk, Tailscale NAS, user's own S3).
- Snapshot manifest with checksums; retention 7 daily / 4 weekly / 12 monthly.
- Monthly restore drill (M4): wipe → restore → verify event/memory counts and a
  sample audit trail.
- Backup encryption key is user-held, separate from `EV_MASTER_KEY`.

## 6. Upgrades

1. `docker compose pull` + `make migrate` (Alembic forward-only).
2. Backups run before any upgrade; on failure, roll back to previous image + restore.
3. Client apps are additive: old clients keep working against new API (versioned
  endpoints, additive fields).

## 7. Device pairing & lifecycle

- Pair: master key → register device (name + capabilities) → device token returned.
- Tokens stored hashed in `devices`; revocation immediate.
- Lost phone: revoke device → export/delete as needed → re-pair.

## 8. Failure modes

| Failure | Recovery |
| --- | --- |
| Host reboot | `restart: unless-stopped` on compose services; health-check auto-start. |
| DB corruption | Restore daily backup; audit log shows post-backup writes. |
| Model API down | Gateway falls back to echo mode (explicit) or queues requests; memory features unaffected. |
| Disk full | Alert via gear telemetry; retention cleanup; backup to external storage. |
| Lost master key | Recover from backup keychain; rotate device tokens. |

## 9. Operations checklist (post-M0)

- `/v1/health` + "EV checkup" green.
- Backups verified weekly; restore drill monthly.
- Error rates and gateway latency monitored; p95 latency within budgets.
- Dependency updates monthly; lockfiles + image digests pinned.

## 10. Cost model (personal scale)

| Item | Estimate | Notes |
| --- | --- | --- |
| Mac electricity | ~$5–12/month | Always-on Apple Silicon, idle-aware |
| Docker/OrbStack | $0 (Docker Desktop free tier or OrbStack ~$20/yr) | Personal use |
| DeepSeek API | ~$2–15/month | 20k-token context, ~50 queries/day, V4 Flash pricing |
| Embeddings API | ~$1–5/month | 50k events, daily re-embed at scale |
| Search API (research) | $0–5/month | Brave/SerpAPI free tiers |
| STT/TTS | $0 (on-device) to $5/month (hosted voices) | Depends on D-04 |
| Tailscale | $0 (personal) | — |
| Backup storage | $0–5/month | User-provided disk/NAS |
| Apple Developer | $0 (free personal) or $99/year | Only if sideloading/notifications need paid account |
| **Typical total** | **~$10–40/month** | Model APIs dominate when enabled |

Cost guardrails: context budget caps tokens; digest batching caps alert volume;
retrieval caching avoids redundant embeddings; everything works in offline
echo/hash mode at $0.
