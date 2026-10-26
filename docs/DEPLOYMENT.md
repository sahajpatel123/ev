# EV — Deployment & Self-Hosting Plan

**Version 2.0 (LAUNCH)** — the daily driver is the **native stack**:
Homebrew-managed PostgreSQL 17 + pgvector, Redis, and launchd-supervised EV
services on the Mac. Docker Compose is **CI-only** (`.github/workflows/ci.yml`
runs `make postgres-e2e`). MinIO is removed from the daily path; the object
store is the filesystem (`EV_OBJECT_STORE_BACKEND=local`).

## 1. Requirements

| Component | Minimum | Recommended |
| --- | --- | --- |
| Host | Always-on Apple Silicon Mac, 8 GB RAM | 16 GB RAM |
| Disk | 20 GB free (5 GB guard for model downloads) | 100 GB+ |
| Runtime | Homebrew + launchd | — |
| Phone | iPhone (iOS 17+) | — |

**Honest hardware note:** on an 8 GB Mac, macOS + Postgres + Redis + the EV
API/worker leave roughly 1.7–2 GB of reclaimable RAM. Always-tier models
(165 MB) fit; an exclusive 2 GB LLM will push the machine into swap. The
ModelArbiter refuses over-ceiling loads, but macOS memory pressure is the
first real-world failure mode (see docs/OPS.md §8).

## 2. Topology (native primary)

```text
iPhone/Watch ── Tailscale/TLS ──► API :8000 (launchd ev.api)
Mac/web/CLI ────────────────────► Postgres 17 + pgvector :5432 (brew)
                                 ► Redis :6379 (brew)
                                 ► filesystem object store (EV_STORAGE_ROOT)
                                 ├── ev.worker     RQ ingestion
                                 ├── ev.scheduler  routines/automations
                                 ├── ev.runtime    daemon tick, digests, DLQ
                                 ├── ev.ears       always-on mic front end
                                 ├── ev.collector  ambient collectors
                                 └── ev.backup     nightly encrypted snapshot
```

## 3. Install (first run)

```text
make prune            # if disk is tight
./brew/setup.sh       # PG17 + pgvector + Redis + launchd services + backup job
cp .env.example .env  # EV_MASTER_KEY, EV_VAULT_KEY (>=16), EV_BACKUP_PASSPHRASE
make migrate
make seed             # optional demo corpus
make native-status    # all services + API health
make doctor           # one-screen health
```

Then complete the go-live ceremony from docs/OPS.md §1: owner identity +
recovery codes offline, consent grants, voice enrollment, model pulls,
first capture/answer/notification, backup + restore drill.

## 3b. Brain migration to Muse Spark (2026-10-25)

The registry now holds `echo`, `mock` and `muse`. A deployed `.env` whose
`EV_CHAT_PROVIDER` names any other provider makes every chat call fail loudly with
`unknown chat provider`, and the Talk sidecar refuses to start. Change the
environment and the code together, through the deploy wrapper only:

1. In the production `.env` set `EV_CHAT_PROVIDER=muse` and
   `EV_AGENT_PROVIDER=muse` (or delete the latter; `auto` picks `muse`).
   `EV_BRAIN_MODEL` defaults to `meta/muse-spark-1.3-contributor`; set it only
   to pin a different OpenRouter id. Leftover unknown `EV_*` keys are ignored.
2. Confirm `EV_OPENROUTER_API_KEY` (in `~/.ev/secrets/production.env`) has
   spend headroom: `GET https://openrouter.ai/api/v1/key` reports
   `limit_remaining`. A key at its total limit returns HTTP 403 on every call.
3. Deploy with `scripts/deploy_production.sh` (flock-guarded; restarts once).
   Do not `launchctl kickstart` by hand: the P0 deploy law applies.
4. From `backend/`: `uv run ev models verify` must exit 0 (both ids resolve).
5. Run `scripts/smoke_brain.py` for the live brain round trip, then a spoken
   test through the Talk sidecar.

Rollback is the same wrapper with the previous SHA and the previous `.env`
lines; the old providers are only available from that earlier SHA.

## 3a. API-first profile (the owner's blessed configuration)

The M2/8 GB host does **not** run local LLM inference; reasoning is
Muse Spark over OpenRouter (`EV_OPENROUTER_API_KEY`) and speech is
Gemini Live 3.8 (`EV_GOOGLE_API_KEY`, paid tier only). Muse Spark is the one mind
for every channel — typed chat, voice pipeline, live Talk receipts, device
text, coding jobs. Speech stays split by design: Gemini Live answers speech
directly and calls `delegate_task` for medium-high work, which Muse Spark does.
Local models stay only where an API is impossible or clearly worse: wake
word, OCR (Apple Vision, free), speaker verification (biometric privacy),
embeddings (recurring cost), and face recognition. The blessed
configuration lives in **`.env.api-first`** and is activated with
`cp .env.api-first .env` (then fill the FILL-ME secrets):

| Organ | Provider in profile | Why |
| --- | --- | --- |
| Chat/mind | `EV_CHAT_PROVIDER=muse` + `EV_COGNITIVE_MODE=mimo_kernel` (retained wire name) | One mind: Muse Spark decides, every channel |
| Brain model | `EV_BRAIN_MODEL=meta/muse-spark-1.3-contributor` | The OpenRouter id for every brain call, coding jobs included; `ev models verify` checks it |
| ASR | `EV_VOICE_ASR_PROVIDER=faster_whisper` | What Agent 2 actually shipped (Parakeet weights not in registry yet) |
| TTS | `EV_VOICE_TTS_PROVIDER=openai_compat` + `EV_ALLOW_REMOTE_TTS=true` | Hosted until Kokoro package + weights land |
| Speaker | `EV_VOICEPRINT_PROVIDER=campp` | Biometric privacy stays local |
| Vision | `EV_VISION_PROVIDER=apple_vision` | Free, on-device OCR via the `evvision` Swift helper |
| Wake | `EV_VOICE_WAKE_PROVIDER=openwakeword` | Wake word must be local |
| Embeddings | `EV_EMBEDDING_PROVIDER=granite` | Agent 8 verified recommendation (granite R2) |
| Face | `EV_FACE_PROVIDER=sface` | Verified SFace ONNX |
| Storage | `EV_OBJECT_STORE_BACKEND=local` | Filesystem; MinIO out of the daily path |
| Runtime | native Postgres 17 + Redis (compose is CI-only) | No fallback brain: Muse Spark + Gemini Live are the only two models |

**Hard refusal note (read before booting):** `default_speaker_verifier()`
(`backend/app/voice/speaker.py`) refuses the hash test double outside pytest.
With `EV_VOICEPRINT_PROVIDER=campp` and no exported `.onnx` in
`EV_VOICEPRINT_MODEL_DIR`, the voice path will not start — that is correct
fail-closed behavior. `make preflight` reports exactly which weight file is
missing and how to obtain it.

The profile also pins the enforceable monthly spend guard
(`EV_MONTHLY_COST_CAP_USD=40`, `EV_COST_CAP_ENABLED=true`) — see
docs/OPS.md §Cost.

## 4. TLS & device access

- Option A (recommended): Tailscale HTTPS (`tailscale serve https / http://localhost:8000`).
- Option B: Caddy reverse proxy with Let's Encrypt or a local CA.
- Never expose :8000 directly to the internet.

## 5. Backups

- Nightly encrypted snapshot: launchd `ev.backup` at 02:30
  (`brew/launchd/ev.backup.plist`), passphrase `EV_BACKUP_PASSPHRASE`
  (user-held, separate from `EV_MASTER_KEY`).
- Retention: `EV_BACKUP_RETENTION_COUNT` (default 7) via `make prune`.
- **Caches are excluded by construction**: the bundle contains events,
  memories, devices, access log, and attachment blobs. Model weights and
  datasets live in `~/.ev/models` + `~/.ev/datasets` and are re-downloadable;
  a 2 GB LLM is never duplicated into a snapshot.
- Monthly wipe→restore drill: `POST /v1/backup/restore` with
  `mode="wipe", confirm_wipe=true`. `/v1/ops/metrics` tracks
  `restore_drill.age_days` and alerts past 35 days.

## 6. Upgrades

```text
make prune-dry-run ; make prune
make migrate                  # Alembic forward-only
./launchd/install.sh          # reload services with new code
make verify                   # lint + typecheck + test + eval
```

## 7. Failure modes

| Failure | Recovery |
| --- | --- |
| Host reboot | launchd `KeepAlive` restarts all EV services; brew services restart Postgres/Redis |
| DB corruption | Restore nightly backup; audit log shows post-backup writes |
| Disk full | `make prune` (dry-run first); model downloads refuse below 5 GB free |
| RAM pressure/swap | `make doctor`; evict exclusive models; add RAM |
| Model API down | Gateway degrades to explicit echo/mock; memory features unaffected |
| Lost master key | Recovery codes + keychain; rotate device tokens |
| Stale restore drill | Alert in `/v1/ops/metrics` after 35 days; run the drill |

## 8. Cost model (personal scale)

Mac electricity ~$5–12/month · Muse Spark ~$2–8/month · on-device STT/TTS $0 ·
Tailscale $0 · backup storage $0–5/month · **typical total ~$10–40/month**.
Offline echo/hash mode costs $0.
