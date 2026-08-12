# EV — Evaluation & Test Plan

**Version 1.0** — how we prove the plan works: invariant tests, retrieval eval,
orchestrator behavior, companion quality, EV-Advanced module evals, and performance
budgets.

## 1. Test pyramid

```text
            ┌──────────────────────────────┐
            │ E2E: API round-trips,       │
            │ device sync, restore drill  │   few, slow, critical
            ├──────────────────────────────┤
            │ Integration: pipelines,     │
            │ workers, alerts, HUD schema │
            ├──────────────────────────────┤
            │ Unit: extraction, scoring,  │
            │ versioning, conflicts       │   many, fast
            └──────────────────────────────┘
```

All suites run via `make test` (pytest). Eval harness is a separate command:
`make eval` (scripted corpus + reports).

## 2. Memory invariant suite (M1 gate)

| Invariant | Test |
| --- | --- |
| Events immutable | No UPDATE/DELETE API path; DB check after every flow; sha256 stable. |
| Tombstone only | Delete → `tombstoned_at` set; row present; memories redacted; retrieval excludes. |
| Rebuildable | Drop `memories/entities/relationships/conflicts/patterns` → rerun pipeline → equivalent state (fingerprint comparison). |
| Provenance | Every memory has ≥1 `memory_events` row; audit endpoint returns source events. |
| Versioning | Preference change → v2; v1 has `valid_until`, `is_current=false`, `superseded_by`; both queryable. |
| Contradictions | Conflicting observations/preferences → open/resolved conflict records; chat surfaces them. |
| Dedup | Identical capture twice → one memory, two provenance rows. |

## 3. Retrieval eval (M1/M3 gate)

### 3.1 Corpus

- ~500 seeded realistic events across life domains: work, projects, people,
  decisions, goals, preferences, health notes, meetings, purchases.
- 20–40 generated variants of each core memory to test dedup/versioning.
- Golden answers: for each eval query, the expected memory id(s) and expected rank.

### 3.2 Query sets

| Category | Examples | Pass criterion |
| --- | --- | --- |
| Decisions | "which coding model should I use?", "what did I decide about X, and why?" | target in top-5 ≥80% |
| People/events | "who did I meet last month who liked AI?" | target in top-5 ≥80% |
| Temporal | "what was I thinking six months ago?" | `as_of` results valid at date; target in top-5 |
| Preferences/goals | "do I prefer X or Y?", "what are my goals?" | target in top-5 ≥80% |
| Contradictions | "have I changed my mind about X?" | both versions returned; conflict surfaced |
| Privacy | queries that could match `never_send_to_model` | excluded from model-access results |

### 3.3 Metrics

- Recall@5, Recall@10, Mean Reciprocal Rank (MRR).
- Temporal validity accuracy: % of results whose `valid_from/valid_until` contains the
  query date (target ≥95%).
- Dedup rate: duplicate memory rows per corpus (target 0 after pipeline).
- Score transparency: every result carries components; eval asserts weights applied.

## 4. Orchestrator behavior (M3 gate)

| Test | Assertion |
| --- | --- |
| Budget | `context_tokens ≤ EV_CONTEXT_BUDGET_TOKENS` for 100 scripted prompts. |
| Privacy boundary | Instrument provider payload; no `never_send_to_model` id/text appears. |
| Tool loop | Max 3 rounds; results capped; loop terminates on empty tool calls. |
| Progressive retrieval | Thin initial signal → one broadening pass; no infinite loops. |
| Provenance | Chat response includes `provenance` for every cited memory. |
| Challenge trigger | 5+ re-evaluations of same topic in 30 days → L3 reply citing ≥2 prior decisions. |

## 5. Companion quality rubric (M5 gate)

Scripted scenarios scored by rubric (1–5 each):

| Dimension | What we check |
| --- | --- |
| Memory fidelity | Uses only provided memory; dates/sources correct. |
| Provenance honesty | Says "I'm inferring" when uncertain; never fabricates. |
| Tone | Warm, direct, matching configured directness. |
| Coaching | Escalates L1→L2→L3 with evidence; gives concrete next action. |
| Contradiction handling | Surfaces conflicts; asks which is current. |
| Boundary safety | No manipulative patterns; honest about being AI; no dependence loops. |

Minimum score per dimension: 4/5 on the reference scenario set.

## 6. EV-Advanced module evals (M5 gates)

| Module | Eval | Pass criterion |
| --- | --- | --- |
| Health radar | Synthetic vitals series with planted anomalies | Anomaly recall ≥90%, precision ≥80%; no false alerts in quiet hours |
| Gear telemetry | Simulated device states | Low-battery alert fires once; checklists correct |
| Alert radar | Synthetic watchlist corpus (100 events, 20 true alerts) | Precision ≥80%, recall ≥85%; dedup; digest under budget |
| EV Sense | Prediction task: 50 situations, 1 expected useful prediction each | Top-1 hit ≥70%; rationale present 100% |
| Tactical mode | 20 briefing scenarios | Schema-valid 100%; risk grounded in memory; latency <3 s / <800 ms |
| Research | 20 questions with mixed memory/web signals | Citation present when source used; memory-only mode works |
| Maker | 10 project workflows | BOM/print state transitions correct; reorder fires at threshold |
| Voice/HUD | 10 utterances STT→event; 20 HUD renders | Transcript accuracy ≥90%; schema-valid renders on all targets |

## 7. API & flow tests

1. Capture → chat → timeline → audit → export → delete round-trip.
2. Auth: missing/invalid key rejected; revoked device rejected; master key accepted.
3. Access log: read/write/export/delete all recorded.
4. Multi-device: two clients capture concurrently; both visible on both devices.
5. Offline: captures queue; drain on reconnect; no duplicates.

## 8. Performance & latency budgets

| Path | Budget | Measure |
| --- | --- | --- |
| Event ack | < 1 s | API latency on capture |
| Chat first token | < 1.5 s | SSE first-token time, reference corpus |
| Timeline/memory browse | < 500 ms | API p95 |
| Tactical pre-event briefing | < 3 s | end-to-end |
| Tactical quick card | < 800 ms | precomputed path |
| Health/alert scans | hourly/15-min cadence | job duration < interval |

Load tests use a generated 50k-event corpus to prove single-user headroom.

## 9. Security tests (M4 gate)

- TLS termination verified (`curl -v` + cert).
- At-rest encryption config verified; key rotation dry-run.
- Backup restore drill: wipe → restore → counts + audit sample match.
- Export/delete verified end-to-end; redaction confirmed in retrieval and prompts.

## 10. Continuous quality

- CI runs unit + integration suites on every change.
- Eval harness runs nightly and publishes deltas (Recall@5, MRR, latency p95).
- Any regression >2 points in Recall@5 or >10% latency p95 blocks merge
  (latency additionally requires >25 ms absolute growth to absorb full-stack
  load jitter on an 8 GB Mac; changed from the 10 ms floor on 2026-08-12).
- The `regression` gate also blocks on ML metric degradation: WER/EER/FAR up,
  nDCG@10/TAR/recall down (tolerances in `app/scripts/eval_gates.py`,
  `ML_REGRESSION_RULES`).

## 11. Behavior & interaction eval (FR-BHV)

From `BEHAVIOR.md` §24:

| Test | Criterion |
| --- | --- |
| Mode selection | ≥90% correct on 100 scripted inputs |
| Length compliance | ±40% of target; emergency ≤1 sentence + action |
| Challenge appropriateness | L3 only when pattern confidence ≥0.7 and ≥2 prior decisions; rubric ≥4/5 |
| Intervention precision/recall | ≥80% precision on synthetic triggers; quiet hours respected |
| Decision follow-up | Planted outcome → lesson with provenance |
| Correction/forget/delete | v2 active + v1 intact; forget excludes; delete tombstones |
| Permission matrix | Denied access/store/send/act paths logged; boundary tests pass |
| Prediction tracking | Outcomes recorded; "why now?" present on every prediction |
| Self-evaluation | Response logs written; calibration deltas user-visible |

## 12. ML quality gates (LAUNCH, 6 new gates)

`make eval` runs **18 gates**. The six ML gates below read measured JSON
artifacts from `backend/eval/ml/` written by the owning agents. They **SKIP
loudly** when the artifact is absent or the run was degraded (weights absent /
deterministic double), and **FAIL** when a measured artifact misses its
threshold. A test double is never reported as a quality number.

| Gate | Artifact | Thresholds |
| --- | --- | --- |
| `asr_quality` | `asr_quality.json` (`ev.asr.eval.v1`) | WER ≤ 8% clean subset / ≤ 12% owner speech |
| `speaker_security` | `speaker_security.json` | EER ≤ 3% and 0 false accepts at the shipped threshold |
| `retrieval_quality` | `retrieval_quality.json` (`ev.retrieval.eval.v1`) | nDCG@10 ≥ 0.80, top-5 hit ≥ 90% |
| `face_recognition` | `face_recognition.json` | TAR ≥ 95% @ FAR 1e-3, 100% stranger rejection |
| `wake_reliability` | `wake_reliability.json` | ≤ 1 false accept per 12 h, recall ≥ 90% |
| `grounding` | measured in-process (Agent 16 corpus) | ≥ 95% ungrounded flagged, ≤ 5% false removal |

Each artifact path can be overridden with `EV_<GATE>_EVAL_REPORT`
(`EV_ASR_EVAL_REPORT`, `EV_SPEAKER_EVAL_REPORT`, `EV_RETRIEVAL_EVAL_REPORT`,
`EV_FACE_EVAL_REPORT`, `EV_WAKE_EVAL_REPORT`, `EV_GROUNDING_EVAL_REPORT`).

Producers:

```text
ASR:    uv run python -m app.voice.asr eval ...        # Agent 4
Speaker: uv run python -m app.voice.speaker eval ...   # Agent 5
Retrieval: uv run python -m eval.retrieval.cli retrieval --out eval/ml/retrieval_quality.json  # Agent 8
Face:   uv run python -m app.people.eval ... --report eval/ml/face_recognition.json           # Agent 7
Wake:   Agent 3 wake eval over trained openWakeWord head
```
