# EV — What Comes After the 19 Domains

**Version 1.0 — post-breadth productization guide**
**As of:** 2026-08-10 · **HEAD:** `431dd95` (+ uncommitted hardening)

This document is the operating plan for the phase *after* breadth is done.
It is grounded in the current repo (WORK_BREAKDOWN, ROADMAP, AGENT_FLEET,
AGENT_BRIEFS, code, tests), not in aspiration.

**Multi-agent execution:** **exactly 15 agents (numbered 1→15)** with exclusive path ownership
for a personal shippable EVIE/EDITH stack (presence + perception + notifications
+ real training + EDITH software + tools) — not 19 equal domain agents and not
Domain 20. Source of truth: [`AGENT_FLEET.md`](AGENT_FLEET.md). **Paste
messages:** [`AGENT_LAUNCH.md`](AGENT_LAUNCH.md).

---

## 0. Executive diagnosis

You have finished **construction of the thesis**. All 19 domains are
implemented at factor level; only two factors remain explicitly **Future**
(HUD hardware targets §13.5, multi-user boundary §16.4). The tree holds
~157 Python modules under `backend/app`, ~256 HTTP endpoints across 17
routers, 424 tests, 84 open "Direction" refinements in WORK_BREAKDOWN, a
compose stack (db/redis/minio/api/worker/scheduler/runtime), CLI,
web workbench, and an iOS Swift package foundation.

That is a phase change. The right next move is **not Domain 20**.

| Phase you just finished | Phase you are entering |
| --- | --- |
| Breadth: every organ exists | Depth: every organ earns trust |
| Architecture & factor completeness | Daily-driver productization |
| Mock/dev providers prove contracts | Real engines prove the experience |
| Tests prove the system *can* work | Usage proves the system *does* work |
| Plan docs as source of truth | Friction log as source of truth |

**The product moat is still memory + provenance + filter.** Everything else
is amplification. Next work should deepen those three under real use, then
surface them on devices you actually carry.

---

## 1. Honest maturity map (19 domains)

Statuses from `docs/WORK_BREAKDOWN.md`. "Depth gap" is what still separates
Built from *lived experience*.

| # | Domain | Status | Depth gap (what is still synthetic / incomplete) |
| --- | --- | --- | --- |
| 1 | Memory & data foundation | **Built** | Rule-based extraction; LLM extractor unused; entity merge weak |
| 2 | Single conversation & context | **Built** | Progressive tool-loaded deep dives; adaptive history window |
| 3 | Intelligence filter | **Built** | Semantic claim grounding; learned critic; chunk-level stream refine |
| 4 | Provider & models | **Built (v1)** | Routing policy still eval-gated; local 2B critic not wired |
| 5 | Voice & speech | **Built / Built (dev)** | Wake is phrase-dev; speaker is `ProfileSpeakerVerifier` (hash embeddings); ASR/TTS have OpenAI-compat seams but default offline path is echo |
| 6 | 24/7 runtime & devices | **Built** | APNs/local notification delivery missing; real on-device wake word |
| 7 | Training & personalization | **Built** | Style/importance learning yes; **weight training still optional/provider-dependent** |
| 8 | Live data & sensors | **Built (v1)** | macOS collectors text-level only; no HealthKit / CoreLocation yet |
| 9 | Intelligence modules | **Built** | Cross-signal loop exists; starved without real health/calendar/live density |
| 10 | E.D.I.T.H. & advanced | **Built** | Real device task executors, maps, OctoPrint, AR surfaces later |
| 11 | Tools & actions | **Built** | Sandbox is process jail (not OS/container); per-call approval UI |
| 12 | Security & privacy | **Built / partial enc** | TLS is deployment-side; full-disk encryption / key ceremony ops |
| 13 | Clients & UX | **Built (v1)** | Swift package ≠ shippable iOS/Watch app; HUD targets Future |
| 14 | Ops & evaluation | **Built (v1)** | Seeded corpus evals, EER curves, structured log dashboards |
| 15 | Perception & multimodal | **Built** | On-device OCR/vision adapters; face-free identity hints |
| 16 | Identity & trust | **Built** (16.4 Future) | Biometric unlock; encrypted identity backup drill |
| 17 | Legal & biometric compliance | **Built** | Regulator-facing privacy statement in-app |
| 18 | Integrations & ecosystem | **Built (v1)** | Adapters registered; **real OAuth calendar/GitHub flows still thin** |
| 19 | Routines & automations | **Built** | Learned-sequence templates; notification delivery for runs |

**Critical reframe:** WORK_BREAKDOWN "Built" means *contract + pipeline +
tests exist*. It does **not** mean production engines, App Store surface, or
months of personal corpus quality.

---

## 2. What you must stop doing

These feel productive after a 19-domain build and usually destroy momentum:

1. **Starting new domains or modules** before a real daily-driver week.
2. **Filling every Direction: line** — 84 refinements are a backlog, not a
   sprint. Prioritize by personal friction only.
3. **LoRA / weight training** before you have a dense, consented corpus of
   rated + corrected replies (months, not days).
4. **Multi-user, AR glasses, OctoPrint, maps** — explicitly Future / late
   ROADMAP; they do not unstick daily use.
5. **Parallel agent fan-out across all 19 domains** without a single merge
   owner — you already have uncommitted cross-cutting work that must land
   cleanly first.
6. **Treating mock voice as "voice done"** — the lifecycle is real; the
   ears and mouth are still test doubles.

---

## 3. Immediate operating order (this week)

### Step 0 — Stabilize the tree (gate for everything else)

Working tree is **ahead of origin by 1 commit** with a large uncommitted
hardening delta (~26 files): required `EV_VAULT_KEY`, vault rotation,
e2e_cli expansion, compose/runtime daemon, docs, sandbox notes, etc.

Do this before any feature work:

```text
1. Review the uncommitted diff as one intentional "ops/security hardening" unit
2. Keep the suite green: make lint && make typecheck && make test && make eval
3. Commit with a message that states the vault-key boundary and e2e proof
4. Push when you are ready to publish (not forced)
```

**Known fix already required (and applied in-tree):** the initial Alembic
migration must only run `CREATE EXTENSION vector` on PostgreSQL. An
unguarded `op.execute(...)` breaks SQLite migration tests (Domain 14 brief).

**Acceptance for Step 0:**

- `uv run ruff check app clients tests` clean
- `uv run mypy app clients` clean
- `uv run pytest -q` green
- `uv run python -m app.scripts.eval_gates --report eval/last-run.json` exit 0
- Uncommitted hardening is either committed or explicitly parked

### Step 1 — Stand up the always-on personal stack

This is the first product, not the twentieth feature.

```text
cp .env.example .env
# set EV_MASTER_KEY, EV_VAULT_KEY (min 16), EV_BACKUP_PASSPHRASE
# optional: EV_DEEPSEEK_API_KEY, EV_EMBEDDING_API_KEY
make install && make compose-up && make migrate
curl http://localhost:8000/v1/health
ev identity owner          # recovery codes printed once — store offline
ev onboarding "…"          # first memories + audit
ev capture / ev ask        # core loop
```

Then:

1. Install **Tailscale** on Mac + phone; reach API via Tailscale IP only.
2. Put **Caddy/nginx TLS** in front (see DEPLOYMENT.md) — do not expose :8000.
3. Schedule **nightly encrypted backup** + a calendar reminder for a
   monthly wipe→restore drill.
4. Leave the stack up 24/7 for at least 7 days. Measure uptime, not features.

### Step 2 — Two-week dogfood protocol (highest leverage)

Use EV as the only place you put personal decisions for 14 days.

| Day habit | Command / surface | What you are measuring |
| --- | --- | --- |
| Morning | `ev card` / web HUD + morning routine | Latency, emptiness vs usefulness |
| Capture | CLI/web every decision, preference, project note | Extraction quality, duplicates |
| Ask | "why did I decide X?", "what was I thinking about Y?" | Provenance honesty |
| Correct | `ev correct` / forget when wrong | Correction rate (training gold) |
| Audit | `ev audit <id>` after surprising answers | Trust calibration |
| Weekly | `GET /v1/recall/week` | Whole-life recall feel |
| End of week | `ev export` + restore dry-run | Survival property |

Keep a **friction log** (plain markdown is fine):

```text
date | surface | intent | what broke or felt dumb | severity 1-5 | domain#
```

After 14 days, **sort the friction log by severity × frequency**. That sorted
list *is* your next sprint backlog. Discard WORK_BREAKDOWN Direction items
that never appeared in friction.

---

## 4. Strategic backlog (ordered after dogfood starts)

Work these in order. Each has an exit gate. Do not start N+1 until N's gate
passes unless blocked on hardware/keys.

### Track A — Presence (turn the system into something that is *there*)

| Priority | Work | Why first | Exit gate |
| --- | --- | --- | --- |
| A1 | **Real ASR** behind `Transcriber` (faster-whisper local *or* OpenAI-compat Whisper) | Text-only companion is a note app | `ev`/web voice utterance → memory event with real transcript; lifecycle tests still green offline |
| A2 | **Real TTS** with style mapping already in contracts | Closes the reply loop | Spoken reply returns audio_ref; quiet-hours still gate |
| A3 | **Real speaker verification** (SpeechBrain ECAPA or on-device) replacing default `ProfileSpeakerVerifier` | Owner-only is the trust spine | Enroll ≥5 samples; unknown voice refused; enrollment still consent + liveness gated |
| A4 | **Wake engine** with real KWS (Porcupine / local) when you have a always-on mic path | Otherwise runtime is API-only | Wake → verify → listen works on one device with offline CI fallback |
| A5 | **Notification delivery** (at least macOS local + later APNs) | Without delivery, routines/alerts/EV Sense are dashboard toys | Quiet-hours digest reaches you without opening the app |

Agent **A1 (Voice Reality)** in [`AGENT_BRIEFS.md`](AGENT_BRIEFS.md) is the
authoritative work order for Track A1–A4 (maps historical Domain 5).

### Track B — Surface (put capture where life happens)

| Priority | Work | Why | Exit gate |
| --- | --- | --- | --- |
| B1 | **iOS app shell** around `EVClient`/`EVUI`: continuous chat, capture, memory browser, offline queue indicator | Phone is the primary capture device | FR-DEV-01 vertical demo from DEMO.md §3 |
| B2 | Share sheet + attachment capture | Frictionless memory | PDF/image → event → ask about it |
| B3 | Offline queue under airplane mode | Trust multi-device | DEMO.md M2 offline steps |
| B4 | Watch quick capture + today card (after B1 stable) | Suit surface | DEMO.md Watch step |
| B5 | APNs via your backend (after Tailscale path solid) | Closes proactive loop | Alert reaches Lock Screen |

Do **not** start Watch/AR until B1–B3 feel boring.

### Track C — Density (feed the intelligence organs real signal)

Intelligence modules (Domain 9) and EV Sense are architecturally complete but
**data-starved**. Priority connectors:

| Priority | Integration | Why it unlocks the most intelligence |
| --- | --- | --- |
| C1 | **Calendar** (read-only first) via adapter + vault | Deadlines, leave-by, tactical prep, quiet hours truth |
| C2 | **HealthKit → health snapshots** (read-only, sensitive) | Health radar + morning brief become real |
| C3 | **macOS collector always-on** (screen app/window, coarse presence) | User state + live context stop being empty |
| C4 | GitHub or one project source you actually use | Alert radar + research sessions earn keep |
| C5 | Web search key (Brave/SerpAPI) only if research is weekly work | Citations path already built |

One excellent calendar integration beats five half-adapters.

### Track D — Quality of mind (deepen the moat)

| Priority | Work | Exit gate |
| --- | --- | --- |
| D1 | Seed a **personal retrieval eval set** (20–50 questions with expected memory ids) | top-5 ≥80% on *your* corpus (EVALUATION.md) |
| D2 | LLM-assisted extraction behind existing interface (keep rules for offline) | Better typed memories on messy natural language; rebuild invariant still holds |
| D3 | Surface conflicts + provenance chips in every chat answer by default | "why do you know that?" never requires a second command |
| D4 | Filter ledger monthly review → apply recalibration only when evidence wins | Training track 7.5 used deliberately, not auto-magically |
| D5 | ContextCompiler progressive deep-dives via tools | Long hard questions stop under-retrieving |

### Track E — Hardening that matches lifelong data

| Priority | Work | Exit gate |
| --- | --- | --- |
| E1 | Monthly **wipe → restore drill** on a spare volume | Counts + sample audit match (test_backup already models this) |
| E2 | Identity recovery drill (lose device token → recovery codes → re-enroll voice) | Fleet **A7** brief / Domain 16 scenario green |
| E3 | Compliance sweep on a schedule you trust (`EV_COMPLIANCE_SWEEP_HOURS`) | Erasure of voiceprints + corpus verified |
| E4 | Threat-model pass against SECURITY.md with real Tailscale topology | Written residual risks accepted or fixed |
| E5 | Document personal runbook: keys location, restore steps, who can help if you are locked out | One page you could follow half-asleep |

---

## 5. Sequencing diagram (recommended 8–10 weeks)

```text
Week 0     Stabilize tree · green suite · commit hardening
Week 1     Always-on compose · Tailscale · identity · onboarding · backups
Week 1–3   Dogfood (CLI + web only) · friction log
Week 2–4   Track A1–A2 (real ASR/TTS) in parallel with dogfood
Week 3–5   Track B1–B3 (iOS shell + offline)
Week 4–6   Track C1–C3 (calendar, health, collector)
Week 5–7   Track D1–D3 (personal eval, extraction, provenance UX)
Week 6–8   Track A3–A5 + B5 (speaker, wake, notifications)
Week 8+    Only then: Watch, maker sources, maker/OctoPrint, maps, AR schemas on hardware
```

ROADMAP.md's M0→M5 order is still correct historically. You are past M0–M1
and most of M3–M5 *as code*. The calendar above is **post-M5 productization**,
not a rewrite of ROADMAP.

---

## 6. Decision discipline (from DECISIONS.md)

Still open; resolve only when a track needs them:

| When needed | Decision | Recommended default |
| --- | --- | --- |
| Track A | D-04 Voice stack | On-device/local Whisper; provider TTS with local fallback |
| Track C health | D-02 Health scope | Read-only HR/HRV/sleep/activity; `sensitive` storage |
| Track C research | D-03 Web research | User-supplied search key; no key = memory-only |
| Track B push | D-08 Notifications | APNs via private path; in-app + Watch haptics later |
| Always | D-06 Model | DeepSeek (or local) behind gateway; never bake identity into the model |
| Defer | D-14 Autonomy P9 | Post product-market-for-one; approval logs first |
| Defer | D-05 AR | HUD schemas now (done); hardware later |

Write the choice into `docs/DECISIONS.md` the day you pick it.

---

## 7. How to work day-to-day (sophisticated cadence)

### Merge rules

- One vertical slice per PR/commit cluster: *engine behind existing contract*,
  not new endpoints without a consumer.
- CI gates remain non-negotiable: lint · mypy · pytest · eval_gates.
- Prefer swapping a provider behind `contracts.py` / adapter registry over
  rewriting lifecycle code.

### Definition of done (post-breadth)

A change is done only when **all** of these hold:

1. Offline CI still green with dev/mock providers.
2. Real provider path documented in ENVIRONMENT.md (keys, models, ports).
3. WORK_BREAKDOWN status/Direction updated for that factor only.
4. You used the feature yourself at least once on the personal stack.
5. Friction log entry closed or refiled.

### Metrics that matter (instrument lightly)

| Metric | Healthy signal |
| --- | --- |
| Captures / day (7-day avg) | Rising then stable — means habit formed |
| Correction rate | Falling after week 2 |
| Chat answers with ≥1 provenance chip | >80% of memory questions |
| Restore drill age | < 35 days |
| Eval gate failures on main | 0 |
| p95 chat first-token / quickcard | Within eval budgets |
| Unknown-voice false accepts | 0 (hard fail) |

Ignore vanity: endpoint count, LOC, domain count.

---

## 8. Anti-goals (explicit)

- **Not** a multi-tenant SaaS.
- **Not** open-internet exposure of the API.
- **Not** stranger surveillance / ambient raw camera/mic to the model.
- **Not** silent weight updates or silent filter policy changes.
- **Not** dependence loops or fabricated intimacy (BEHAVIOR.md / ethics).
- **Not** replacing the immutable event log with "editable chat history."

If a shiny idea violates these, it is out of scope regardless of coolness.

---

## 9. If energy is limited — the 20% that yields 80%

Do only this sequence:

1. Green tree + personal compose stack + identity + backup passphrase.
2. Dogfood with CLI/web for 14 days; keep friction log.
3. Real ASR (local Whisper) + one phone capture path (even a thin iOS shell).
4. Calendar read integration.
5. Personal 30-question retrieval eval; fix top misses in extraction/retrieval.

Everything else can wait until those five feel ordinary.

---

## 10. Relationship to existing docs

| Doc | Role going forward |
| --- | --- |
| `WORK_BREAKDOWN.md` | Factor truth + Direction backlog (do not inflate domains) |
| `ROADMAP.md` | Historical milestone structure; exit gates still useful |
| **`AGENT_FLEET.md`** | **Multi-agent SSOT:** roster 1–15, exclusive paths, merge, bans, done-when |
| **`AGENT_LAUNCH.md`** | **Elite paste-ready messages** — send in order 1→15 |
| **`AGENT_BRIEFS.md`** | Historical domain briefs; prefer AGENT_LAUNCH for fan-out |
| `DEMO.md` | Acceptance scripts — run them on the personal stack, not only CI |
| `DECISIONS.md` | Record choices when tracks force them |
| `DEPLOYMENT.md` / `ENVIRONMENT.md` | Living ops truth for the always-on host |
| **This file** | Phase strategy after breadth (dogfood, tracks A–E); agents execute via fleet plan |

Update this file when a track's exit gate flips or dogfood forces reprioritization.

---

## 11. Bottom line

You did not build a half-finished assistant. You built a **complete personal
intelligence substrate** with honest contracts, rebuildable memory, a filter
that owns identity, and surfaces that can grow.

The sophisticated next step is almost boring on purpose:

> **Run it as your life’s second brain for two weeks, then deepen only what
> daily use proves is weak — starting with real voice, a real phone capture
> path, and one dense integration (calendar).**

Breadth is complete. Depth, density, and presence are the work now.
