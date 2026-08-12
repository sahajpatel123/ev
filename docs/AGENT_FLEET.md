# EV — Multi-Agent Fleet Plan v3.0 (make EVIE real · 20 agents)

**Version 3.0 — 2026-08-11**
**Authority:** ownership, merge protocol, done-when.
**First action for every agent:** read [`FLEET_LAW.md`](FLEET_LAW.md) — it is
binding and unedited.
**Paste-ready messages:** [`AGENT_LAUNCH.md`](AGENT_LAUNCH.md) (v2 launch pack;
Agent 1 publishes the v3 pack as Agent 3–20 briefs land).
**Strategy:** [`NEXT_STEPS.md`](NEXT_STEPS.md) and the human's Fleet-of-20 plan.

This is not process theatre. The job is a **personal product you can live in**
after Agents 1–20 finish: real ML engines behind clean contracts, always-on
presence, memory, filter, companion, real signal, perception, training, EDITH
software, surface, ops.

---

## 0. Decision

1. **Exactly 20 agents** (numbered **1–20**). Hard cap 20. No 19 equal domain
   agents; no Domain 20.
2. **Send order is number order only:** 1, then 2, then … then 20. Run at most
   2–3 agents concurrently on this 8 GB Mac.
3. **North star:** EVIE-class personal presence — not SaaS, not AR hardware,
   **not Domain 20**.
4. **Priority law:** real engines and density beat new scaffold. Offline CI
   stays green. The suite is sacred.
5. **Exclusive paths.** Nested `clients/**` ownership is banned (see §2).

## 1. Roster — 1 → 20

| # | Codename | Domain | When they are done, you can… |
| --- | --- | --- | --- |
| **1** | CONDUCTOR | Tree, merge, law, CI, contract, migration chain | Merge 19 agents without collision |
| **2** | FOUNDRY | ML runtime, ModelArbiter, weight + dataset registry | Load real models without killing the Mac |
| **3** | EARS | Mic capture, VAD, wake word, audio scene | Say "EVIE" and be heard |
| **4** | VOICE | ASR + TTS + audio persistence + barge-in | Talk and be answered aloud |
| **5** | SENTRY | Speaker verification, liveness, anti-spoof | Trust that only you can open it |
| **6** | EYES | Screen/camera capture, OCR, detection, labels | Show her a document or your screen |
| **7** | ROSTER | Consented face enrollment, recognition, biodata | Have her know the people you know |
| **8** | SYNAPSE | Embeddings, retrieval, reranking, eval | Get the right memory back |
| **9** | MNEMO | Extraction, entity resolution, consolidation, context | Dump messy life and get typed memory |
| **10** | CORTEX | Gateway, SSE streaming, local brain, tools, sandbox | Think fast, act safely, work offline |
| **11** | FORGE | Real LoRA/DPO training, eval harness, rollback | Train her on your own corrections |
| **12** | CONDUIT | Real OAuth: calendar, health, repos, mail | Feed her your actual day |
| **13** | AMBIENT | macOS collectors, live stream/retention/rebuild | Give her ambient context, privately |
| **14** | PULSE | launchd always-on, **real notification delivery** | Be reached when it matters |
| **15** | ORACLE | EDITH modules: HUD, tactical, radars, research | Pull a briefing that isn't empty |
| **16** | CONSCIENCE | Filter grounding + persona | Get honest answers with character |
| **17** | WORKBENCH | CLI + web HUD console | Open a console every morning |
| **18** | SUIT | Native macOS menu-bar app + iOS project | Have EVIE live in your menu bar |
| **19** | VAULT | Identity, WebAuthn, compliance, erasure | Lose a device and recover; delete for real |
| **20** | LAUNCH | ML eval gates, native stack, backups, runbook | Ship it and sleep |

## 2. File-level exclusive ownership

| # | OWNS | MUST NOT TOUCH |
| --- | --- | --- |
| **1** | `docs/{FLEET_LAW,AGENT_FLEET,AGENT_LAUNCH}.md`, `.github/workflows/**`, `backend/eval/contract_v1.json`, `backend/app/{main,db,contracts}.py`, migration chain linearization | Feature code in 2–20 unless a documented hard unblocker |
| **2** | `backend/pyproject.toml`, `backend/uv.lock`, `backend/app/ml/**`, `backend/app/datasets/**`, `docs/{MODELS,DATASETS,MODEL_BUDGET}.md` | Any feature module; any perception engine |
| **3** | `backend/app/audio/**`, `backend/app/voice/wake.py`, `backend/clients/ears/**`, `docs/AUDIO.md` | `voice/{asr,tts,speaker,anti_spoof}.py`; collectors; CLI |
| **4** | `backend/app/voice/{asr,tts,pipeline,lifecycle,contracts}.py`, `backend/app/api/voice.py`, `docs/VOICE.md` | `wake.py`, `speaker.py`, `anti_spoof.py`, `audio/**` |
| **5** | `backend/app/voice/{speaker,anti_spoof,security,sensitive}.py`, `docs/VOICE_SECURITY.md` | `asr.py`, `tts.py`, `wake.py`, identity service |
| **6** | `backend/app/vision/**`, `backend/app/ev/vision.py`, `helpers/evvision/**` (Swift OCR helper), `docs/VISION.md` | `app/people/**`; collectors; voice |
| **7** | `backend/app/people/**`, `backend/app/ev/people.py`, `docs/PEOPLE.md` | `app/vision/**`; identity service; compliance policy |
| **8** | `backend/app/embeddings.py`, `backend/app/memory/retrieval.py`, `backend/app/rerank.py`, `backend/eval/retrieval/**` | `memory/{extraction,entities,writer}.py`; gateway |
| **9** | `backend/app/memory/{extraction,entities,importance,patterns,writer}.py`, `backend/app/services/{processor,consolidation,recall,rebuild,importer,event_service}.py`, `backend/app/context/**` | `retrieval.py`; `embeddings.py`; voice; filter |
| **10** | `backend/app/gateway/**`, `backend/app/services/{tool_loop,model_call}.py`, `backend/app/tools/**`, `backend/app/search/**`, `backend/app/ev/{tools,tool_select,actions}.py` | Voice engines; filter ledger; training |
| **11** | `backend/app/training/**`, `backend/app/api/training.py` (except voice-enroll seams), `docs/TRAINING.md` | Voice engine files; filter policy apply; gateway providers |
| **12** | `backend/app/integrations/**`, `backend/app/api/integrations.py`, `docs/INTEGRATIONS.md` | `collectors/**`; `device_listener.py`; surface UI |
| **13** | `backend/clients/collectors/**`, `backend/app/services/live_{stream,retention,rebuild}.py`, `docs/LIVE_DATA.md` | `device_listener.py`; `cli/**`; `web/**`; voice |
| **14** | `backend/app/workers/**`, `backend/app/services/runtime.py`, `backend/app/api/runtime.py`, `backend/app/notify/**`, `backend/app/routines/**`, `backend/app/api/routines.py`, `backend/clients/device_listener.py`, `launchd/**` | `collectors/**`; `cli/**`; `web/**`; voice engines |
| **15** | `backend/app/ev/**` except `vision.py`, `people.py`, `tools.py`, `tool_select.py`, `actions.py`, `companionship.py`, `personality.py`, `interaction.py`, `conversation.py`; `docs/schemas/**` | Other agents' `ev/` files; AR hardware |
| **16** | `backend/app/filter/**`, `backend/app/ev/{companionship,personality,interaction,conversation}.py`, `backend/app/api/filter.py`, `docs/BEHAVIOR.md` | ASR/TTS; gateway providers; training apply |
| **17** | `backend/clients/cli/**`, `backend/clients/web/**`, `backend/app/api/web.py`, `docs/CLIENTS.md` | `device_listener.py`; `collectors/**`; `clients/ears/**`; any `app/` engine |
| **18** | `macos/**` (new SwiftPM app), `ios/**`, `docs/APPLE_CLIENTS.md` | Any Python under `backend/app/**` |
| **19** | `backend/app/identity/**`, `backend/app/compliance/**`, `backend/app/security/**`, `backend/app/auth.py`, `backend/app/api/{identity,compliance}.py`, `docs/{SECURITY,IDENTITY_TRUST}.md` | Voice engines; training trainer; multi-user anything |
| **20** | `backend/app/scripts/**`, `backend/app/ops/**`, `backend/app/api/{ops,backup,maintenance}.py`, `backend/app/services/{backup,maintenance,access_log}.py`, `docs/{DEPLOYMENT,OPS,QA,EVALUATION}.md`, `brew/**` | Feature code in 2–19 unless a documented ops unblocker |

**No nested globs:** parent `clients/**` must never be owned while children are
split (`device_listener.py` vs `cli/**` vs `web/**` vs `collectors/**`).

## 3. Shared append-only files

Unassigned-by-design shared files (append-only, see FLEET_LAW §3):
`Makefile`, `.env.example`, `compose.yaml`, `docs/ENVIRONMENT.md`,
`backend/app/config.py`, `backend/app/models.py`, `backend/app/schemas.py`,
`backend/app/api/{core,ev,edith,companion,tools}.py`.

Rules:

- Append inside a block marked `# --- AGENT <N> <CODENAME> ---`.
- Never modify, reorder, reformat, or delete another agent's lines.
- Never change an existing endpoint signature, table column, or setting
  default. Additive only.

## 4. Migration head (publish and pin)

**Current Alembic head: `2f31c7d0a1b2`** (verified 2026-08-11).

Every agent sets `down_revision` to the head that exists when they start.
Never edit another agent's migration. CONDUCTOR linearizes the chain at merge.
`CREATE EXTENSION vector` runs on PostgreSQL only — SQLite upgrades must stay
clean.

## 5. Roster board (idle | in_progress | blocked | done)

| # | Codename | Status |
| --- | --- | --- |
| 1 | CONDUCTOR | done |
| 2 | FOUNDRY | in_progress |
| 3 | EARS | idle |
| 4 | VOICE | idle |
| 5 | SENTRY | idle |
| 6 | EYES | idle |
| 7 | ROSTER | idle |
| 8 | SYNAPSE | idle |
| 9 | MNEMO | idle |
| 10 | CORTEX | idle |
| 11 | FORGE | idle |
| 12 | CONDUIT | idle |
| 13 | AMBIENT | idle |
| 14 | PULSE | idle |
| 15 | ORACLE | idle |
| 16 | CONSCIENCE | idle |
| 17 | WORKBENCH | idle |
| 18 | SUIT | idle |
| 19 | VAULT | idle |
| 20 | LAUNCH | idle |

## 6. Communication and merge order

1. Shared worktree `/Users/sahajpatel/Code/ev`. No force-push. No commit/push
   unless the human asks.
2. Conflict → stop and report. Dependency notes instead of silent expansion.
3. Verify: domain tests + ruff + mypy + `pytest -q`; do not leave the suite red.
4. **Merge order:** ascending number **1 → 20**.
5. Every agent reads FLEET_LAW.md first and ends every report with the
   mandatory footer (§8).

## 7. Bans

| Ban | Why |
| --- | --- |
| Domain 20 | Breadth complete |
| 19 equal agents | Thrash |
| Multi-user / guest mode | One owner |
| Public bare API ports | Tailscale + TLS |
| Silent LoRA / silent filter apply | Consent + eval |
| Mock engines passed as done | Lifecycle ≠ presence |
| AR hardware as hard goal | Software first |
| Stranger identification | Legal + trust |

## 8. Mandatory report footer

```text
── REPORT ──
FILES TOUCHED:        every path, all inside OWNS
COMMANDS RUN:         exact command lines + pass/fail counts
MEASURED NUMBERS:     each acceptance metric with its value and how you measured
MODELS ADDED:         name · license · source_url · sha256 · disk_mb · resident_mb · tier
DEP REQUEST:          packages needed from Agent 2, with reason + wheel size
DEPENDENCY NOTES:     messages for other agents, addressed by number
HUMAN APPROVALS:      keys, OS permissions, downloads >250 MB, physical recordings
WHAT IS STILL NOT REAL: honest gaps, ranked by impact. Must be non-empty.
```

## 9. Done when (product usable)

- [ ] Suite green offline under CONDUCTOR / LAUNCH: ruff 0, mypy 0, full pytest,
      `eval_gates` exit 0
- [ ] FOUNDRY arbiter: 1000-load/evict fuzz with 0 ceiling breaches / 0 OOM
- [ ] EARS: wake word recall ≥ 90%, VAD ≥ 95%, scene top-1 ≥ 80%
- [ ] VOICE: ASR WER ≤ 8%, TTS audio persisted and playable
- [ ] SENTRY: speaker EER ≤ 3%, 0 replay accepts
- [ ] EYES: OCR ≥ 95% char accuracy, screen capture ≤ 600 ms
- [ ] ROSTER: TAR@FAR=1e-3 ≥ 95%, 100% stranger rejection
- [ ] SYNAPSE: retrieval nDCG@10 ≥ 0.80
- [ ] MNEMO: extraction precision ≥ 85%, recall ≥ 75%
- [ ] CORTEX: first token ≤ 800 ms local, sandbox 20/20 escapes blocked
- [ ] FORGE: LoRA train within 8 GB, rollback byte-identical
- [ ] CONDUIT: real calendar OAuth, 7 days of events, 0 secrets logged
- [ ] AMBIENT: 24 h continuous at ≤ 40 MB RSS, 0 raw pixels stored
- [ ] PULSE: real Notification Center delivery, 72 h uptime under launchd
- [ ] ORACLE: HUD schemas 100% valid, quickcard p95 ≤ 800 ms
- [ ] CONSCIENCE: ≥ 95% ungrounded claims flagged, ≤ 5% false removal
- [ ] WORKBENCH: day-1 capture + ask + audit in ≤ 10 actions
- [ ] SUIT: SwiftPM `.app` launchable with CLT only
- [ ] VAULT: recovery drill automated; full erasure covers face/voice/corpus
- [ ] LAUNCH: `make eval` exit 0 with ML gates; wipe-restore drill passes
- [x] No Domain 20 / multi-user / AR-as-hard-goal in scope

## 10. Bottom line

Twenty agents. Number order only. Exclusive paths. Real engines behind locked
contracts. After Agents 1–20 finish, the human can live in this system without
babysitting a broken tree.

## 11. Decision log — inference topology (2026-08-12)

The owner's machine (Apple M2, 8 GB) cannot host local LLM inference.
Reasoning now runs through the hosted DeepSeek API. Recorded 2026-08-12:

- Local models are permitted and preferred ONLY where an API is impossible or
  clearly worse: wake word (continuous mic), OCR (Apple Vision, free), speaker
  verification (biometric privacy), face embedding.
- Everything else uses an API provider behind the existing seam.
- Every remote path must pass a `remote_processing_allowed()` gate.

Binding text: [`FLEET_LAW.md`](FLEET_LAW.md) §13 (INFERENCE TOPOLOGY).
Decision log: [`DECISIONS.md`](DECISIONS.md) DC-14.

## 12. Post-fleet baseline (2026-08-12)

Regression reference for the follow-up wave. Numbers below are the measured
state of the committed tree (follow-up refresh after contract regeneration and
ML eval artifacts landed; at fleet-report time the tree measured 977 collected,
256 mypy files, 275/298 OpenAPI paths/operations, and 18/18 gates with 5
skipped).

| Metric | Value | Evidence |
| --- | --- | --- |
| Tests collected | 1028 | `uv run pytest --collect-only -q` |
| Test result | 1023 passed, 5 skipped, 0 failed | `uv run pytest -q` |
| mypy | 0 issues in 262 source files | `uv run mypy app clients` |
| ruff | 0 issues | `uv run ruff check app clients tests` |
| OpenAPI paths / operations | 276 / 299 | generated from `app.main` OpenAPI |
| Locked contract paths / operations | 272 / 295 | `backend/eval/contract_v1.json` |
| Contract gate | PASS — all 295 locked endpoints present; no unlocked /v1 routes | `eval_gates` api_contract gate |
| Eval gates | 18/18 passed · 124/124 checks · 3 skipped · exit 0 | `uv run python -m app.scripts.eval_gates --report eval/last-run.json` |
| Alembic head | `c0d0e0f0a7b1` (mergepoint) | `uv run alembic heads` |
| Migrations | 7 scripts; SQLite `alembic upgrade head` clean; `CREATE EXTENSION vector` PostgreSQL-guarded | `alembic upgrade head` on fresh SQLite |

### Skip reasons (verbatim)

1. **speaker_security**

   ```text
   SKIPPED: artifact reports provider='profile-v1' degraded (weights absent / deterministic double); a test double is never a measured quality number. Run the owning agent's eval with real weights and rewrite /Users/sahajpatel/Code/ev/backend/eval/ml/speaker_security.json.
   ```

2. **face_recognition**

   ```text
   no eval artifact at /Users/sahajpatel/Code/ev/backend/eval/ml/face_recognition.json; run `python -m app.people.eval --people-dir ... --strangers-dir ... --report eval/ml/face_recognition.json` with the SFace model and consented photo sets
   ```

3. **wake_reliability**

   ```text
   no eval artifact at /Users/sahajpatel/Code/ev/backend/eval/ml/wake_reliability.json; run Agent 3's wake eval against the trained openWakeWord head ({"provider":"openwakeword","degraded":false,"false_accepts_per_12h":0.0,"recall":0.95,"hours_audio":12})
   ```
