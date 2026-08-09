# EV — Requirements & Traceability

**Version 1.0** — every E.V. film component maps to functional requirements (FR),
assigned to a milestone, with an acceptance test. Requirement IDs are stable;
tests reference them.

## 1. Traceability matrix

| E.V. component | FR group | Milestone |
| --- | --- | --- |
| Self-built / no dependency | `FR-SYS` | M0 |
| Understands Peter deeply (memory) | `FR-MEM` | M0–M1 |
| Companion / friend | `FR-COMP` | M3/M5 |
| Suit + workbench surfaces | `FR-DEV` | M2 |
| HUD in mask lenses | `FR-HUD` | M5 |
| Vitals / body scan | `FR-HEALTH` | M5 |
| Web-shooter diagnostics | `FR-GEAR` | M5 |
| Research help | `FR-RESEARCH` | M3/M5 |
| Spider-sense supplement | `FR-SENSE` | M5 |
| Criminal alerts | `FR-ALERT` | M5 |
| Tactical combat analysis | `FR-TACTICAL` | M5 |
| Street navigation | `FR-NAV` | M5 |
| Diagnostics / calibration | `FR-DIAG` | M0/M5 |
| Voice | `FR-VOICE` | M2/M5 |
| Less intrusive than Karen | `FR-UX` | M3/M5 |
| Secret identity / privacy | `FR-SEC` | M0–M4 |

## 2. System & self-hosting (`FR-SYS`)

| ID | Requirement | Acceptance |
| --- | --- | --- |
| FR-SYS-01 | The system runs fully self-hosted via Docker Compose on an always-on Mac. | `docker compose up` boots db, redis, minio, api, worker; `/v1/health` green. |
| FR-SYS-02 | Phone access works over Tailscale without exposing ports to the internet. | iOS client reaches API via Tailscale address. |
| FR-SYS-03 | The reasoning model is provider-swappable via a gateway registry. | Switching `EV_CHAT_PROVIDER` changes provider without code changes; `/v1/models` reflects it. |
| FR-SYS-04 | Embeddings use a dedicated model API, with an offline deterministic fallback. | Provider interface; `hash` provider works without network. |
| FR-SYS-05 | All derived data can be regenerated from raw events. | Drop derived tables → rerun pipeline → equivalent state (test). |

## 3. Memory (`FR-MEM`)

| ID | Requirement | Acceptance |
| --- | --- | --- |
| FR-MEM-01 | Every input becomes an immutable event with provenance fields. | No UPDATE/DELETE path on events; sha256 recorded. |
| FR-MEM-02 | Every memory traces to ≥1 source event. | Provenance query returns events for 100% of memories. |
| FR-MEM-03 | Typed memories: episodic, semantic, fact, decision, goal, preference, observation, pattern, summary. | Extraction produces correct types for seeded corpus. |
| FR-MEM-04 | Inferred claims are phrased as observations, never facts. | Extract "I prefer X" → preference; unstructured → "Observed: …". |
| FR-MEM-05 | Changed preferences/decisions create a new version linked to the old one with a reason. | v1 remains queryable after v2; `supersedes`/`valid_until` correct. |
| FR-MEM-06 | Temporal queries ("what was I thinking in March?") respect `valid_from/valid_until`. | Retrieval with `as_of` returns only valid-at-date memories. |
| FR-MEM-07 | Contradictory memories create conflict records and surface in chat. | Two conflicting preferences → open conflict; chat mentions it. |
| FR-MEM-08 | Deduplication prevents repeated identical memories. | Same note captured twice → one memory, two provenances. |
| FR-MEM-09 | Entities and typed relationships are extracted and browsable. | `GET /v1/people` returns extracted person entities. |
| FR-MEM-10 | Every read/write is recorded in the access log. | Capture/query/audit/delete all appear in `access_log`. |

## 4. Retrieval & orchestration (`FR-RETR`, `FR-ORCH`)

| ID | Requirement | Acceptance |
| --- | --- | --- |
| FR-RETR-01 | Hybrid retrieval implements the locked scoring formula with per-component scores. | Score = 0.35S+0.20K+0.15R+0.15I+0.10Rel+0.05C; components returned. |
| FR-RETR-02 | Representative queries surface intended memory in top-5 ≥80% of cases. | Eval harness (see `EVALUATION.md`). |
| FR-RETR-03 | Retrieval respects privacy levels and `as_of`. | `never_send_to_model` absent from model-access results. |
| FR-ORCH-01 | Chat assembles context within the configured token budget. | Orchestrator tests assert `context_tokens ≤ budget`. |
| FR-ORCH-02 | Model can call bounded memory tools with a terminating loop. | Tool loop max 3 rounds; results capped; termination test. |
| FR-ORCH-03 | Response returns memory deltas and retrieval provenance. | `memory_delta` + `provenance` present in chat response. |
| FR-ORCH-04 | Repeated decision loops trigger Level-3 challenge citing prior decisions. | Scripted corpus → challenge text cites ≥2 prior decisions. |

## 5. Companion (`FR-COMP`)

| ID | Requirement | Acceptance |
| --- | --- | --- |
| FR-COMP-01 | EV maintains relationship memory (topics, tone, cadence, check-ins). | Interaction stats queryable; cadence adapts to activity. |
| FR-COMP-02 | Tone calibration adapts to user preference (configurable directness). | Setting change alters response style in scripted test. |
| FR-COMP-03 | EV answers "why do you know that?" with sources and confidence. | Audit view + in-chat provenance. |
| FR-COMP-04 | EV never fabricates memories; states when inferring. | Scripted scenarios; companion rubric (see EVALUATION). |
| FR-COMP-05 | Coaching levels L1/L2/L3 escalate with evidence. | Decision-loop test triggers L3 with citations. |
| FR-COMP-06 | No manipulative patterns or fabricated intimacy. | Safety test suite (boundary prompts, no dependence loops). |

## 6. Devices & surfaces (`FR-DEV`)

| ID | Requirement | Acceptance |
| --- | --- | --- |
| FR-DEV-01 | iOS app supports voice, camera, share sheet, notes, chat, memory browser, timeline. | UI flow tests + manual demo. |
| FR-DEV-02 | Watch app provides quick capture and a today/HUD card. | Watch demo; payload sync verified. |
| FR-DEV-03 | Mac/web/CLI share the same backend and memory. | Cross-device capture → query consistency test. |
| FR-DEV-04 | Offline captures queue locally and sync on reconnect. | Airplane-mode test; queue drains on reconnect. |
| FR-DEV-05 | Multi-device sync converges without data loss. | Two clients capture concurrently → both memories present. |

## 7. Health radar (`FR-HEALTH`)

| ID | Requirement | Acceptance |
| --- | --- | --- |
| FR-HEALTH-01 | Imports HealthKit vitals (HR, HRV, sleep, activity) with explicit permission. | Health data appears in `health_snapshots`; permission revocable. |
| FR-HEALTH-02 | Computes readiness, sleep debt, stress, and activity trends. | Synthetic data → expected trend outputs. |
| FR-HEALTH-03 | Detects anomalies (HRV drop, sleep regression) and alerts. | Synthetic anomaly → alert with priority. |
| FR-HEALTH-04 | Health data is stored `sensitive`, excluded from model context by default. | Boundary test: no health values in prompts without opt-in. |
| FR-HEALTH-05 | Produces a morning readiness brief. | Daily brief generated and renderable as HUD card. |

## 8. Gear telemetry (`FR-GEAR`)

| ID | Requirement | Acceptance |
| --- | --- | --- |
| FR-GEAR-01 | Monitors iPhone/Watch/Mac battery, storage, backup status. | Telemetry rows update; low-battery alert fires. |
| FR-GEAR-02 | Produces pre-departure gear checklists. | "Leave soon" flow includes gear status card. |
| FR-GEAR-03 | EV self-diagnostics surface in the same telemetry view. | `/v1/gear` includes provider health. |

## 9. EV Sense (`FR-SENSE`)

| ID | Requirement | Acceptance |
| --- | --- | --- |
| FR-SENSE-01 | Predicts useful next actions from patterns, calendar, health, deadlines. | Scripted corpus → expected prediction ranks top. |
| FR-SENSE-02 | Every prediction is inspectable ("why did EV tell me this?"). | Prediction carries trigger ids + rationale. |
| FR-SENSE-03 | Respects quiet hours and intrusiveness dial. | Quiet hours suppress delivery; digest batches. |
| FR-SENSE-04 | No prediction is acted on autonomously without permission. | All EV Sense outputs are suggestions only. |

## 10. Alert radar (`FR-ALERT`)

| ID | Requirement | Acceptance |
| --- | --- | --- |
| FR-ALERT-01 | User-defined watchlist (topics/projects/people) with sources: calendar, reminders, bills, RSS/email, repos. | Watchlist CRUD + source connectors behind permission gates. |
| FR-ALERT-02 | Alerts are deduplicated, priority-ranked, and digest-batched. | Precision/recall eval on synthetic corpus; dedup test. |
| FR-ALERT-03 | Notification budget and quiet hours are enforced. | >budget alerts are queued into digest. |
| FR-ALERT-04 | Each alert links to its trigger event and rationale. | Alert detail shows provenance. |

## 11. Tactical mode (`FR-TACTICAL`)

| ID | Requirement | Acceptance |
| --- | --- | --- |
| FR-TACTICAL-01 | Pre-event briefings include objective, context, people, risks, options, recommendation, talking points. | Output validates against `ev.hud.briefing.v1` schema. |
| FR-TACTICAL-02 | Risk assessment uses past outcomes from memory. | Scripted outcomes alter risk ratings. |
| FR-TACTICAL-03 | Pre-event briefing latency < 3 s; quick card < 800 ms. | Latency tests on reference corpus. |
| FR-TACTICAL-04 | Briefings render on Watch/Lock Screen/Mac as HUD cards. | Schema renders on all targets. |

## 12. Research assistant (`FR-RESEARCH`)

| ID | Requirement | Acceptance |
| --- | --- | --- |
| FR-RESEARCH-01 | Research sessions capture question, sources, notes, conclusions. | Session CRUD; notes link to memories. |
| FR-RESEARCH-02 | Answers cite personal memory and external sources. | Citation fields present; sources saved as events. |
| FR-RESEARCH-03 | "Remember this finding" persists with provenance. | Capture flow → memory with source event. |
| FR-RESEARCH-04 | Works without external search (memory-only mode). | No-key configuration returns memory-grounded answers. |

## 13. Navigation (`FR-NAV`)

| ID | Requirement | Acceptance |
| --- | --- | --- |
| FR-NAV-01 | Provides route-to-next-event briefings with leave-by alerts. | Calendar event → card with route + leave-by. |
| FR-NAV-02 | Integrates Apple Maps (or user-selected provider) via permission. | Deep-link opens directions; ETA in card. |

## 14. Maker companion (`FR-MAKER`)

| ID | Requirement | Acceptance |
| --- | --- | --- |
| FR-MAKER-01 | Projects with design files, BOM, build logs. | Project CRUD + file attachment. |
| FR-MAKER-02 | Materials inventory with locations and reorder thresholds. | Reorder alert at threshold. |
| FR-MAKER-03 | Print queue integration (OctoPrint-compatible). | Job status events; failure alert. |
| FR-MAKER-04 | Step-by-step assistance tied to project state. | "Next step" returns current build step. |

## 15. Voice & HUD (`FR-VOICE`, `FR-HUD`)

| ID | Requirement | Acceptance |
| --- | --- | --- |
| FR-VOICE-01 | Voice capture uses on-device STT; transcripts become events. | Voice note → transcript event with attachment. |
| FR-VOICE-02 | Persona TTS with local fallback and configurable voice. | Voice setting applies; offline fallback works. |
| FR-HUD-01 | All time-sensitive outputs validate against HUD card/briefing schemas. | Schema validation in CI. |
| FR-HUD-02 | HUD schemas render to Watch complication, widget, Mac card; AR-ready. | Renderers implemented for targets; AR schema validated. |

## 16. Diagnostics (`FR-DIAG`)

| ID | Requirement | Acceptance |
| --- | --- | --- |
| FR-DIAG-01 | `/v1/health` and "EV checkup" report DB, queues, storage, gateway, error rates. | Checkup returns per-component status. |
| FR-DIAG-02 | Pipeline errors are logged and surfaced without losing events. | Failed extraction retried; event intact. |

## 17. UX & attention (`FR-UX`)

| ID | Requirement | Acceptance |
| --- | --- | --- |
| FR-UX-01 | Notification attention budget, quiet hours, digest batching. | Enforcement tests. |
| FR-UX-02 | Intrusiveness dial (Quiet/Balanced/Proactive). | Threshold changes alter proactive volume. |
| FR-UX-03 | Every proactive message has a visible "why now?" rationale. | Rationale present in alert/briefing. |
| FR-UX-04 | Voice-first parity and accessibility (Dynamic Type, contrast, reduce motion). | Accessibility review checklist. |

## 18. Security (`FR-SEC`)

| ID | Requirement | Acceptance |
| --- | --- | --- |
| FR-SEC-01 | Master-key auth + device registration/revocation. | Unauthorized request rejected; revoked device rejected. |
| FR-SEC-02 | TLS in transit and encryption at rest. | Config + restore drill verify. |
| FR-SEC-03 | `never_send_to_model` excluded at retrieval boundary. | Boundary tests instrument provider payloads. |
| FR-SEC-04 | Export returns full bundle; delete tombstones and redacts. | Round-trip test. |
| FR-SEC-05 | Encrypted backups with restore drill. | Wipe → restore → verify counts + audit. |
| FR-SEC-06 | Access log covers reads/writes/export/delete. | Log assertions in API tests. |

## 19. Behavior & interaction (`FR-BHV`)

Addendum from `BEHAVIOR.md` — the interaction intelligence layer, user state,
decision intelligence, proactive behavior, personality, and self-evaluation.

| ID | Requirement | Acceptance |
| --- | --- | --- |
| FR-BHV-01 | Interaction Intelligence layer between reasoning and response; mode selected deterministically from state. | Mode-selection accuracy ≥90% on scripted corpus. |
| FR-BHV-02 | Six communication modes (casual, technical, analytical, coaching, emergency, collaborative) with triggers and constraints. | Each mode produced in scripted scenario; emergency ≤1 sentence + action. |
| FR-BHV-03 | Adaptive response length targets minimum useful communication. | Length within ±40% of target; filler trimmed. |
| FR-BHV-04 | Conversational continuity: conversations are events; "continue" resumes session/user state. | "Continue" test reconstructs active project/goal. |
| FR-BHV-05 | User State Engine maintains activity/project/goal/task/focus from events. | State fields update on scripted event stream. |
| FR-BHV-06 | Major recommendations carry a goal-alignment tag. | Tag present in response log; correct on scripted cases. |
| FR-BHV-07 | Decision schema includes context, problem, options, reason, expected/actual outcome, related goal/project. | Schema validation on decision memories. |
| FR-BHV-08 | Decision follow-up compares expected vs actual outcome; lessons written with provenance. | Planted outcome → lesson memory with event links. |
| FR-BHV-09 | Pattern engine emits evidence, frequency, time range, confidence, kind. | Pattern record fields validated. |
| FR-BHV-10 | Constructive challenge L0–L4 with evidence gates; L4 requires standing permission. | L3 only with confidence ≥0.7 + ≥2 decisions; L4 blocked without permission. |
| FR-BHV-11 | Intervention scoring with do-nothing/mention/notify policy. | Score thresholds respected in tests; quiet hours enforced. |
| FR-BHV-12 | Predictions stored with confidence, basis, outcome; "why now?" rationale. | Prediction lifecycle test. |
| FR-BHV-13 | Memory explainability includes evidence summaries from provenance. | Audit shows evidence summary. |
| FR-BHV-14 | Corrections create new versions; old records preserved. | Correction test: v2 active, v1 intact. |
| FR-BHV-15 | Forget vs permanent-delete are distinct operations. | Forget hides from retrieval; delete tombstones raw events. |
| FR-BHV-16 | Tool registry with selection intelligence; sensitive tools require per-call permission. | Calculator/memory/web routing test; denied tool logged. |
| FR-BHV-17 | Model routing hidden, policy-based, eval-gated. | Routing policy config; user never sees model ids by default. |
| FR-BHV-18 | Personality profile structured, versioned, consistent with core invariants. | Profile CRUD + invariant tests. |
| FR-BHV-19 | Relationship stats evidence-backed and user-visible. | Stats view returns counts from log. |
| FR-BHV-20 | Self-evaluation writes response logs; aggregates calibrate behavior. | Response log rows; calibration deltas visible. |
| FR-BHV-21 | Per-action permission matrix (access/store/send-to-model/send-to-service/act) enforced at engine boundaries. | Denial logs + boundary tests. |
| FR-BHV-22 | Emotional-state inference requires consent and is labeled as inference. | No inference without opt-in; labels present. |
| FR-BHV-23 | Advanced autonomy (P9) is deferred; permissioned micro-actions have approval logs. | No autonomous action without approval record. |
