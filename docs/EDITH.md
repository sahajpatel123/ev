# EDITH Software Layer — Oracle (Agent 15)

**Status:** live in the working tree; owned by Agent 15 (Oracle).

This is the layer the owner actually experiences: HUD cards, tactical
briefings, radars, research, gear telemetry, and the digital twin. It is
software-first — the AR hardware is out of scope, but every output is
schema-locked so any future Watch/widget/AR renderer can consume it unchanged.

## Real signal feeds (no synthetic placeholders)

| Signal | Source agent / module | EDITH consumer |
| --- | --- | --- |
| Calendar | Agent 12 → live events (`calendar.event.updated`) | `app.ev.calendar` → HUD status, route briefings, tactical briefings, EV Sense, alert radar |
| Screen / location / audio / health live events | Agent 13 → live channels | `app.ev.live` → EV Sense, user state, alert radar, HUD |
| People | Agent 7 entities + calendar attendees | Tactical briefings (`people`), person finder (`/v1/people/*`) |
| Health | Agent 12 ingestion → `health_snapshots` | `app.ev.health_radar` → readiness, anomalies, morning brief, HUD, twin |
| Retrieval | Agent 8 (`Retriever`) | Tactical briefings, research conclusions |

`app.ev.calendar` is the consumer-side bridge for calendar data. It reads
stored live events from active calendar integrations, derives the compact
signals (`next_event`, `leave_by`, `day_density`, `deadline_proximity`,
participants), and attaches the live-event ids as provenance. Every EDITH
surface that talks about timing now cites those ids instead of inventing a
deadline.

## Surfaces and schemas

| Surface | Endpoint / module | Schema |
| --- | --- | --- |
| HUD status card | `GET /v1/hud/card` | `ev.hud.card.v1` |
| HUD alerts | `GET /v1/hud/alerts` | `ev.hud.alert.v1` |
| HUD focus | `GET /v1/hud/focus` | `ev.hud.focus.v1` |
| HUD ops card | `GET /v1/hud/ops` | `ev.hud.ops.v1` |
| HUD lookouts | `POST /v1/runtime/lookouts`, `ev://present` | `ev.hud.lookout.v1` |
| Route briefing | `GET /v1/hud/route` | `ev.hud.route.v1` |
| Tactical briefing | `POST /v1/tactical/brief` | `ev.hud.briefing.v1` |
| Tactical quick card | `POST /v1/tactical/prepare`, `GET /v1/tactical/quick` | `ev.hud.quickcard.v1` |
| Ops center | `GET /v1/ops/center` | `OpsCenterOut` |
| Digital twin (+ as-of) | `GET /v1/twin?as_of=…` | `TwinOut` |
| Research | `POST /v1/research/sessions`, notes, conclude | `ResearchSessionOut` |
| Gear | `POST /v1/gear/snapshot`, `GET /v1/gear`, `POST /v1/gear/scan` | `GearSnapshotOut`, `GearScanResponse` |

All card/briefing/quickcard payloads pass through `HUD_SCHEMAS` and
`validate_hud`; the contract tests (`test_hud_contract.py`,
`test_ev_hud_twin.py`, `test_tactical_quickcard.py`) enforce 100% schema
conformance. Canonical JSON-Schema files live in `docs/schemas/` for
`ev.hud.card.v1`, `ev.hud.quickcard.v1`, `ev.hud.briefing.v1`,
`ev.hud.focus.v1`, `ev.hud.route.v1`, `ev.hud.alert.v1`,
`ev.hud.ops.v1`, and `ev.hud.lookout.v1`.

## Tactical briefings

`app.ev.tactical.build_briefing` assembles:

- objective and context from the request plus real retrieval (`Retriever`);
- people from memory-linked entities **and** the next live calendar
  commitment's attendees;
- risks from reviewed `decision_outcomes`, with likelihood/impact explicitly
  labeled as EV estimates;
- options from decision history with evidence ids;
- a recommendation that is explicitly labeled rule-based when no model or
  outcome history supports it;
- provenance for every memory and calendar claim; a briefing with no live
  calendar event says so instead of pretending timing is grounded.

## Radars

- **Health radar** (`app.ev.health_radar`): readiness score, z-score
  anomalies, Helio/HealthKit aliases (HR, HRV, SpO2, stress, resp rate),
  clinical emergency bands, morning brief from real `health_snapshots`.
  The iPhone reads Apple Health (Amazfit Helio via Zepp) and POSTs
  `/v1/health/snapshot`. A clinical flag opens a vitals lookout + pulse.
- **Alert radar** (`app.ev.alert_radar`): watchlist matching over events,
  memories, **and live events** (screen/audio/location/calendar/health derived
  fields only), with dedup, priority routing, digest batching, and a labeled
  precision report (`precision_report`) that honestly reports `None` until a
  labeled week exists. The owner labels real alerts through the existing
  dismiss API: `correct`/`useful` = true positive, `incorrect`/
  `false_positive` = false positive.
- **EV Sense** (`app.ev.ev_sense`): intervention score
  (importance × urgency × confidence × goal relevance × benefit), "why now?"
  rationale on every prediction, calendar-deadline signals with live-event
  provenance, outcome calibration, and delivery governed by **Agent 14's**
  `notify.policy.decide` (quiet hours, dedup, daily cap, max attempts) rather
  than a second EV-invented budget.

## Gear telemetry

`app.ev.gear.report` reports only what this Mac can actually observe:

- latest per-device `GearSnapshot` (battery/storage/CPU/memory/uptime);
- a live stdlib-only OS probe (`mac_observed`: storage via `disk_usage`,
  model via `platform`, battery via `pmset`, load average), with `None` and a
  note when a source is unreadable;
- newest on-disk encrypted backup under `storage_root/backups` with computed
  `age_hours`;
- provider health from recent audit rows (no live API call offline);
- model residency from Agent 2's arbiter (`resident_total_mb`, ceiling);
- an explicit `hardware_gaps` list (AR glasses, wearables, live routing,
  OctoPrint, notification delivery ownership).

## Research

- `ResearchService.web_search` only persists results returned by the search
  provider (mock in offline tests, Brave with the user's own key otherwise);
  no citation is ever invented.
- `ResearchService.remember` turns a note into a durable `fact` memory with
  `source_url`, `source_title`, the research session id, and links to the
  note's raw event plus every session event (full provenance).

## Twin / time travel

`GET /v1/twin?as_of=<ts>` returns facts/preferences/goals/patterns as they
stood at that moment using the versioned memory chain (`valid_from` /
`valid_until`, `version`, `supersedes_id`), with source-event provenance on
every item.

## Honest gaps (what is still thin)

- **Alert precision on a labeled week of real signal:** the measurement
  harness exists (`alert_radar.precision_report`) and the labeling path is the
  existing dismiss API (reason `correct`/`useful`/`incorrect`/`false_positive`),
  but no labeled real week exists on this Mac yet, so no real precision number
  is claimed.
- **Mac hardware telemetry:** battery/storage are reported only when a
  collector posts a `GearSnapshot`; this repo does not silently synthesize a
  snapshot.
- **Live routing:** travel time in route briefings is an explicit estimate
  until a real maps API is wired.
- **Quickcard/briefing latency** is measured in-process on this Mac by
  `test_tactical_quickcard.py` and by the standalone probe that produced the
  numbers below; the eval-gates latency gate currently measures only four
  endpoints, so adding tactical endpoints there is a dependency note for the
  ops owner.

## Measured latency (Apple M2, 2026-08-11, warm in-process ASGI)

| Metric | p95 | Budget | Result |
| --- | --- | --- | --- |
| Tactical quick card (`GET /v1/tactical/quick`, cached) | 6.0 ms | 800 ms | pass |
| Tactical briefing (`POST /v1/tactical/brief`) | 32.0 ms | 3000 ms | pass |

## Dependency notes

- Agent 14 (PULSE): EDITH produces alert content; delivery is yours.
- Agent 14 / Conductor: `/v1/sense/predict` still passes
  `budget_override=tuning.daily_budget`; EV Sense now consults
  `notify.policy.decide` regardless, but the API should drop that override so
  Agent 14's budget is the only cap in production.
- Agent 20 / ops owner: extend `eval_gates.run_latency_gate` with
  `tactical_briefing` and `tactical_quick_card` probes (budgets already
  defined in `app.ops.budgets`).
- Agent 5 (SENTRY) / Agent 20: `eval_gates` currently fails outside pytest
  because `EV_VOICEPRINT_PROVIDER=hash` is refused; the gate needs either a
  test-runtime env or a provider override so offline CI can run.
