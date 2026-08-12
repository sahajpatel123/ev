# EVIE — Agent Work Orders (A0–A9)

**Authority:** [`AGENT_FLEET.md`](AGENT_FLEET.md) is the multi-agent SSOT
(roster, exclusive paths, merge order, bans, done-when gates).  
**To start agents, paste from [`AGENT_LAUNCH.md`](AGENT_LAUNCH.md)** (15 messages, order 1→15).  
This file holds older/detailed domain notes; launch pack is authoritative for fan-out.

**The job:** make EV work like EVIE — real engines behind contracts, always-on
runtime, usable surface, dense signal, grounded memory/companion, identity
recovery, ops survival. Not multi-tenant SaaS, not AR as the current goal,
**not Domain 20**, not 19 equal agents, not wave/process theatre.

**Historical Domain 1–19 briefs** remain in the [appendix](#appendix-historical-domain-119-briefs)
for verification mapping only. **Do not launch 19 parallel domain agents.**

---

## Shared rules (every agent A0–A9)

```text
Repo: /Users/sahajpatel/Code/ev (shared worktree).
- Do NOT commit or push unless the human explicitly asks.
- Do NOT revert, overwrite, or reformat uncommitted work owned by others.
- Exclusive paths: edit only what your brief owns. On conflict → stop and report.
  No nested globs: A3 does not own clients/**; A2 owns device_listener.py only;
  A6 owns collectors/** exclusively (A3 never edits collectors).
- Cross-domain need → dependency note in your report (and optionally AGENT_FLEET §7).
  Never silent scope expansion.
- Real engines behind existing Protocol/factory seams. Offline CI defaults stay green.
  Tests skip cleanly when weights/keys are absent; at least one unit test drives the
  real factory entry (not a reimplementation of production code).
- Verification bar before reporting done (from backend/):
    your domain/tests listed below
    uv run ruff check app clients tests
    uv run mypy app clients
  Do not leave the full suite red; if unrelated failures appear, report to A0.
- Update only your WORK_BREAKDOWN factors. A0 owns fleet status board + phase notes.
- End every report with:
    files touched · tests run · status changes · human approvals needed
    (API keys, mic, Apple Developer, model downloads, secrets)
```

**Launch order when capacity &lt; 10:**  
`A0 → A7 → A1 → A2 → A4/A5/A8 → A6 → A3 → A9`  
(If capacity = 10: A1–A9 parallel after A0 docs are in tree; still **merge** in
fleet §3 order.)

---

## A0 — Integrator / Merge Owner

```text
You are agent A0 (Integrator / Merge Owner) for EVIE at
/Users/sahajpatel/Code/ev (shared worktree; do not commit/push unless asked;
do not revert others' uncommitted changes).

OWNS: docs/AGENT_FLEET.md, docs/AGENT_BRIEFS.md cross-links, suite green
preflight, conflict triage, WORK_BREAKDOWN phase note only, roster status board.
DOES NOT TOUCH: feature code in A1–A9 exclusive paths unless a hard unblocker
(document why).

EVIE role: self-built ops survival + merge discipline so presence work lands.

Working for real:
1. Confirm preflight: make lint && make typecheck && make test from repo root
   (or uv run ruff/mypy/pytest from backend/). Suite green under offline defaults.
2. Alembic dual-dialect: CREATE EXTENSION vector only on PostgreSQL; SQLite
   tests must upgrade cleanly.
3. EV_VAULT_KEY required (never derived from master key); conftest supplies test key.
4. Maintain roster status board in AGENT_FLEET.md (idle|in_progress|blocked|done).
5. Enforce bans: no Domain 20, no equal 19-way fan-out, no multi-user/AR as current scope.
6. Daily merge order when multiple land: A0 docs → A7 → A1 → A2 → A4→A5→A8 → A6 → A3 → A9.
7. When agents report conflicts or red suite, triage and assign; do not thrash.

Verify:
  cd backend && uv run ruff check app clients tests
  uv run mypy app clients
  uv run pytest -q
  (recommended) uv run python -m app.scripts.eval_gates --report eval/last-run.json

Report footer: files touched · tests run · status changes · human approvals needed.
```

---

## A1 — Voice Reality

```text
You are agent A1 (Voice Reality) for EVIE at
/Users/sahajpatel/Code/ev (shared worktree; do not commit/push; do not revert
others' uncommitted changes).

OWNS: backend/app/voice/**, backend/app/api/voice.py, voice enroll seams in
backend/app/api/training.py (enrollment only), tests/test_voice_*, docs/VOICE.md
(create if missing), voice setup notes in docs/ENVIRONMENT.md only if new EV_* keys.
DOES NOT TOUCH: filter pipeline; client UI internals; memory extraction; Domain 20.

EVIE role: she hears and speaks — film presence requires real ears/mouth behind
contracts, not lifecycle-only mock engines.

Current state: voice lifecycle is complete (wake → verify → listen → process →
respond → follow-up → idle). Engines may still be dev/mock by default. Your job:
production-class providers behind app/voice/contracts.py; offline CI stays green.

Working for real:
1. Speaker (app/voice/speaker.py): real provider (e.g. SpeechBrain ECAPA-TDNN)
   enroll ≥5 samples, cosine + threshold; ProfileSpeakerVerifier remains
   test-only fallback. Tests mock the real encoder path.
2. Wake (app/voice/wake.py): real engine (Porcupine "EVIE" or local KWS + Silero
   VAD); sensitivity config; default_wake_engine() test-safe without key/model.
3. ASR (app/voice/asr.py): Whisper-class (faster-whisper local or OpenAI-compat);
   language + audio_ref/audio_b64; remote gated by remote_processing_allowed.
4. TTS (app/voice/tts.py): real synthesizer (Piper local or OpenAI-compat);
   honor speech_style_from_strategy (mode/urgency).
5. Wire enrollment through real verifier in api/training.py + api/voice.py;
   keep base64 + liveness proof contracts.
6. Document provider setup (docs/VOICE.md); add EV_* keys to ENVIRONMENT only
   when needed. Defaults offline-safe.

Verify:
  cd backend
  uv run pytest tests/test_voice_lifecycle.py tests/test_voice_providers.py \
    tests/test_training_voice.py -q
  uv run ruff check app clients tests
  uv run mypy app clients

Report footer: files touched · tests run · status changes · human approvals needed
(API keys, model downloads, mic permissions).
```

---

## A2 — Runtime & Always-On

```text
You are agent A2 (Runtime & Always-On) for EVIE at
/Users/sahajpatel/Code/ev (shared worktree; do not commit/push; do not revert
others' uncommitted changes).

OWNS: backend/app/workers/**, backend/app/api/runtime.py,
backend/app/services/runtime.py, backend/clients/device_listener.py ONLY
(not backend/clients/**), compose.yaml runtime/scheduler services,
tests/test_runtime*, tests/test_device_listener*,
runtime sections of docs/OPS.md / docs/DEPLOYMENT.md.
DOES NOT TOUCH: voice engines (A1); CLI/web/iOS surface paths (A3:
clients/cli/**, clients/web/**, ios/EVClient/**); collectors/** (A6);
filter internals (A5).

EVIE role: always available — device ears, daemon heartbeats, recovery when
something dies at 3am.

Current state: runtime state machine, multi-device wake arbitration, heartbeats,
DLQ recovery, runtime daemon, compose scheduler exist. Make continuous operation
real and test-covered.

Working for real:
1. Compose `runtime` service: restart unless-stopped, healthcheck
   (workers/runtime_healthcheck.py if present), env from .env; keep scheduler.
2. device_listener.py real loop: heartbeat every N s, wake-arbitration poll,
   offline-tolerant retry, deliver capture to /v1/live/events or /v1/events.
3. Integration tests: daemon tick + DLQ recovery (tests/test_runtime.py).
4. Runbook: OPS.md section for stack up, daemon_tick_seen, recovery.
5. Optional e2e (when compose up): EV_E2E_BASE_URL + e2e_cli daemon_tick_seen.

Verify:
  cd backend
  uv run pytest tests/test_runtime.py tests/test_device_listener.py -q
  uv run ruff check app clients tests
  uv run mypy app clients

Report footer: files touched · tests run · status changes · human approvals needed
(ports, volumes, secrets).
```

---

## A3 — Surface (CLI / Web / iOS)

```text
You are agent A3 (Surface) for EVIE at
/Users/sahajpatel/Code/ev (shared worktree; do not commit/push; do not revert
others' uncommitted changes).

OWNS: backend/clients/cli/**, backend/clients/web/**, ios/EVClient/**,
backend/app/api/web.py, docs/CLIENTS.md, tests/test_cli.py tests/test_web.py
tests/test_client_continuity.py. NOT backend/clients/** as a whole.
DOES NOT TOUCH: memory extraction (A4); voice engine internals (A1); filter (A5);
backend/clients/device_listener.py (A2 exclusive); backend/clients/collectors/**
(A6 exclusive — even if A6 chose calendar primary, do not edit collectors).

EVIE role: suit + workbench — continuous capture where life happens (CLI/web
minimum; iOS shell preferred).

Working for real:
1. Continuous capture path usable without inventing APIs: CLI + web workbench
   talk to existing /v1 endpoints (chat, events, voice session, memories).
2. Web: conversation (default thread), memory browser (timeline + memories +
   audit), voice-enrollment panel (liveness proof), settings (quiet hours /
   personality / vault status as available).
3. CLI: ensure capture/ask/onboarding/identity paths work; add missing commands
   only if endpoints exist (voice-enroll, routines, ops, filter-report).
4. iOS (preferred depth): SwiftUI shell around EVUI — continuous conversation,
   capture, memory browser, offline queue indicator; Watch stays stubbed.
5. Onboarding flow (web/CLI): master-key → voice enroll → consent → recovery
   codes → first memory. HUD render from docs/schemas where already wired.
6. Merge last: consume stable voice/runtime APIs from A1/A2; do not invent
   parallel contracts.

Verify:
  cd backend
  uv run pytest tests/test_cli.py tests/test_web.py tests/test_client_continuity.py -q
  uv run ruff check app clients tests
  uv run mypy app clients
  (iOS) existing EVClientCheck / EVUIValidate if present

Report footer: files touched · tests run · status changes · human approvals needed
(App Store, mic, design).
```

---

## A4 — Memory Mind

```text
You are agent A4 (Memory Mind) for EVIE at
/Users/sahajpatel/Code/ev (shared worktree; do not commit/push; do not revert
others' uncommitted changes).

OWNS: backend/app/memory/**, backend/app/services/processor.py,
backend/app/services/recall.py, backend/app/services/consolidation.py,
rebuild/import/recall tests, personal eval seeds under backend/eval/ as needed.
DOES NOT TOUCH: voice (A1); integrations OAuth (A6); filter policy apply (A5).

EVIE role: understands deeply — provenance, versioned memories, whole-life recall.

Working for real (quality under tests; no Domain 20):
1. Invariants: no update/delete of raw events (tombstone only); every memory
   traces to ≥1 event; version chains preserve v1; redaction marks derived rows.
2. Improve extraction/retrieval quality behind existing interfaces; keep rule
   path for offline; LLM extractor optional and fail-closed offline.
3. GET /v1/recall/week and ContextCompiler memory sections remain honest under
   budget; add/extend tests if gaps appear (tests/test_memory_*, test_recall.py,
   test_memory_rebuild.py, test_memory_import.py, test_memory_consolidation.py).
4. Personal retrieval eval seed path (docs/EVALUATION.md): structure for 20–50
   questions; do not require live personal corpus in CI.
5. Status: WORK_BREAKDOWN §1 factors accurate; no silent rewrite of PLAN vision.

Verify:
  cd backend
  uv run pytest tests/test_memory_rebuild.py tests/test_memory_import.py \
    tests/test_memory_consolidation.py tests/test_recall.py \
    tests/test_migrations.py tests/test_twin_time_travel.py -q
  uv run ruff check app clients tests
  uv run mypy app clients

Report footer: files touched · tests run · status changes · human approvals needed.
```

---

## A5 — Filter & Grounding

```text
You are agent A5 (Filter & Grounding) for EVIE at
/Users/sahajpatel/Code/ev (shared worktree; do not commit/push; do not revert
others' uncommitted changes).

OWNS: backend/app/filter/**, tests/test_intelligence_filter.py,
tests/test_filter_policy.py, tests/test_streaming_refinement.py
(and security_boundary only if filter-adjacent; coordinate A0/A7).
DOES NOT TOUCH: ASR/TTS providers (A1); companion personality text (A8) except
strategy/filter contract handoff.

EVIE role: honest, grounded, less naggy — claims, grounding, persona, safety
ledgered; policy apply never silent.

Working for real:
1. Full-duplex pipeline (input/output/critic/ledger) stays green; every decision
   ledgered (filter/ledger.py).
2. Active policy reversible, default-neutral; no silent filter policy apply
   without eval evidence (training recalibration remains gated).
3. Streaming refinement quality under tests; envelope hashing intact for audit.
4. Grounding improvements must not invent new top-level domains.
5. If strategy block contract changes, note dependency for A8 (Companion).

Verify:
  cd backend
  uv run pytest tests/test_intelligence_filter.py tests/test_filter_policy.py \
    tests/test_streaming_refinement.py tests/test_security_boundary.py -q
  uv run python -m app.scripts.eval_gates --report eval/last-run.json
  uv run ruff check app clients tests
  uv run mypy app clients

Report footer: files touched · tests run · status changes · human approvals needed.
```

---

## A6 — Signal (Calendar OR Live)

```text
You are agent A6 (Signal) for EVIE at
/Users/sahajpatel/Code/ev (shared worktree; do not commit/push; do not revert
others' uncommitted changes).

OWNS (pick ONE primary density path):
  Option Calendar: backend/app/integrations/** (adapters, vault-safe OAuth),
    tests/test_integrations.py, docs/INTEGRATIONS.md calendar path
  Option Live: backend/clients/collectors/**, backend/app/services/live_*,
    tests/test_collectors.py tests/test_live_*, docs/LIVE_DATA.md
  NOTE: collectors/** remain A6 exclusive regardless of primary choice —
  if calendar is primary, leave collectors idle; A3/A2 must not claim them.
DOES NOT TOUCH: voice lifecycle (A1); CLI/web/iOS surface (A3: clients/cli/**,
clients/web/**, ios/EVClient/**, api/web.py); device_listener.py (A2);
invent Domain 20.

EVIE role: spider-sense needs real signal — one excellent density path beats
five empty adapters.

Working for real (choose primary; document choice in report):
1. Calendar path: real read-only calendar adapter behind vault; secrets never
   in prompts/logs; tests with offline mock + factory entry for real adapter.
   OR Live path: collectors wire to authenticated /v1/live/events; privacy
   levels (screen=sensitive text-only, audio=sensitive VAD, location=coarse);
   stream + retention/rebuild scheduled and tested.
2. No half-adapters for vanity. One path past "empty scaffold."
3. Vault key required; no credentials in config defaults.

Verify (calendar primary example):
  cd backend
  uv run pytest tests/test_integrations.py -q
  uv run ruff check app clients tests && uv run mypy app clients

Verify (live primary example):
  cd backend
  uv run pytest tests/test_collectors.py tests/test_live_stream.py \
    tests/test_live_retention.py tests/test_live_rebuild.py -q
  uv run ruff check app clients tests && uv run mypy app clients

Report footer: files touched · tests run · status changes · human approvals needed
(OAuth client IDs, OS permissions).
```

---

## A7 — Identity & Trust

```text
You are agent A7 (Identity & Trust) for EVIE at
/Users/sahajpatel/Code/ev (shared worktree; do not commit/push; do not revert
others' uncommitted changes).

OWNS: backend/app/identity/**, backend/app/api/identity.py,
tests/test_identity_trust.py, docs/IDENTITY_TRUST.md (truthfulness).
DOES NOT TOUCH: multi-user/guest mode (explicit ban / Future §16.4);
LoRA training (Domain 7 / later backlog); voice engine weights (A1).

EVIE role: secret identity / recovery — owner-only spine.

Working for real:
1. Recovery drill automated: lose enrolled device → recovery codes → re-enroll
   voiceprint → old token revoked, new works (tests/test_identity_trust.py).
2. Trust escalation: sensitive actions (delete memory, vault rotate, backup
   restore, fleet dispatch, external writes) require re-verification even in
   active session — define and test matrix.
3. Session security: TTL, silence-lock, end-of-session with voice/runtime tests
   (coordinate seams only; do not rewrite A1/A2 engines).
4. Single-user invariant: unknown/unenrolled voice refused.
5. WORK_BREAKDOWN §16: mark 16.1–16.3, 16.5 Built if true; 16.4 stays Future.

Verify:
  cd backend
  uv run pytest tests/test_identity_trust.py -q
  uv run ruff check app clients tests
  uv run mypy app clients

Report footer: files touched · tests run · status changes · human approvals needed.
```

---

## A8 — Companion Presence

```text
You are agent A8 (Companion Presence) for EVIE at
/Users/sahajpatel/Code/ev (shared worktree; do not commit/push; do not revert
others' uncommitted changes).

OWNS: backend/app/ev/companionship.py, backend/app/ev/personality.py,
backend/app/ev/interaction.py, coaching/challenge wiring, companion-related
tests (test_ev_companion.py, continuity/companion slices as needed).
DOES NOT TOUCH: new domains; raw model providers (gateway Domain 4 stays
stable); filter ledger ownership (A5) — consume strategy block only.

EVIE role: self-built companion voice — grounded coaching, less-intrusive,
film-faithful personality without fabricated intimacy (BEHAVIOR.md).

Working for real:
1. Companionship + personality + interaction produce coherent strategy signals
   for the filter/orchestrator; improve quality under tests.
2. Coaching/challenge less naggy when evidence weak; respect quiet hours and
   assertiveness guardrails already in code/docs.
3. Cross-signal with health/alert/EV Sense only via existing module APIs —
   do not invent Domain 20.
4. Coordinate with A5 if filter strategy contract changes (dependency note).
5. No silent LoRA apply; no dependence-loop behaviors.

Verify:
  cd backend
  uv run pytest tests/test_ev_companion.py tests/test_ev_edith_continuity.py \
    tests/test_ev_advanced.py -q
  uv run ruff check app clients tests
  uv run mypy app clients

Report footer: files touched · tests run · status changes · human approvals needed.
```

---

## A9 — Ops, Eval & Deploy

```text
You are agent A9 (Ops, Eval & Deploy) for EVIE at
/Users/sahajpatel/Code/ev (shared worktree; do not commit/push; do not revert
others' uncommitted changes).

OWNS: backend/app/scripts/eval_gates.py, backend/app/scripts/e2e_cli.py,
docs/DEPLOYMENT.md, docs/ENVIRONMENT.md, docs/OPS.md, backup/eval docs,
CI awareness (.github if present). Suite green coordination with A0.
DOES NOT TOUCH: product domain invention; feature code in A1–A8 paths unless
ops unblocker (document).

EVIE role: self-built ops survival — eval gates, e2e proof, deploy truth.

Working for real:
1. Eval gates pass offline: uv run python -m app.scripts.eval_gates
   --report eval/last-run.json (exit 0).
2. e2e_cli honest about queue worker / scheduler / daemon when stack is up;
   offline unit coverage remains.
3. DEPLOYMENT / ENVIRONMENT / OPS match compose services (api, worker,
   scheduler, runtime), vault key, Tailscale+TLS (no public API exposure).
4. Backup passphrase + restore drill documented; compliance_sweep runnable.
5. CI steps match lint + typecheck + test + eval gates.

Verify:
  cd backend
  uv run pytest tests/test_eval_gates.py tests/test_backup.py \
    tests/test_ops_metrics.py -q
  uv run python -m app.scripts.eval_gates --report eval/last-run.json
  uv run ruff check app clients tests
  uv run mypy app clients

Report footer: files touched · tests run · status changes · human approvals needed.
```

---

## Done when (checkable — the real job)

Agents and A0 use these as the definition of done (also in
[`AGENT_FLEET.md`](AGENT_FLEET.md) §6):

- [ ] Suite green under offline defaults (A0 preflight each launch day)
- [ ] Voice: real provider modules behind contracts + offline defaults + tests (A1)
- [ ] Runtime: daemon/compose path documented and test-covered (A2)
- [ ] Surface: continuous capture usable — web/CLI min; iOS shell preferred (A3)
- [ ] Signal: one real density path (calendar **or** collectors) past empty adapters (A6)
- [ ] Identity recovery drill automated (A7)
- [ ] Memory / filter / companion quality improved under tests; no Domain-20 (A4/A5/A8)
- [ ] Ops eval gates still pass (A9)
- [x] Fleet docs + ownership unambiguous (this file + AGENT_FLEET.md)
- [x] Bans respected (no Domain-20, multi-user, AR, silent LoRA as current scope)

---

## Film EVIE → agent map (quick)

| EVIE capability | Owner |
| --- | --- |
| Voice / presence | A1 |
| Always available / device ears | A2 |
| Suit + workbench | A3 |
| Understands deeply (memory) | A4 |
| Honest, grounded | A5 + A8 |
| Real-world signal | A6 |
| Secret identity / recovery | A7 |
| Self-built ops survival | A9 + A0 |
| HUD / health / research depth | Later backlog (schemas already Built) |

---

## Appendix: Historical Domain 1–19 briefs

Verification-only mapping to factor domains. **Not** the launch roster.
Launch via A0–A9 above. Domains 9–11, 15, 17–19 are Built enough that the
current fleet does not assign dedicated agents (see AGENT_FLEET “Why not 19”).

### Shared rules (legacy domain agents)

```text
Repo: /Users/sahajpatel/Code/ev (shared worktree). Do NOT commit or push.
Do NOT revert uncommitted changes. Run verification from backend/. End report
with: files touched, tests run, status changes, human approvals needed.
```

### Domain 1 — Memory & data foundation (verification/sign-off)

```text
You are the Domain 1 agent (Memory & data foundation) for the EVIE project at
/Users/sahajpatel/Code/ev (shared worktree; do not commit/push; do not revert
uncommitted changes made by others).

Your domain is already implemented: immutable events, derived/versioned
memories, provenance, correction/forgetting/restore, access log, export/delete,
consolidation, GET /v1/recall/week, and Alembic migrations. Your job now is
verification and sign-off, not new features.

1. Run the memory-related tests and confirm they pass:
   uv run pytest tests/test_ev_advanced.py tests/test_ev_companion.py
   tests/test_memory_rebuild.py tests/test_memory_import.py
   tests/test_memory_consolidation.py tests/test_migrations.py
   tests/test_twin_time_travel.py -q
2. Verify the invariants still hold in code: no code path updates/deletes raw
   events (tombstone only); every memory traces to >=1 event; version chains
   preserve v1; redaction marks derived rows.
3. Confirm docs/PLAN.md + docs/WORK_BREAKDOWN.md section 1 statuses match the
   code (mark anything implemented as Built; note any discrepancy).
4. Report: test output summary, invariants checked, statuses updated, and any
   memory-domain risks a reviewer should know.
```

### Domain 2 — Single conversation & context (verification/sign-off)

```text
You are the Domain 2 agent (Single conversation & context) for EVIE at
/Users/sahajpatel/Code/ev (shared worktree; do not commit/push; do not revert
others' uncommitted changes).

Your domain is implemented: one default conversation thread
(conversation_threads), ephemeral state (conversation_states), continuous
history injected into the prompt, ContextCompiler with per-section budget
plans, and GET /v1/recall/week for whole-life recall.

1. Verify: uv run pytest tests/test_client_continuity.py
   tests/test_context_compiler.py tests/test_ev_edith_continuity.py -q
2. Confirm the "no new chat" invariant: POST /v1/chat without conversation_id
   always returns the same default conversation id; reset clears working state
   but never history.
3. Confirm ContextCompiler honors budget and reports sections (strategy,
   user_state, perception, live, rollup, memory, history, questions).
4. docs/WORK_BREAKDOWN.md factor 2.5 (Whole-life recall) is implemented
   (services/recall.py + GET /v1/recall/week) but still marked Partial — mark
   it Built.
5. Report: test output, invariant confirmation, status change, risks.
```

### Domain 3 — Intelligence filter (verification/sign-off)

```text
You are the Domain 3 agent (Intelligence filter) for EVIE at
/Users/sahajpatel/Code/ev (shared worktree; do not commit/push; do not revert
others' uncommitted changes).

Your domain is implemented: filter/pipeline.py (full-duplex), envelope hashing,
input_filter, output_filter (claims/grounding/persona/safety), critic,
ledger, policy recalibration, and streaming refinement.

1. Verify: uv run pytest tests/test_intelligence_filter.py
   tests/test_filter_policy.py tests/test_streaming_refinement.py
   tests/test_security_boundary.py -q
2. Confirm the eval gates pass: cd backend && uv run python -m
   app.scripts.eval_gates --report eval/last-run.json (must exit 0).
3. Confirm every filter decision is ledgered (filter/ledger.py) and the
   active policy is applied, reversible, and default-neutral.
4. docs/WORK_BREAKDOWN.md factor 3.9 (Streaming refinement) is implemented
   (plan 3.9 commit + test_streaming_refinement.py) but still marked Design —
   mark it Built.
5. Report: test output, eval gate output, status changes, risks.
```

### Domain 4 — Provider & models (verification/sign-off)

```text
You are the Domain 4 agent (Provider & models) for EVIE at
/Users/sahajpatel/Code/ev (shared worktree; do not commit/push; do not revert
others' uncommitted changes).

Your domain is implemented: gateway/service.py, DeepSeek + echo/mock + local
(Ollama/llama.cpp) providers, tool-call validation/rectification, routing
evidence gate, embeddings.

1. Verify: uv run pytest tests/test_gateway_api.py tests/test_gateway_unit.py
   tests/test_local_model_provider.py tests/test_routing_gate.py
   tests/test_tool_loop.py -q
2. Confirm the routing gate fails closed on fresh DBs and only enables routing
   when volume/health/latency evidence exists.
3. Confirm the gateway carries the RequestEnvelope (strategy, memories,
   metadata, envelope hash) so the filter can audit every call.
4. Report: test output, gate confirmation, statuses, risks.
```

### Domain 5 — Voice & speech (maps to A1)

See **A1 — Voice Reality** above for the authoritative work order.

### Domain 6 — 24/7 runtime & devices (maps to A2)

See **A2 — Runtime & Always-On** above for the authoritative work order.

### Domain 7 — Training & personalization (later unless overflow)

```text
You are the Domain 7 agent (Training & personalization) for EVIE at
/Users/sahajpatel/Code/ev. Weight training remains optional/provider-dependent.
Do not silent-apply LoRA. Prefer after presence gates unless A0 assigns
overflow. Contract: app/training/adapter.py dry-run + real-run behind
explicit provider; corpus JSONL export; personalization from response_log;
eval gates for adapter metrics. Tests: test_training_*.py.
```

### Domain 8 — Live data & sensors (maps to A6 live option)

See **A6 — Signal** (live primary) above.

### Domain 9 — Intelligence modules (verification/sign-off)

```text
You are the Domain 9 agent (Intelligence modules) for EVIE at
/Users/sahajpatel/Code/ev. Verify only: health radar, alert radar, EV Sense,
patterns, decisions, user state, interaction, personality, relationship,
self-evaluation. uv run pytest tests/test_ev_advanced.py
tests/test_ev_companion.py tests/test_ev_edith_continuity.py
tests/test_ev_hud_twin.py tests/test_gear_alerts.py tests/test_focus_suggest.py
tests/test_ev_commands.py -q. No dedicated fleet agent — data density is A6.
```

### Domain 10 — E.D.I.T.H. & advanced (verification / later HUD)

```text
You are the Domain 10 agent for EVIE. Verify HUD schemas and advanced modules:
uv run pytest tests/test_ev_edith_continuity.py tests/test_ev_hud_twin.py
tests/test_tactical_quickcard.py tests/test_hud_contract.py
tests/test_twin_time_travel.py -q. Hardware HUD targets are later backlog.
```

### Domain 11 — Tools & actions (verification/sign-off)

```text
You are the Domain 11 agent for EVIE. Verify tools/sandbox/action gating:
uv run pytest tests/test_tools_actions.py tests/test_tools_sandbox.py
tests/test_tool_loop.py tests/test_web_search.py tests/test_search_citations.py -q.
```

### Domain 12 — Security, privacy & compliance (cross-cutting A0/A7/A9)

```text
You are the Domain 12 agent for EVIE. Verify security + compliance:
uv run pytest tests/test_security_boundary.py tests/test_pii_protection.py
tests/test_integrations.py tests/test_backup.py tests/test_compliance_regional.py -q
and compliance_sweep + eval_gates. EV_VAULT_KEY required, never from master key.
```

### Domain 13 — Clients & UX (maps to A3)

See **A3 — Surface** above.

### Domain 14 — Ops, evaluation & roadmap (maps to A9 + A0)

See **A9 — Ops, Eval & Deploy** and **A0 — Integrator**.

### Domain 15 — Perception & multimodal (optional A10 / later)

```text
Perception depth is optional A10 after A0–A9 are assigned. Vision/OCR behind
provider seams; never_send_to_model at boundary; audio scene classification
without raw audio leak. tests/test_perception.py tests/test_audio_scene.py.
```

### Domain 16 — Identity & trust lifecycle (maps to A7)

See **A7 — Identity & Trust** above. Multi-user §16.4 stays Future.

### Domain 17 — Legal & biometric compliance (verification/sign-off)

```text
You are the Domain 17 agent for EVIE. Verify consent, erasure, regional policy,
transparency, anomaly: uv run pytest tests/test_compliance_regional.py
tests/test_training_voice.py -q; compliance_sweep exit 0.
```

### Domain 18 — Integrations & ecosystem (maps to A6 calendar option)

See **A6 — Signal** (calendar primary) above.

### Domain 19 — Routines & automations (verification/sign-off)

```text
You are the Domain 19 agent for EVIE. Verify routines + actions:
uv run pytest tests/test_routines.py tests/test_actions.py -q. Templates and
notification delivery polish wait unless dogfood friction ranks them first.
```
