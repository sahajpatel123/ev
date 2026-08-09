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

## 3. Decision process

1. Record the question here with options.
2. Default to the recommended row unless the user overrides.
3. Any override updates the impacted plan docs (traceability via FR IDs) and this log.
4. Decisions are revisited at milestone reviews (M1, M3, M5).
