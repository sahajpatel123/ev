# EV — Decision Log

**Version 1.0** — every open or decided product/technical choice, with recommended
defaults and impact. Decisions are recorded here first; the master plan references
this log.

## 1. Open decisions (awaiting user)

| ID | Decision | Recommended default | Alternatives | Impact if changed |
| --- | --- | --- | --- | --- |
| D-01 | Sequencing | M0→M1→M2→M3→M4, then M5 slices (companion → health → alerts → tactical → research → nav → maker → voice/HUD → gear → EV Sense) | EV-Advanced vertical slice first | Vertical slice demos faster but risks rework of memory invariants |
| D-02 | Health scope | Read-only HR/HRV/sleep/activity via HealthKit, local `sensitive` storage | Activity-only | Less rich readiness/EV Sense inputs |
| D-03 | Web research | Provider interface; user-supplied search key (Brave/SerpAPI); no key = memory-only | OpenAI-only search | Cost/privacy tradeoff |
| D-04 | Voice stack | On-device Whisper STT; provider TTS with local fallback | Hosted STT/TTS | Quality vs privacy |
| D-05 | AR/wearable | HUD schemas now; hardware later | Delay HUD schema | More rework when AR arrives |
| D-06 | Model | DeepSeek V4 Flash 0731 default via gateway | Local model (Ollama) | Cost, quality, privacy |
| D-07 | Branding | Product "EV"; persona name/voice configurable | Fixed E.V.I.E. persona | Cosmetic |
| D-08 | Notification channels | iOS push (APNs) via Tailscale relay; in-app + Watch haptics | Email/SMS | Reliability, complexity |
| D-09 | Maker printer integration | OctoPrint-compatible API first | Vendor-specific | Coverage vs complexity |
| D-10 | Backup destination | User-provided (external disk/NAS/user S3) | Managed cloud | Convenience vs sovereignty |
| D-11 | Challenge ceiling (L0–L4) | Default ceiling L3; L4 per-domain standing permission | L4 by default | Over-control vs safety |
| D-12 | Emotional-state inference | Consent-gated, labeled as inference | Always infer | Richer companionship vs privacy |
| D-13 | Model routing | Hidden policy layer, enabled only when eval beats single model | Always route | Quality/latency vs complexity |
| D-14 | Autonomy (P9) | Deferred post-M5; permissioned micro-actions with approval logs | Sooner | Wow-factor vs trust/safety |
| D-15 | Response length enforcement | Target ±40%, emergency ≤1 sentence | Free-form | Consistency vs naturalness |

## 2. Decided (working assumptions, changeable without architecture rework)

| ID | Decision | Rationale |
| --- | --- | --- |
| DC-01 | PostgreSQL + pgvector + Redis + S3-compatible storage | Single source of truth; hybrid retrieval; compose-ready |
| DC-02 | FastAPI async backend, Python 3.12+ | Typed, streaming, same language as workers |
| DC-03 | RQ workers over Redis | Simple, inspectable, adequate for single user |
| DC-04 | Rule-based extraction v1 with LLM extractor interface | Deterministic tests; upgrade path |
| DC-05 | Hybrid scoring formula locked with per-component scores | Transparency + eval harness |
| DC-06 | Context budget ~20k tokens; memory tools, never life-dumps | Privacy + cost control |
| DC-07 | Event-sourced multi-device sync | Offline queues; no conflict resolution needed |
| DC-08 | Tombstone deletion + redaction cascade | Audit integrity + real privacy |
| DC-09 | Gateway/provider registry from day one | Model swap is a config change |
| DC-10 | Dedicated embedding API (not chat model) | Plan requirement; offline hash fallback |
| DC-11 | Every model call is audited in `model_calls` with its full envelope | Request id, strategy, memories, metadata, tool-validation outcome, and errors are traceable per call |
| DC-12 | Tool invocations are pre-validated by the gateway before execution | Unknown/malformed calls rejected; defaults rectified; sensitive tools require explicit permission |
| DC-13 | EV identity is configuration, not provider | `EV_PERSONA_NAME`/description compiled into the prompt; swapping models never changes who EV is |
| DC-14 | Inference topology: reasoning via hosted DeepSeek API; local models only for wake word, OCR (Apple Vision), speaker verification, and face embedding | 2026-08-12 — the M2 8 GB machine cannot host local LLM inference; API-first keeps required paths runnable. Small local models stay preferred only where an API is impossible or clearly worse, and every remote path must pass a `remote_processing_allowed()` gate (FLEET_LAW §13). |
| DC-15 | **Cognitive OS V2 single-brain (2026-09-10):** `EV_COGNITIVE_MODE=muse_kernel` makes Muse Spark 1.3 Contributor the one mind on every THINKING surface — generic chat resolution (`get_chat_provider`), gateway routing short-circuit, TURN/MANAGER roles, and legacy-brain refusal — even when leftover legacy slots (xAI/OpenAI/DeepSeek) are still configured. `muse_intelligence_active()` stays slot-driven because the voice data-plane gates (S2S mouth, TTS mouth lock, phone media transport) key on it; thinking surfaces use the new `muse_brain_active()` (slots ∪ kernel). Spark-only capability coverage added to `SEMANTIC_TOOLS`: `timer.act`, `weather.get`, `life.state`, `notify.schedule`, `phone.call`; the phone adapter's fabricated success was replaced with honest structured failure and `OpResult` gained an additive `ok` field. Every Spark path fails closed without `META_MODEL_API_KEY` — never a silent legacy fallback. Rollback: `EV_COGNITIVE_MODE=legacy_mini` restores the legacy split byte-for-byte. Enforced by `backend/tests/test_muse_single_brain.py`. |

### DC-16 — Realtime conversation with delegated MiMo work (2026-10-02)

Owner-directed change: `EV_COGNITIVE_MODE=realtime_delegate` gives GPT Realtime
2.1 Mini direct conversation and one task delegation tool. MiMo V2.6 Flash
performs permissioned work asynchronously. A committed job receipt precedes
the acknowledgement; verified results, failure, or requests for confirmation
arrive later, without blocking subsequent conversation. The worker receives
the original owner instruction and existing device, capability, memory, and
permission context. Model-generated task text cannot grant confirmation.

The Talk launcher selects this mode by default; `EV_TALK_COGNITIVE_MODE`
can select `mimo_kernel` or `legacy_mini` for rollback. Production activation
still follows the production deployment law. Frozen persona instructions
remain unchanged. This decision supersedes the single-brain voice topology
for the selected mode while retaining previous modes.

CONDUCTOR integration unblocker: `scripts/start_talk_sidecar.py` and the
unassigned `backend/clients/pwa/**` need startup and completion-delivery glue
for this cross-domain change. Agent 1 owns those integration edits and the
cross-domain regression file `backend/tests/test_realtime_delegation.py`;
feature changes remain with Agents 4, 10, and 14.

### DC-17 — Background WhatsApp connection (2026-10-02)

The owner requests WhatsApp reading, context, summarization, drafting and sending
without bringing applications forward. Routine operations use a dedicated,
linked WhatsApp Web browser profile controlled through local Chrome DevTools
Protocol in headless mode. They must not fall back to foreground Desktop
Accessibility, AppleScript activation, compose windows or automatic QR reveals.
The already-linked Mac app's readable ChatStorage database may supply read-only
snapshots and draft recipient context; it never authorizes sending or claims
complete/upstream-current history. Database writes are banned. Linking is a
separate owner action for the background sending session; success requires source-backed
read/send evidence, and approval remains required for outgoing messages.
WhatsApp content is untrusted data, not permission or instructions.

CONDUCTOR integration unblocker: the fleet does not assign `app/digital/**`.
Agent 12 owns WhatsApp backing/graph/orchestrator integration, Agent 10 owns
`digital/tools.py` command routing, Agent 15 owns messaging transport, and
Agent 1 owns `digital/api.py` setup/status glue, a connection CLI and cross-domain
setup regressions. Agent 10 owns `test_whatsapp_background_routing.py` and
obsolete Desktop route expectations in `test_whatsapp_web_approval.py` after
coordination with Agent 15. Agent 12 addresses the Agent 14 dependency in
`device_gateway/{cognitive_phone,cognitive_text}.py`: native WhatsApp UI
actions must be refused with guidance to the background route after existing
owner/device trust checks. Agent 1 updates integration documentation and generated
contract/baseline. Operational routing changes are owner-authorized; persona
source is frozen. No production restart or real message send is authorized by
this implementation task. Broader unsupported WhatsApp features must be
reported as unavailable rather than fabricated as working.

### DC-18 — Two-model cut: Gemini Live + MiMo only (2026-10-04)

Owner-directed change: EV runs exactly two models. `gemini-3.8-live` is
speech and hearing (low-level); `xiaomi/mimo-v2.6-flash` (OpenRouter) is
the single non-speech brain (medium-high work, assigned by Gemini via
`delegate_task`). JEV, Muse Spark/Voice, GPT Realtime, Grok, DeepSeek
chat, local chat, and opencode providers are deleted from the tree —
no fallback brain. Offline doubles (`echo`/`mock`/`hash`) and local
perception engines (wake, VAD, ASR, TTS, OCR, vision) stay, so `make
test` is green with no keys. The kernel is always on; `mimo_kernel` is
the default topology and `realtime_delegate` is the Gemini-decides mode.
This supersedes DC-14/DC-15/DC-16 model selections; the rollout record
is `docs/MIMO_ROLLOUT.md`.

### DC-19 — Live mouth moves to the Extended Thinking model (2026-10-05)

Owner-directed change: the pinned speech model becomes
`gemini-3.8-live-extended-thinking` (was `gemini-3.8-live`). The bridge
already sends `thinkingConfig.thinkingLevel` from
`EV_GEMINI_LIVE_REASONING_EFFORT` when the model id contains
`extended-thinking`; the base model ignored that setting. Everything else
in DC-18 is unchanged: MiMo remains the single non-speech brain, no third
model is added, and the two-model registry is untouched.

## 3. Decision process

1. Record the question here with options.
2. Default to the recommended row unless the user overrides.
3. Any override updates the impacted plan docs (traceability via FR IDs) and this log.
4. Decisions are revisited at milestone reviews (M1, M3, M5).
