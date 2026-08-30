# EV — Glossary

**Version 1.0** — shared vocabulary across the plan suite.

| Term | Definition |
| --- | --- |
| Event | Immutable raw input (text, voice, image, file, note, share, conversation turn) with provenance fields. |
| Tombstone | Deletion marker on an event; row is preserved, memory derived from it is redacted. |
| Redaction | Hiding derived memories whose source events were tombstoned; rows remain for audit. |
| Memory | Derived, typed, versioned record (episodic, semantic, fact, decision, goal, preference, observation, pattern, summary). |
| Provenance | The chain from a memory to its source events, versions, and reasons. |
| source_type | `explicit` (user-stated), `inferred` (observation), or `derived` (computed). |
| Privacy level | `private | normal | sensitive | never_send_to_model`; enforced at retrieval. |
| Version group | A chain of memory versions representing one evolving fact/preference/decision. |
| Conflict | Record of contradictory memories; surfaced instead of silently resolved. |
| Hybrid retrieval | Locked scoring formula (semantic/keyword/recency/importance/relationship/confidence). |
| Context budget | Token cap for assembled model context (~20k default). |
| Memory tools | Bounded functions the model can call (search_memory, search_decisions, …). |
| Orchestrator | Layer that decomposes, retrieves, ranks, assembles, and writes memory. |
| AI Gateway | Provider registry that makes the model replaceable. |
| Interaction Intelligence | New layer choosing mode, tone, length, and assertiveness per response. |
| InteractionState | Snapshot of context, intent, urgency, emotion, task type, goal, confidence, permissions. |
| Communication mode | Casual / technical / analytical / coaching / emergency / collaborative. |
| User State Engine | Maintains current activity, project, goal, task, focus from events. |
| Session state | Ephemeral working context; expires on inactivity; not long-term memory. |
| Decision intelligence | Extended decision schema + expected-vs-actual outcome loop. |
| Pattern engine | Evidence-backed detection of repeated behaviors with confidence. |
| Assertiveness level | L0 neutral → L1 recommend → L2 strong → L3 challenge → L4 critical intervention. |
| Intervention score | importance × urgency × confidence × goal relevance × benefit; drives do-nothing/mention/notify. |
| EV Sense | Predictive layer that anticipates useful information before being asked. |
| Alert radar | Watchlist-driven alerts with priority, dedup, digests, quiet hours. |
| Tactical mode | Pre-event briefings and quick cards for high-stakes situations. |
| Health radar | HealthKit vitals → readiness, trends, anomaly alerts. |
| Gear telemetry | Device/watch battery and system diagnostics. |
| Maker companion | Projects, BOM, inventory, print queue. |
| HUD schema | `ev.hud.card.v1` / `ev.hud.briefing.v1` JSON contracts rendered on any surface. |
| Personality profile | Structured, versioned communication parameters with core invariants. |
| Relationship model | Evidence-backed stats of interactions, recommendations, corrections, trust. |
| Self-evaluation | Logged assessment of answer usefulness, recommendation follow-through, prediction accuracy. |
| Permission matrix | Per-action rights: access, store, send-to-model, send-to-service, act. |
| Attention budget | Cap on actionable notifications; quiet hours + digest batching. |
| Idempotency key | Client-supplied key preventing duplicate event writes. |
| SSE | Server-Sent Events streaming for chat responses. |
| EV LIVE | Full-duplex conversational voice runtime: continuous state, silence-aware turn-taking, backchannels, barge-in, and foreground speech with DeepSeek delegated for deep work (`WS /v1/voice/live`). |

