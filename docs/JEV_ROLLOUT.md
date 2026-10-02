# JEV rollout — Evie's non-coding decision lane

> **Superseded 2026-10-02 (owner-directed):** Evie now runs a single
> non-speech brain, `xiaomi/mimo-v2.6-flash` (OpenRouter), with
> `gpt-realtime-2.1-mini` as the speech/hearing model. JEV and Muse Spark are
> no longer selected in the active profile; their code paths remain dormant
> pending deletion. This document is kept as the JEV-era record.

Owner of this document: Agent 10 CORTEX (gateway), with cross-owner dependency
notes per `docs/AGENT_FLEET.md` §2 and `docs/FLEET_LAW.md`.

This is the authoritative plan **and completion record** for adding TypeSafe
**JEV 1.13** to EV. Every claim was verified with a command; re-verify before
trusting it.

---

## 0. Measured facts (2026-10-01)

| Fact | Value | How it was measured |
| --- | --- | --- |
| Model exists | `typesafe/jev-1.13` | `curl -s https://openrouter.ai/api/v1/models/typesafe/jev-1.13/endpoints` |
| Modality | `text->decisions` (text in, typed decisions out; **no prose guarantee**) | same |
| Context | 32,000; `max_completion_tokens` 28,800 | same |
| Price | $0.042 / 1M prompt, **$0 completion** | same |
| Sampling params | `supported_parameters: []` — EV sends none | same |
| Tool choice | `none`, `auto`, `required`, `function` all supported | same |
| Transport | OpenRouter native typed Decisions API (`POST /api/alpha/decisions`) with `Authorization: Bearer` and the exact `{model,state,questions}` body. **Live-verified 2026-10-01: 200 with typed answers + usage + cost receipt.** `/api/v1/chat/completions` is rejected for this model (HTTP 400). | owner's key, live probe |
| `/api/alpha/decisions` | **The real transport.** Live-verified 2026-10-01: `200` with the bearer API key and a valid `{model,state,questions}` body. An earlier empty-body probe returned 401 "No cookie auth credentials found", which was a probe artifact, not a cookie-only rule. | owner's key, live probe |
| `~typesafe/jev-latest` | alias, `text->decisions` | `/api/v1/models/~typesafe/jev-latest/endpoints` |
| `typesafe/jev-router` | text-output router running on Jev; **not** the owner-directed model | `/api/v1/models/typesafe/jev-router/endpoints` |
| Mouth lane | `gpt-realtime-2.1-mini` (`EV_OPENAI_REALTIME_MODEL`) | `backend/app/config.py`, `.env` |
| Code lane | `EV_CODE_MODEL=muse-spark-1.3-contributor` in both profiles | grep of profiles |
| JEV provider flag | `EV_JEV_ENABLED=false` by default; no profile enabled | `.env.example`, config |

## 1. Final architecture (as implemented)

JEV cannot generate prose, arbitrary JSON, or streaming tokens, so "JEV for
everything non-coding" is a **decision-first** architecture:

```
owner ask/voice transcript
  -> cognitive kernel finite decision (ModelGateway.decide, typed questions)
  -> validated typed choice (caller-owned enum; never fabricated stats)
  -> allowlisted deterministic handler (existing executor + policy + audit)
  -> spoken/written result (deterministic receipt or Mini mouth for voice)
```

| Lane | Config | Model | Provider | Notes |
| --- | --- | --- | --- | --- |
| Decision (text) | `EV_COGNITIVE_MODE=jev_kernel` + `EV_JEV_ENABLED=true` | `typesafe/jev-1.13` | `openrouter` | native typed decisions (`choice`/`score`/`noul`); one call answers every question |
| Code | `EV_CODE_MODEL=muse-spark-1.3-contributor` | Muse Spark Contributor | `meta_muse_spark` | never JEV |
| Mouth | `EV_OPENAI_REALTIME_MODEL=gpt-realtime-2.1-mini` | Realtime Mini | `openai-realtime` | speech only; no tools, no decisions |
| Perception | existing local engines | ASR/TTS/OCR/wake/vision | local | pixels never reach JEV; only derived text |

Resolver: `backend/app/gateway/roles.py` (`resolve_text_brain`,
`resolve_code_brain`, `resolve_voice_mouth`, `text_role_available`,
`chat_via_role`, `chat_structured_via_role`, `choose_with_jev`).

### Provider transport (`backend/app/gateway/openrouter_jev.py`)

- `decision_payload()` builds the sanitized typed representation
  `{model, state, questions}` for the gateway's privacy/cost/audit seam; the
  same body is what crosses the wire to the native Decisions endpoint.
- `decide()` POSTs it with the bearer key and parses the typed `answers`
  mapping (choice/score/noul), probabilities/confidence/legend when present,
  `usage` (input/output tokens + `cost`), and the response id.
- `probabilities`/`confidence`/`legend` are optional and validated only when
  the provider reports them; EV never fabricates statistics.
- Audit records the SHA-256 of the exact sanitized request body; raw state is
  never copied into metadata.
- Gates per request: `EV_JEV_ENABLED`, `EV_ALLOW_REMOTE_CHAT`, active
  revocable `chat_egress` consent, key, circuit breaker; raw media/data URLs,
  never-send markers, and model overrides refused locally.

### Kernel decision turn (`backend/app/cognitive/kernel.py`)

Under `jev_kernel`, `_muse_turn` asks one finite `kernel_action` choice
(currently `goal_status` vs `unsupported`). A `goal_status` answer runs the
existing read-only `goal.status` executor with evidence; everything else
returns an explicit unsupported/unavailable result. No write action is
reachable through JEV, and a working Spark key can never silently substitute.

### Degradation rule

`require_text_provider()` and `chat_structured_via_role()` fail clearly when
JEV owns the role; call sites catch `OpenRouterJevError` and fall back to
deterministic handling. The gateway refuses streaming for JEV (buffered
decisions only). A blocked/unavailable call is reported, never substituted.

### Live workspace audit (2026-10-01/02)

Measured against the running launchd services and the production Postgres
(`model_calls`, `events`), not prose:

- **The live server was not on JEV.** Process env: `EV_COGNITIVE_MODE=muse_kernel`,
  no `EV_JEV_ENABLED`; `model_calls` show `meta_muse_spark /
  muse-spark-1.3-contributor` serving turns at **13–15 s** per call, with 40 s
  outliers. JEV had never been enabled in the running profile.
- **Greetings were slow or empty.** The deterministic reflex matched only bare
  `hi/hello/hey/yo`; "how are you", "give your introduction", "what can you
  do" went to the 13–15 s model path. Event audit showed a greeting answered
  61 s after the utterance, an intro answered ~50 s later, and one turn
  answered with the kernel fallback "I can't think that through right now."
- **Fix (this tree):** `app/cognitive/reflex.py` now answers social
  ("how are you", "how's it going", ASR-prefixed variants), identity/intro
  ("who are you", "give your introduction", "what can you do"), thanks, and
  time-of-day greetings deterministically — no model call. Measured locally:
  `reflex:social` 1.1 ms, `reflex:identity` 0.0 ms, `reflex:greeting` 0.0 ms,
  `muse_turns == 0`. Substantive turns ("Hello, is my order ready?",
  "how are you going to fix this") still reach cognition (negative tests).
- **Still Spark-bound:** arbitrary knowledge questions (the Muse profile) and
  any long tool turn remain 13–15 s; the live path only plays a
  `thinking_filler` when `needs_deep_work()` is true. Under JEV those
  questions cannot be answered at all (decisions-only), so a chat prose lane
  remains an owner decision.
- **Deployment:** the running service predates the reflex fix. It goes live
  only through `scripts/deploy_production.sh` (owner authority).

---

## 2. Phase status

### Phase 0 — baseline (done)

- [x] Failures classified against a clean `HEAD` worktree, not prose
      (`test_tool_loop`, `test_eval_gates::test_api_contract_gate_passes`, and
      5 voice tests are pre-existing reds at HEAD).
- [x] The working tree was red at the start (missing `choose_with_jev` /
      `jev_decision_tool`, dead transport); now green for the lane.
- [ ] Reconcile unlocked `/v1/file-sandbox/*` contract ops — Agent 1.
- [ ] `eval_gates` active-profile parser false positive — Agent 20.
- [ ] Chat latency gate diagnosis — Agent 10/20.
- [x] External blockers recorded: GitHub Actions billing; Linux portability.

### Phase 1 — provider on the real API (done)

- [x] Real transport: native typed Decisions endpoint, no sampling
      parameters, buffered, receipt-based cost.
- [x] Typed decisions with caller-owned enums; gateway validation; optional
      stats never fabricated.
- [x] `ModelGateway.decide` privacy/cost/audit seam; SHA-256 of the sent body.
- [x] Egress + per-request consent + key + circuit gates; raw media and model
      overrides refused locally.
- [x] `scripts/smoke_jev.py` rewritten: catalogue probe is key-free and
      honest, provider checks SKIP (exit 2) without a key.

### Phase 1.5 — role resolver completion (done)

- [x] `_muse_turn` uses the decision path under `jev_kernel`; the clobbering
      Spark assignment is gone.
- [x] `luna_adapter._call_spark_intent` branches to JEV first (no Spark attempt
      that could silently win).
- [x] Structured/prose helpers fail closed under JEV instead of fabricating.
- [x] Mode precedence: single `cognitive_mode` value; `muse_kernel` untouched
      as rollback.

### Phase 2 — non-coding call sites (converted to real JEV decisions)

- [x] Every finite decision surface now chooses through JEV under
      `jev_kernel` (offline tests + live spot checks):
      - `spark_task` — life family / manner / focus / latest (4 typed questions);
      - `spark_act` — one `act` choice over the 10 turn classes;
      - `spark_look` — `look` / `recall` / `chat` camera choice;
      - `spark_phone` — one tool choice over `chat` + 29 Home Station tools,
        arguments filled deterministically from the owner's words;
      - `desk_meaning.interpret_owner_act` — `act` choice; list items extracted
        deterministically (`extract_inventory`), never invented;
      - `luna_adapter` — `route` + `operation` choices into `TurnIntent`;
      - `brain_file_runner` — JEV picks among deterministic plan candidates
        (or `none`); paths/content are never model-invented;
      - `cognitive/kernel` — the bounded `kernel_action` choice (Phase 4).
- [x] Generative text that a decisions model cannot produce stays deterministic
      under `jev_kernel`, never Spark: `laptop_files._intelligent_rewrite`
      (file bodies), `desk_meaning.spark_inventory` (invented list items).
- [x] `app/ev/vision.py` stays pixel-local; only derived text is eligible for
      JEV (provider refuses raw media).
- [x] Live spot check (owner key): "when did mom's text arrive" →
      `messages/lookup/when`; "text mom I'm late" → `life`; "open safari" →
      `computer`; "how are you" → `chat`; "what's the weather" →
      `get_weather`; "what's my goal" → `STATE_QUERY/GOAL_GET`; "find my notes"
      → `search` plan (`source=jev`).
- [x] Clear desk jobs keep their deterministic frames first; the JEV desk
      act is the fuzzy-phrasing fallback (`spark_desk_candidate` gate). Live
      `_jev_act` proof: "add milk and eggs to the shopping list" → `append`
      with deterministic items `["milk", "eggs"]`; "remind me to call dad
      tomorrow" → `set_reminder` tool goal.
- [ ] `app/memory/{curator,llm_extractor}.py`, `app/memory/life_archive/` —
      **ownership assignment required** (not listed in `AGENT_FLEET.md` §2).
- [ ] `app/services/runtime.py`, `app/scripts/preflight.py` availability copy
      — Agents 14/20.

### Phase 3 — voice (done for the lane)

- [x] `_voice_turn_model()` follows the decision role under `jev_kernel`
      (`typesafe/jev-1.13`), Spark under `muse_kernel`, else legacy provider.
- [x] ASR/TTS/wake engines untouched; mouth remains `gpt-realtime-2.1-mini`
      with no tools/decisions.
- [x] The live-voice session path already funnels through the kernel decision
      surface (no second reasoning planner added).

### Phase 4 — computer use (done for the bounded read-only path)

- [x] Reuses the existing tool registry, executor, policy, and audit — no
      second registry or job framework.
- [x] JEV answers one bounded choice; only the allowlisted deterministic
      handler runs; `unsupported` is explicit.
- [x] Refusals, unavailable provider, and non-allowlisted choices do not
      execute anything (tests).
- [ ] Live read-only computer task with real UI evidence — owner run when key
      lands (Phase 7 dogfood).

### Phase 5 — continuity and decision memory (invariants held)

- [x] A JEV decision call writes only `ModelCallLog`; no `Memory`, `Event`, or
      `DecisionOutcome` rows (test).
- [x] Decision memories in extraction are `evidence_type=owner_asserted` with
      source event IDs; model rationale/confidence is never a fact.
- [x] Cross-device handoff lifecycle already exists
      (`app/device_gateway/handoff.py`, `owner_handoff_contexts`); decision
      answers reuse it rather than adding a framework.
- [ ] The documented cross-process conversation/offer race remains open —
      **ownership assignment required**.

### Phase 6 — evaluation (done, live gate passed)

- [x] `run_jev_gate()` added to `app/scripts/eval_gates.py` and registered in
      `_run_all`: lane topology, chat refusal, no fabricated JSON. Key-free.
- [x] Offline tests cover payload shape, enum validation, consent/egress,
      raw-media refusal, cost receipts, kernel decision turn, lane separation.
- [x] **Live smoke passed 5/5** (owner's key, temp SQLite + real `chat_egress`
      consent): catalogue, typed choice (`read_only`, 763 ms,
      `typesafe/jev-1.13-20260917`), gateway audit, raw-media refusal,
      model-override refusal. `backend/eval/jev-smoke.json` holds the report.

### Phase 7 — dogfood (done, live pass)

- [x] `scripts/dogfood_jev.py` runs the real kernel chain
      (ask → JEV decision → deterministic handler → verified result) and exits
      0 only on a bounded `decision_tool` with evidence; SKIP/exit 2 without a
      key.
- [x] **Live dogfood passed**: "What am I working on right now?" → JEV chose
      `goal_status` → deterministic `goal.status` handler → spoken
      "Nothing is in progress." with evidence, 3,048 ms.
      `backend/eval/jev-dogfood.json` holds the receipt.

---

## 3. Measured verification (this tree)

| Check | Result |
| --- | --- |
| `pytest tests/test_gateway_streaming.py tests/test_jev_rollout.py -q` | **63 passed** |
| `pytest tests/test_gateway_streaming.py tests/test_gateway_api.py tests/test_gateway_unit.py tests/test_routing_gate.py -q` | **70 passed** |
| `pytest tests/test_spark_task.py tests/test_spark_act.py tests/test_spark_look.py tests/test_desk_meaning.py tests/test_laptop_files.py tests/test_g13_turn_controller.py tests/test_gateway_api.py tests/test_gateway_unit.py tests/test_routing_gate.py tests/test_cognitive_os_v2.py -q` | **155 passed** |
| `ruff check` changed gateway/ev/voice/eval/scripts/tests files | clean |
| `mypy` changed gateway files + `test_jev_rollout.py` | clean |
| `python ../scripts/smoke_jev.py` (no key) | catalogue PASS, provider checks SKIP, exit 2 |
| `python ../scripts/smoke_jev.py` (owner key, temp DB) | **5/5 PASS, exit 0** |
| `python ../scripts/dogfood_jev.py` (owner key, temp DB) | **PASS, exit 0** (`decision_tool`, 3,048 ms) |
| Pre-existing reds at HEAD (not introduced): `test_tool_loop` 1, `test_eval_gates::test_api_contract_gate_passes` 1, 5 voice tests | reproduced in `/tmp/ev-head` |

## 4. Ownership / dependency ledger

| Path | Owner | Note |
| --- | --- | --- |
| `backend/app/gateway/**`, `docs/GATEWAY.md` | Agent 10 CORTEX | in-lane |
| `backend/app/ev/{spark_*,desk_meaning,brain_file_runner,laptop_files,diagnostics,luna_adapter}.py` | Agent 15 ORACLE | migrated; review welcome |
| `backend/app/voice/pipeline.py` | Agent 4 VOICE | one helper added; ASR/TTS untouched |
| `backend/app/ev/vision.py` | Agent 6 EYES | pixels stay local |
| `backend/app/cognitive/{kernel,mode}.py` | **unassigned** | decision turn added; Agent 1 must assign |
| `app/memory/{curator,llm_extractor}.py`, `app/memory/life_archive/` | **unassigned** | do not change before assignment |
| `app/scripts/eval_gates.py` | Agent 20 LAUNCH | one key-free gate added |
| `chat_egress` consent lifecycle | Agent 19 VAULT | provider enforces active record |
| `config.py`, `.env.example`, `docs/ENVIRONMENT.md` | shared | append-only blocks |

## 5. Go-live runbook (when the owner provides the key)

```sh
# 1. Key + gates (local secrets only, never git)
export EV_OPENROUTER_API_KEY=...
export EV_ALLOW_REMOTE_CHAT=true
export EV_JEV_ENABLED=true
export EV_COGNITIVE_MODE=jev_kernel
# 2. Grant the revocable chat_egress consent record (existing VAULT flow)

# 3. Prove the real contract (from backend/)
uv run python ../scripts/smoke_jev.py       # must exit 0

# 4. Prove one real workflow
uv run python ../scripts/dogfood_jev.py     # must exit 0

# 5. Only then make it the blessed profile; one-flag rollback:
#    EV_JEV_ENABLED=false  (or EV_COGNITIVE_MODE=muse_kernel)
```

## 6. What is still not real

- **Live behavior is now proven** for the provider, the kernel decision turn,
  and the converted classification surfaces (smoke 5/5, dogfood pass, live
  spot checks above). Broader live quality (score/noul questions, sustained
  latency, every phone-tool argument) is not yet measured.
- Generative text surfaces are intentionally deterministic under `jev_kernel`
  (file bodies, invented list items, memory summaries) because a decisions
  model cannot produce them; they never fall back to Spark.
- Unowned cognitive/memory/handoff paths still need Agent 1 ownership before
  they can be changed.
- Full `make lint`/`mypy`/`pytest` remain below their historical baselines;
  this work adds no new failures in the lane (pre-existing reds listed above).
- A stale parallel `opencode` server repeatedly overwrote these files during
  the session; it was identified and terminated. If edits appear from nowhere
  again, check for that process first.
