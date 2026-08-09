# EV — Roadmap & Execution Plan

**Version 1.0** — milestones M0–M5 with task breakdowns, dependencies, estimates,
recommended sequencing, and exit gates. Estimates use S/M/L (S ≤ 1 day, M ≤ 3 days,
L ≤ 1 week for a single engineer).

## 1. Dependency graph

```text
M0 Skeleton ──► M1 Memory core ──► M2 App surfaces ──► M3 Intelligence ──► M4 Hardening
                     │                  │
                     │                  └──► M5 EV Advanced (parallel slices)
                     └──► M5 Companion core / tactical / research
```

M5 modules depend on M1 (memory) and mostly on M2 (mobile/Watch surfaces).
Independent M5 slices: companion core (M1+), self-diagnostics (M0+), research
assistant (M1+), maker companion (M1+). Health radar and tactical HUD need M2.

## 2. Recommended sequencing

1. **M0 first** — proves the core loop ("remember → query → provenance") on one
   backend with CLI/web clients. Smallest end-to-end slice of the thesis.
2. **M1 next** — the product's moat: versioning, contradictions, audit, export/delete.
   Without M1, nothing else is trustworthy.
3. **M2 app** — the product surface; enables phone capture and the Watch, which M5
   health/tactical modules need.
4. **M3 intelligence** — coaching, patterns, tools; validates the "model interrogates
   memory" architecture.
5. **M4 hardening** — backups, encryption, restore drills before long-lived personal
   data accumulates.
6. **M5 slices in this order:** companion core → health radar → alert radar →
   tactical mode → research → navigation → maker → voice/HUD → gear telemetry →
   EV Sense (depends on alert/health/pattern maturity).

Alternative (user's call): an EV-Advanced **vertical slice** (tactical mode + health
radar) on a minimal backend — faster demo, but higher rework risk for memory
invariants later.

## 3. M0 — Skeleton

| ID | Task | Size | Deps |
| --- | --- | --- | --- |
| M0.1 | Repo, pyproject, ruff/mypy/pytest, CI skeleton | S | — |
| M0.2 | Docker Compose: Postgres/pgvector, Redis, MinIO, API, worker | M | — |
| M0.3 | Domain models + Alembic migration | M | M0.1 |
| M0.4 | Event ingestion API (immutable + tombstone) | M | M0.3 |
| M0.5 | Processing pipeline (sync + RQ) | M | M0.4 |
| M0.6 | Embeddings + hybrid retrieval (basic) | M | M0.3 |
| M0.7 | Chat endpoint + context assembly | M | M0.5, M0.6 |
| M0.8 | Memory browser + timeline API | M | M0.4 |
| M0.9 | CLI client (`ev capture`, `ev ask`, `ev timeline`) | S | M0.4–M0.8 |
| M0.10 | Minimal web client | S | M0.7 |
| M0.11 | Self-diagnostics endpoint + health checks | S | M0.2 |
| M0.12 | Test suite: invariants, API round-trip, auth | L | M0.4–M0.9 |

**Exit gate:** "remember this" → query returns memory with source; events immutable;
`uv run pytest` green; compose stack boots.

## 4. M1 — Memory core

| ID | Task | Size | Deps |
| --- | --- | --- | --- |
| M1.1 | Extraction engine (rules + interface for LLM extractor) | M | M0.5 |
| M1.2 | Entities + relationships + entity extraction | M | M0.3 |
| M1.3 | Typed memory model: decisions/goals/preferences/facts | M | M1.1 |
| M1.4 | Versioning (version groups, supersede, valid range) | M | M1.3 |
| M1.5 | Contradiction detection + conflict records | M | M1.4 |
| M1.6 | Importance scoring + confidence model | S | M1.1 |
| M1.7 | Audit endpoint ("why do you know that?") | M | M1.4 |
| M1.8 | Export (full bundle) + delete (tombstone + redaction) | M | M0.4 |
| M1.9 | Access log on all read/write paths | S | M0.3 |
| M1.10 | Retrieval eval harness (seeded corpus, top-5 target ≥80%) | L | M1.3, M0.6 |
| M1.11 | Invariant tests: regeneration, provenance, versioning, conflicts | L | M1.4–M1.9 |

**Exit gate:** "why did I decide X?" answers with provenance; preference change yields
v2 with intact v1; contradictions produce records; drop-all-derived → regenerate →
equivalent state.

## 5. M2 — App surfaces

| ID | Task | Size | Deps |
| --- | --- | --- | --- |
| M2.1 | iOS app shell + auth + device registration | M | M0.2 |
| M2.2 | Voice capture + share sheet + camera + notes | M | M0.4 |
| M2.3 | AI chat screen with streaming | M | M0.7 |
| M2.4 | Memory browser + timeline + audit view | M | M0.8, M1.7 |
| M2.5 | Watch app: quick capture + today card | M | M2.1 |
| M2.6 | Offline capture queue + sync | M | M2.2 |
| M2.7 | Mac desktop/web polish + CLI parity | M | M0.9–M0.10 |
| M2.8 | Multi-device sync consistency tests | M | M2.6 |

**Exit gate:** capture from iPhone or Mac appears on all devices within seconds;
offline captures sync on reconnect.

## 6. M3 — Intelligence

| ID | Task | Size | Deps |
| --- | --- | --- | --- |
| M3.1 | Memory tools (search/decisions/timeline/patterns) + tool loop | M | M1.3, M0.7 |
| M3.2 | Hierarchical retrieval (entity-first descent) | M | M1.2, M0.6 |
| M3.3 | Context monitor (budget, trimming, progressive retrieval) | M | M0.7 |
| M3.4 | Pattern engine (frequency, first/latest, confidence) | M | M1.3 |
| M3.5 | Coaching engine L1→L2→L3 with decision-loop detector | M | M3.4 |
| M3.6 | Proactive notification service (bounded, quiet hours) | M | M3.5 |
| M3.7 | Orchestrator behavior tests (bounds, termination, budget) | L | M3.1–M3.6 |
| M3.8 | Interaction Intelligence: modes, tone selection, response length (P1) | L | M3.1 |
| M3.9 | User State Engine: activity/project/goal/task/focus (P2) | M | M1.3 |
| M3.10 | Decision follow-up loop: outcomes + lessons (P3) | M | M1.3 |
| M3.11 | Behavior kinds: loops, drift, tool churn, abandonment (P4) | M | M3.4 |
| M3.12 | Intervention scoring + delivery policy (P5) | M | M3.6 |

**Exit gate:** repeated research loop triggers Level-3 challenge citing previous
decisions; context never exceeds budget; `never_send_to_model` absent from prompts.

## 7. M4 — Hardening

| ID | Task | Size | Deps |
| --- | --- | --- | --- |
| M4.1 | Threat model review + fixes | M | M0+ |
| M4.2 | At-rest encryption + key management | M | — |
| M4.3 | Encrypted backups + restore drill | M | — |
| M4.4 | TLS in transit (Caddy/nginx + Tailscale) | S | — |
| M4.5 | Performance pass (indexes, queries, caching) | M | M0+ |
| M4.6 | Long-horizon consolidation (monthly/yearly summaries) | M | M3.4 |
| M4.7 | Security test suite (auth, boundary, export/delete, restore) | L | M4.1–M4.6 |

**Exit gate:** restore drill verified; export/delete verified; latency budgets met.

## 8. M5 — EV Advanced (slices)

| ID | Slice | Tasks | Size | Deps |
| --- | --- | --- | --- | --- |
| M5.1 | Companion core | Persona prompt, tone calibration, relationship memory, adaptive check-ins | M | M1, M3.5 |
| M5.2 | Health radar | HealthKit import, readiness score, trends, anomalies, morning brief | L | M2 (iOS/Watch) |
| M5.3 | Alert radar | Watchlist, calendar/deadline/bill/news sources, priority + digest | L | M2, M3.6 |
| M5.4 | Tactical mode | Briefing schema, quick cards, risk engine, latency budgets | L | M2, M3.1 |
| M5.5 | Research assistant | Sessions, sources, citations, memory saves | M | M1 |
| M5.6 | Navigation | Route-to-event briefings, leave-by alerts | S | M2 |
| M5.7 | Maker companion | Projects, BOM, inventory, OctoPrint queue | M | M1 |
| M5.8 | Voice & HUD | STT/TTS, wake word, HUD schema rendering, widgets/complications | M | M2 |
| M5.9 | Gear telemetry | Device/watch battery, backup health, diagnostics | S | M2 |
| M5.10 | EV Sense | Predictive layer over patterns + health + alerts; inspectable | L | M5.2, M5.3, M5.4 |
| M5.11 | Personality engine (P7) | Structured profile, mode adaptation, consistency invariants | M | M3.8 |
| M5.12 | Self-evaluation + prediction tracking (P8) | Response log, outcome reviews, calibration | M | M3.10 |
| M5.13 | Tool orchestration extension (P6) | Web/file/code/API tools, selection intelligence, sandbox | L | M3.1 |
| M5.14 | Constructive challenge system | L0–L4, evidence gates, permissions, escalation log | M | M3.5, M3.11 |
| M5.15 | Relationship model + memory explainability/forgetting | Evidence-backed stats; forget vs delete | M | M1.4, M1.7 |

Behavior priorities P1–P8 (see `BEHAVIOR.md` §22) land in M3/M5 as above; P9
(advanced autonomy) is explicitly post-M5.

**Exit gate per slice:** vertical slice demo + tests + acceptance criteria from
`PLAN.md` §12; HUD schema validates; quiet-hours notification rules enforced.

## 9. Suggested calendar (single engineer, estimates)

| Weeks | Focus |
| --- | --- |
| 1–2 | M0 (skeleton + CLI + web + compose) |
| 3–5 | M1 (memory core + eval + invariants) |
| 6–9 | M2 (iOS/Watch + sync + offline) |
| 10–12 | M3 (tools, coaching, notifications) |
| 13–14 | M4 (hardening + backups) |
| 15+ | M5 slices, ordered: companion → health → alerts → tactical → research → navigation → maker → voice/HUD → gear → EV Sense, with behavior layer (P1–P8) woven through M3/M5 |

Estimates assume one full-time engineer; parallelization across workers can compress
M0–M2 (backend, mobile, infra are separable).

## 10. Testing gates per milestone

| Milestone | Gate |
| --- | --- |
| M0 | pytest green; compose up; capture→query round-trip |
| M1 | invariant suite green; retrieval top-5 ≥80%; eval report |
| M2 | multi-device sync test; offline queue test |
| M3 | tool-loop bounds; budget; challenge trigger demo |
| M4 | restore drill; security suite green |
| M5 | per-slice vertical demo + HUD schema + alert precision tests |
