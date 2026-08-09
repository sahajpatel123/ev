# EVIE Intelligence Filter — Architecture v2 (full-duplex, voice-aware, 24/7)

**Status:** architecture + implementation (deterministic stages live in
`backend/app/filter/`; the LLM critic and voice/wake hardware tracks remain
future work). Supersedes v1 (output-only filter). This version treats the
filter as the complete **EVIE runtime** between the user and the provider
brain.

## 1. Independent review of v1 (unbiased)

What v1 got right:

- The core insight — raw provider output is not the product; a filter layer is.
- Grounding audit, persona/style enforcement, contract validation, safety
  redaction, bounded critic loops, filter ledger, staged trust.
- Honest constraints: the filter never invents facts; edits are auditable.

What v1 missed (the user's requirements):

- **It was output-only.** The user explicitly said *every* pass to and from the
  provider must go through a filter. v1 had no input filter.
- **No voice identity.** "Recognize my voice only" — v1 had nothing about
  speaker verification, wake words, or enrollment.
- **No training path.** "Train our model with plenty of data" — v1 had no
  enrollment/adaptation/fine-tuning tracks.
- **No 24/7 runtime.** v1 described a request-response filter, not an
  always-on assistant lifecycle (IDLE → LISTENING → AWAKE → …).
- **No 1M-window strategy.** "Remember my whole life" was implied but not
  architected (hierarchical context, progressive loading, scratch workspace).
- **No action/search loop.** v1 refined answers but didn't specify how the
  filter turns a command into actions and searches.

This v2 fixes all of those.

## 2. The model: EVIE = Provider brain + Intelligence Filter runtime

DeepSeek V4 Flash (D-V4) is the **brain**: it has general reasoning but no
specialized EVIE knowledge, no voice identity, no life memory, no personality
enforcement. The **Intelligence Filter** is everything around the brain:

```text
                 ┌────────────────────────────────────────────┐
                 │              USER (owner voice)            │
                 └─────────────────────┬──────────────────────┘
                                       │ audio / text / live data
                 ┌─────────────────────▼──────────────────────┐
                 │  PERCEPTION LAYER (devices, 24/7)          │
                 │  wake engine → voice ID → ASR → NLU        │
                 └─────────────────────┬──────────────────────┘
                 ┌─────────────────────▼──────────────────────┐
                 │  INPUT FILTER (identity, privacy, intent,  │
                 │  state, memory retrieval, context compile) │
                 └─────────────────────┬──────────────────────┘
                 ┌─────────────────────▼──────────────────────┐
                 │  PROVIDER GATEWAY — D-V4 Flash (raw brain) │
                 └─────────────────────┬──────────────────────┘
                 ┌─────────────────────▼──────────────────────┐
                 │  OUTPUT FILTER (grounding, persona, style, │
                 │  safety, critic, finalize)                 │
                 └─────────────────────┬──────────────────────┘
                 ┌─────────────────────▼──────────────────────┐
                 │  ACTION & RESPONSE (TTS, HUD, tools,       │
                 │  searches, notifications)                  │
                 └─────────────────────┬──────────────────────┘
                                       │
                 ┌─────────────────────▼──────────────────────┐
                 │  RUNTIME + LEDGER + TRAINING HARVESTER     │
                 │  (24/7 daemon, queues, feedback loops)     │
                 └────────────────────────────────────────────┘
```

The provider is swappable. EVIE's identity, voice, memory, and filter rules are
**not** — they live in the runtime.

## 3. Perception layer: "Hey EVIE" 24/7

### 3.1 Wake path (multi-stage, low power)

Industry pattern (Sensory, AONDevices/TDK, 2026): an ultra-low-power always-on
front end listens continuously (single-digit mW, ~15–20 KB RAM class models);
on wake word detection, a larger processor powers up in a short burst for STT
and NLU.

```text
mic → wake engine ("EVIE")        [always on, on-device]
    → speaker verification        [owner-only gate]
    → ASR (Whisper-class, on-device or local server)
    → NLU (intent + slots; local or EVIE runtime)
    → Input Filter → provider → Output Filter → TTS reply
    → 30s follow-up window without repeating the wake word
    → return to low-power IDLE
```

### 3.2 Voice identity (owner-only)

- **Enrollment:** the user shares voice samples (e.g., 5–20 clean phrases).
  Samples are stored as encrypted attachments; a speaker-embedding model
  (ECAPA-TDNN / SpeechBrain `spkrec-ecapa-voxceleb`, 192–512-dim) produces a
  **voiceprint**; enrollment uses multiple samples and versions the voiceprint
  as the voice changes.
- **Verification:** every wake event computes a speaker embedding and compares
  against the enrolled voiceprint (cosine similarity; threshold tuned to the
  user; asymmetric enroll-verify with a lightweight verifier for on-device
  speed, e.g., ECAPA-TDNNLite at ~11.6M FLOPs).
- **Anti-spoofing / liveness:** replay, synthesis, and voice-conversion attacks
  are blocked by liveness checks (prosody/pitch dynamics, challenge-response
  phrases, wavelet/CQCC-based countermeasures).
- **Unknown voice:** polite refusal, logged, never triggers actions.
- **Text fallback:** typing always works; sensitive actions require
  re-verification even if the session is unlocked.

## 4. Input filter (before the brain)

Every inbound utterance passes through:

1. **IdentityGate** — verified speaker + confidence; attaches `speaker_conf` to
   the request envelope.
2. **InputGuard** — prompt-injection checks, PII classification, privacy-level
   resolution (`never_send_to_model` hard cap), secret scanning, command-vs-
   chat classification.
3. **Intent & State compiler** — existing `interaction.py` logic: intent,
   urgency, emotion, mode, assertiveness; continuous-conversation state
   (one thread, never a new chat).
4. **MemoryBroker** — retrieval, provenance, conflicts, user state, live-data
   context; assembles only permitted content.
5. **ContextCompiler** — builds the provider prompt under budget, including:
   strategy block, continuous conversation history, retrieved memory, and a
   progressive plan for the 1M window (below).

## 5. Provider gateway

- The gateway receives the compiled envelope and calls D-V4 Flash (chat +
  tools, streaming).
- It returns raw text/tool calls/usage **plus** the envelope (strategy,
  memories, conversation, request id) so the output filter can audit.
- Tool calls are pre-validated by the gateway against the tool registry
  (Invocation-Refiner pattern: a post-processor rectifies tool invocations
  before execution).

## 6. Output filter (after the brain)

Stages (from v1, kept and tightened):

1. **Structural** — validate HUD/JSON contracts (`ev.hud.card.v1`,
   `ev.hud.briefing.v1`, `ev.hud.route.v1`); deterministic repair of missing
   fields or unparseable JSON; extract claims. *(implemented)*
2. **Grounding audit** — deterministic claim extraction and verification
   (entity/date/number overlap against the memories actually in context).
   Unsupported personal claims are removed, never kept; the audit trails in
   the filter report. Semantic/embedding verification and an optional critic
   are future refinements. *(implemented, deterministic)*
3. **Persona & style** — enforce mode, length bounds, EVIE voice
   (no generic-assistant phrasing), challenge-evidence gating, and urgency
   conciseness. *(implemented, rule-based)*
4. **Safety & privacy** — output-side PII/secret redaction, toxicity,
   manipulation/dependency nudging, jailbreak leaks. *(implemented,
   deterministic detectors)*
5. **Critic & refine** — deterministic rubric judge (grounding, persona,
   actionability, honesty, contract validity) with a max-two-iteration
   refinement loop; staged trust. A provider-backed LLM critic is future work.
   *(implemented, rule-based)*
6. **Finalize** — filter report, response log, SSE `filter-report` event,
   honest fallback on repeated failure. *(implemented)*

## 7. Training tracks ("train the model from my data")

Training happens on **four independent tracks** so one does not block another:

### Track 1 — Voice enrollment (no LLM training)

- Voice samples → voiceprints (encrypted, versioned) + liveness models.
- "Train" = enroll; happens in minutes, not weeks.

### Track 2 — Life-data personalization (retrieval, not weights)

- Every event/memory/live-data point is stored (existing architecture).
- The filter learns what to retrieve, what matters (importance), and what the
  user expects — via the ledger and self-evaluation, not weight updates.

### Track 3 — Adapter fine-tune (optional, later)

- A LoRA-style adapter over the provider or a local model trained on:
  EVIE conversation corpus (filtered responses + user corrections from
  `response_log`), voice-command transcripts, and preference labels.
- Versioned adapters (`evie-v1-lora`, `evie-v2-lora`); each gated by eval
  (persona compliance, grounding precision, correction-rate regression).
- Kept swappable: adapter = EVIE-specific layer; provider = general brain.

### Track 4 — Filter self-improvement (continuous)

- Ledger rows (draft → edits → scores → user reaction) feed monthly
  recalibration of thresholds and, later, a learned feedback model
  (LLMRefine-style).

## 8. One conversation + "remember my whole life" (1M window strategy)

- **One thread forever** (already implemented: `conversation_threads` +
  `conversation_states`; reset clears working state, never history).
- The 1M-token window is a **scratch workspace, not a life-dump**. The
  ContextCompiler uses hierarchical descent:

```text
Level 0  rolling summary of the conversation (compressed each N turns)
Level 1  recent turns (last 10–20)
Level 2  retrieved memories for this request (scored, bounded)
Level 3  deep-dive workspace: when the task needs it, progressively load
         timelines/decisions/patterns via tools into the 1M window
```

- "Remember my whole life" is answered by the **memory store** (events,
  memories, entities, version chains) + the compiler's ability to fetch the
  right slice — not by fitting a lifetime into context.
- Long-horizon summaries (daily/weekly/monthly) are additional derived indexes;
  raw events remain the source of truth.

## 9. 24/7 runtime state machine

```text
IDLE ──wake word──▶ VERIFYING ──voice ok──▶ AWAKE
  ▲                                            │
  │                                            ├─▶ PROCESSING (provider)
  │                                            │      │
  │                                            │      ▼
  │                                            │  OUTPUT FILTER
  │                                            │      │
  │                                            │      ▼
  │                                            │  RESPONDING (TTS/action)
  │                                            │      │
  └──── timeout / quiet hours / done ◀─────────┴── 30s FOLLOW-UP window
```

- **Devices as ears:** the fleet (Mac, iPhone, future glasses/watch) each run a
  wake engine; the closest/online device wins; events stream to the runtime.
- **Heartbeat & queues:** runtime health checks, ingestion queues, dead-letter
  handling, quiet hours and daily attention budget (existing settings).
- **ActionRouter:** commands become approved actions (searches, tasks,
  HUD cards, fleet tasks) with per-action permission checks.

## 10. Data & storage (what changes; details later)

- Voiceprints + liveness models: encrypted, versioned, never sent to the
  provider.
- Voice samples: attachments with privacy level `sensitive` by default.
- Audio transcript: becomes an immutable event; audio itself is user-owned.
- Filter ledger: `filter_ledger` table (request id, stage, action, name,
  severity, detail, draft, final text, scores, iterations, costs, envelope
  hash, model) + `/v1/filter/ledger` and `/v1/filter/ledger/aggregate`.
  *(implemented)*
- Training corpus: versioned snapshots derived from the ledger and events;
  rebuildable, exportable, deletable with the user's data.

## 11. Evaluation gates (v2)

- Voice: enrollment with ≥5 samples; verification EER ≤ 3–5% on user test
  phrases; false-accept of a different speaker ≈ 0; liveness blocks replay.
- Wake: on-device latency ≤ 300 ms to AWAKE; power budget compatible with
  always-on device; follow-up window works without re-wake.
- Filter: grounding precision ≥ 0.95; contract validity ≥ 99%; persona
  compliance ≥ 0.9; over-refinement ≤ 2%; filter overhead ≤ 30% tokens /
  ≤ 40% latency; correction rate after filtering < before.
- Life-memory: "continue" reconstructs focus without user restating context;
  any point in the past is answerable with provenance (hierarchical load).

## 12. Risks & guardrails

- **Spoofed voice:** liveness + challenge-response + thresholds; degrade to
  text verification for sensitive actions.
- **Over-filtering:** minimal-edit diffs, "show original" toggle, critic
  calibrated on gold labels.
- **Latency:** wake→reply budget ~1.5–3 s for casual; deep tasks can be
  progressive; hard iteration caps.
- **Privacy:** audio/voiceprints never leave the user's machines; critic
  prompts only contain already-permitted context.
- **Training drift:** adapters versioned and eval-gated; user can roll back.
- **Provider change:** gateway + envelope contracts keep the filter
  provider-agnostic.

## 13. Sources

- ECAPA-TDNN speaker verification (SpeechBrain `spkrec-ecapa-voxceleb`);
  asymmetric enroll-verify ECAPA-TDNNLite (arXiv 2110.04438, EER 3.07%,
  11.6M FLOPs).
- Always-on low-power wake engines: Sensory wake word (~15–20 KB RAM,
  <1 mA) and AONDevices/TDK AON1100 M3 (2026) — wake on-device, then burst a
  larger processor for STT/NLU.
- Voice assistant pipeline reference (Zenodo 2026): wake → Whisper/Silero ASR
  → intent/slot NLU → TTS, ~402 ms E2E; follow-up window pattern
  (jarvis-voice-assistant: 30 s without re-wake).
- Anti-spoofing / voice liveness: replay/synthesis/voice-conversion
  countermeasures (wavelet scattering, CQCC; VocalID-style Whisper + ECAPA
  combination).
- Post-processing refinement: Invocation Refiner (ACL 2026), LLMRefine
  (NAACL 2024), N-Critics (NeurIPS 2023), CRITIC (ICLR 2024), Guardrails AI /
  NeMo Guardrails, Vertex grounding check.
