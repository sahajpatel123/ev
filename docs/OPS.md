# EV — Operations, Evaluation Gates & Go-Live Runbook

**Version 2.0 (LAUNCH)** — the daily driver is the **native stack** on an
Apple Silicon Mac (Homebrew Postgres 17 + pgvector, Redis, launchd-supervised
EV services, filesystem object store). Docker Compose is **CI-only**
(`make postgres-e2e`); MinIO is not part of the daily path.

## 1. One-page go-live runbook

Follow this in order on a fresh Mac. Each step is one command or one file.

```text
1. Free disk            df -h / ; make prune-dry-run ; make prune
2. Native bootstrap     ./brew/setup.sh            # PG17+pgvector+Redis+launchd
3. Keys & vault         cp .env.example .env
                        # set EV_MASTER_KEY, EV_VAULT_KEY (>=16 chars),
                        # EV_BACKUP_PASSPHRASE; store all three offline
4. Migrate & seed       make migrate ; make seed
5. Owner identity       ev onboarding "First goal" --owner "Name" --consent voice_enrollment
                        # write recovery codes to paper NOW (ev identity recovery)
6. Consent grants       ev consent grant voice_enrollment ; face_enrollment ; training_corpus
7. Voice enrollment     ev voice-enroll <5 wavs> --liveness live
8. Model pull           make ml-install ; python -m app.ml.cli pull <models>
9. First capture        ev capture "hello evie"
10. First answer        ev ask "what did I just capture?"
11. First notification  make notify-test           # grants macOS notification permission
12. Nightly backup      launchctl kickstart -k gui/$UID/ev.backup
13. Verify backup       curl -X POST localhost:8000/v1/backup/verify -d '{"path":...,"passphrase":...}'
14. Restore drill       POST /v1/backup/restore (mode=wipe, confirm_wipe=true), then make native-e2e
15. Health              make doctor ; make native-status
```

**Runbook executed on this Mac: 2026-08-11/12 — see §7 for exactly where it
broke.**

## 2. Native stack

| Command | What it does |
| --- | --- |
| `./brew/setup.sh` | Installs/starts Postgres 17 + pgvector + Redis, creates `ev` role/db, loads all launchd services, installs the nightly backup job |
| `make native-up` | Runs `brew/setup.sh` |
| `make native-down` | Unloads EV launchd services and stops brew Postgres/Redis |
| `make native-status` | Brew services + launchd labels + API health in one screen |
| `make doctor` | One screen explaining why EV feels slow (RAM, disk, swap, RSS, service states, API latency, restore-drill age) |
| `make preflight` | One screen answering "is EV actually real right now?" — per-organ REAL/DOUBLE/PARTIAL with the exact remediation |
| `make eval-ml` | Runs every available ML eval (`ev-eval all` → `eval/ml/*.json`), then `eval_gates`; skipped gates stay explicit |
| `make prune` | Reclaims old backups, dev caches, old weights, expired datasets; `make prune-dry-run` previews bytes |
| `make verify` | lint + typecheck + test + eval in one pass (previously `make doctor`) |

The object store default is `EV_OBJECT_STORE_BACKEND=local`
(`EV_STORAGE_ROOT=./storage`); attachments live on the filesystem. Compose
still exists for CI: `.github/workflows/ci.yml` runs `make postgres-e2e`
(compose up → migrate → seed → e2e CLI) with `E2E_RESET_DB=1`.

## 3. Evaluation gates (`make eval`)

18 gates; exit 0 means a healthy tree. The six ML gates read artifacts from
`backend/eval/ml/` and **skip loudly** when weights/data are absent so offline
CI stays green, and **fail** when a measured artifact misses its threshold.

| Gate | Thresholds | Artifact |
| --- | --- | --- |
| `asr_quality` | WER ≤ 8% clean / ≤ 12% owner | `eval/ml/asr_quality.json` |
| `speaker_security` | EER ≤ 3%, 0 false accepts at shipped threshold | `eval/ml/speaker_security.json` |
| `retrieval_quality` | nDCG@10 ≥ 0.80, top-5 hit ≥ 90% | `eval/ml/retrieval_quality.json` |
| `face_recognition` | TAR ≥ 95% @ FAR 1e-3, 100% stranger rejection | `eval/ml/face_recognition.json` |
| `wake_reliability` | ≤ 1 false accept / 12 h, recall ≥ 90% | `eval/ml/wake_reliability.json` |
| `grounding` | ≥ 95% ungrounded flagged, ≤ 5% false removal | measured in-process (Agent 16 corpus) |

`regression` compares every measured metric (latency and ML) against
`eval/previous-run.json`; ML degradation (WER/EER/FAR up, nDCG/TAR/recall
down) blocks the run.

## 4. Budgets

Event ack < 1000 ms · chat first token < 1500 ms · timeline browse < 500 ms ·
tactical briefing < 3000 ms · quick card < 800 ms · monthly spend ≤ $40.
Measured in `/v1/ops/metrics` and `/v1/ops/center`.

## 4a. Monthly DeepSeek cost (API-first)

The blessed profile (`.env.api-first`) sends reasoning to DeepSeek
(`deepseek-v4-flash-0731`). At the documented owner-scale usage — ~50
conversations/day, ~20k prompt tokens each, ~500 completion tokens each:

```text
input:    50 × 30 days × 20k tokens = 30M tokens  × $0.27/M  = $8.10
output:   50 × 30 days × 0.5k tokens = 0.75M tokens × $1.10/M = $0.83
--------------------------------------------------------------------
expected total ≈ $9/month  (realistic range $3–15 with context reuse)
```

Knobs that reduce it:

- **Agent 9 extraction batching** — memory extraction/consolidation runs as
  batched LLM passes instead of one call per event, cutting DeepSeek call
  count and prompt duplication.
- **Agent 10 CORTEX cap** — local `qwen3-1.7b` (Ollama) handles
  classification/offline reasoning, so those tokens never reach DeepSeek.
- **Context budget** — `EV_CONTEXT_BUDGET_TOKENS=20000` caps every prompt;
  rolling summaries (`EV_ROLLUP_BUDGET_TOKENS`) keep history small.
- **Digest batching** — `EV_DAILY_ALERT_BUDGET` caps notification/alert LLM
  volume; quiet-hours digest batches instead of per-alert calls.

When the cap is hit: `app/gateway/costs.py` projects each request's cost
before calling the provider and raises `CostCapExceeded` when the current
month's spend plus the projection would exceed `EV_MONTHLY_COST_CAP_USD`
(default $40, `EV_COST_CAP_ENABLED=true`). No further paid calls are made
that month; the chat request fails with the cap message (today surfaced as a
500 from the chat endpoint). The owner raises the cap, waits for the calendar
month to roll, or turns on the CORTEX local brain to keep answering offline.

## 5. Backups & restore drills

- Nightly encrypted snapshot: launchd `ev.backup` (02:30, logs in
  `~/Library/Logs/ev/backup.*.log`), passphrase from `EV_BACKUP_PASSPHRASE`,
  retention `EV_BACKUP_RETENTION_COUNT` (default 7).
- Backups exclude model/dataset caches by construction: the bundle contains
  the event log, memories, devices, access log, and attachment blobs only.
  Caches live in `~/.ev/models` + `~/.ev/datasets` and are re-downloadable.
- `POST /v1/backup/restore {mode:"wipe", confirm_wipe:true}` records the drill
  timestamp; `/v1/ops/metrics` reports `restore_drill.age_days` and alerts
  when > 35 days.

## 6. Failure recovery & troubleshooting

| Symptom | Check / fix |
| --- | --- |
| API unreachable | `make native-status`; `launchctl print gui/$UID/ev.api`; logs `~/Library/Logs/ev/api.*.log`; postgres/redis via `brew services list` |
| No `daemon` events | runtime service down; `make migrate`; `.env` has `EV_VAULT_KEY` ≥ 16 chars |
| Disk near full | `make prune-dry-run` then `make prune`; model downloads refuse below 5 GB free |
| EV feels slow | `make doctor` — RAM/swap/stack RSS/API latency in one screen |
| Restore drill stale | `make doctor` shows age; run a wipe→restore drill via `/v1/backup` |
| Queue worker not writing | `ev.queue` shows backlog; restart `ev.worker`; check Redis |

## 7. Go-live execution log (this Mac, 2026-08-11/12)

Executed end to end on this Mac. Exactly where it broke, and the fix:

1. **Brew bootstrap (broke):** postgresql@17 + pgvector + redis installed;
   Redis refused to start because a stale Redis-Stack config block loaded
   nonexistent `./modules/*.so` files. Fix: `brew/setup.sh` backs up
   `/opt/homebrew/etc/redis.conf` and comments the `loadmodule` lines.
2. **Native API on SQLite (broke):** launchd services sourced `.env`, which
   had no `EV_DATABASE_URL`/`EV_REDIS_URL`, so the API silently used the
   default SQLite file and crashed on the new schema. Fix: `brew/setup.sh`
   appends the native Postgres/Redis/queue/local-store defaults to `.env`
   (additive only) and restarts services.
3. **pgvector extension (broke):** Alembic failed with
   "permission denied to create extension vector" because `ev` is not a
   superuser. Fix: `brew/setup.sh` runs `CREATE EXTENSION IF NOT EXISTS
   vector` as the local superuser before migrate.
4. **Nightly backup job (broke twice):** first the job ran before
   `EV_BACKUP_PASSPHRASE` existed (fail-closed, correct); after setting the
   passphrase, a directory `--destination` was written as a literal file
   named `backups` and then crashed on a missing `Path` import. Fix:
   `backup_snapshot.py` treats a directory destination as a directory and
   stamps `ev-backup-<ts>.evbackup`; retention now works (kept 2).
5. **ears/collector auth (broke):** both launchd services exited with
   `EV_API_KEY is not set`. Fix: `brew/setup.sh` appends
   `EV_API_KEY=<master key>` for the single-user local stack.
6. **launchd bootstrap I/O errors:** after editing env, `install.sh` can hit
   `Bootstrap failed: 5: Input/output error` on already-loaded services;
   `launchctl bootout` + `bootstrap` per service clears it. The stack is
   running after the manual reset.
7. **Voice enrollment (blocked, honest):** the live API refuses the hash
   test double outside pytest (`EV_VOICEPRINT_PROVIDER resolves to the hash
   test double, which is not a security control`). Voice round trip cannot be
   proven until Agent 5's CAM++/ECAPA weights are present and
   `EV_VOICEPRINT_PROVIDER=campp` is set. `make native-e2e` therefore runs
   with `EV_E2E_EXPECT_REAL_VOICE=0` and prints an explicit skip.
8. **Model pull (blocked, dependency):** only 1 of 13 registry entries is
   checksum-verified; `python -m app.ml.cli pull` refuses seed entries until
   Agent 2 (FOUNDRY) pins `sha256` + `verified=True`. No weights could be
   pulled during this runbook execution.

What passed: `make native-up` (after fixes), migrate + seed on Postgres 17,
owner identity + 8 recovery codes, consent grants, first capture + grounded
answer with provenance, notification delivered with a console receipt,
encrypted backup + verify + wipe→restore drill (7/7 events, memories 12,
audit sample survived, backup 97 KB vs 275 MB model cache excluded,
`restore_drill.age_days=0`), and `make native-e2e` (worker wrote captures,
daemon tick observed, scheduler executed a routine, notification receipt,
export→import round trip, onboarding). `make prune` reclaimed 149.2 MB.
Cold boot after `make native-down`: **12.5 s** (≤ 60 s acceptance), all
launchd services + Postgres 17 + Redis healthy, API green, `make eval`
18/18. `make doctor` reports ~1.8–2.0 GB reclaimable RAM on this 8 GB Mac
with the full stack — the ≥3 GB free-RAM acceptance is not physically
achievable here and is item 1 of the residual risks.

### 7.1 API-first profile execution (Follow-up 8, 2026-08-12)

`cp .env.api-first .env` (merged additively over the existing secrets),
restarted all services, and re-ran the runbook:

| Check | Result |
| --- | --- |
| Cold boot (`make native-down` → `make native-up`) | **17.3 s** (≤60 s) |
| Stack RSS (idle services) | ~130–194 MB |
| granite embeddings + sface face loaded (held) | process RSS 126 MB (CoreML/ANE offload), free RAM while held **~1.47 GB** |
| wake-openwakeword + speaker-campp | not loadable — weights are unpinned seed entries (Agent 2) |
| `make preflight` | 6 REAL, 0 DOUBLE, 6 PARTIAL — exact remediations per organ |
| `make eval-ml` | exit 0; `retrieval_quality` **measured** (nDCG@10 0.98, top-5 0.98) and `asr_quality` **measured** (WER 5.9% on LibriSpeech test-clean, faster_whisper); speaker/face/wake skip explicitly |
| Chat | breaks honestly: 503 "401 Unauthorized" until `EV_DEEPSEEK_API_KEY` is set |
| Voice enrollment | breaks honestly: 422 "liveness model unavailable; failing closed" (liveness weights missing) — then CAM++ refusal after that |
| Notification | delivered (console receipt) |
| Backup | 20 events, 726 KB, verified |

Breakages found under the profile (all expected, all with named remediations in
`make preflight`): missing DeepSeek key, missing liveness/CAM++/wake/ASR/TTS
weights (Agent 2 seed entries), missing `kokoro` package (Agent 2 dep), and
the `face` extra (fixed during this run with `uv sync --extra face`). Apple
Vision helper was built (`swift build -c release` in `helpers/evvision`) and
is REAL.

## 8. Residual risks (no sugarcoating)

- **8 GB RAM is the hard wall.** Always-tier models (165 MB) + macOS +
  Postgres + Redis + API/worker leave ~1.7 GB reclaimable today; an exclusive
  LLM (2 GB) will push the machine into swap. The arbiter refuses over-ceiling
  loads, but macOS itself will throttle long before EV does.
- **Real ML quality is unmeasured until the owning agents ship artifacts.**
  Five of the six ML gates currently skip: no WER/EER/FAR/wake numbers exist
  on this machine, and the hash/echo doubles are explicitly not security
  controls.
- **No wake hardware.** The always-on wake path needs Porcupine/openWakeWord
  weights + a custom EVIE head; until then the phrase double is not a product
  wake word.
- **Backup passphrase ceremony is manual.** If `EV_BACKUP_PASSPHRASE` is lost,
  backups are unrecoverable; it must be stored offline before go-live.
- **First failure mode under real use:** disk (models + datasets + backups on
  13 GB free), then RAM (swap), then a stale restore drill (alert at 35 days).
