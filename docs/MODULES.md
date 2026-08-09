# EV — Flagship Module Deep-Dives

**Version 1.0** — detailed design for the most advanced modules. Companion to
`PLAN.md` §8 and `REQUIREMENTS.md`.

## 1. Companion core

### 1.1 Relationship memory

- Interaction stats: topic histogram, cadence, active hours, tone preference,
  check-in responsiveness.
- Stored as derived memories (`pattern`/`summary`) + `relationships` edges to people.
- Tone calibration: user sets Directness (1–5) and Warmth (1–5); EV learns from
  corrections ("too chatty", "be blunt") and stores explicit preference versions.

### 1.2 Adaptive check-ins

- Baseline: 1 morning brief + 1 evening reflection prompt (configurable).
- Scaling: high-stress days (health/calendar signal) → +1 midday check-in; quiet
  hours or focus blocks → suppress.
- Every check-in is skippable; three skips in a row reduces that cadence by one step
  until explicitly re-enabled.

### 1.3 Safety

- Guardrails in `UX.md` §9 are enforced in the system prompt *and* by post-hoc
  rubric tests; no dark patterns.

## 2. Health radar

### 2.1 Inputs

HealthKit read-only: HR, HRV (SDNN), resting HR, sleep (stages/duration), steps,
workouts, active/resting energy, manual mood. Daily snapshot at 07:00 + after
workouts.

### 2.2 Readiness score (draft formula)

```text
readiness = 0.35·sleep_quality + 0.25·hrv_baseline_ratio + 0.20·resting_hr_norm
          + 0.10·activity_balance + 0.10·mood_input
```

- `sleep_quality`: hours vs 7.5 target × stage mix.
- `hrv_baseline_ratio`: 7-day rolling HRV median ratio (current/baseline).
- `resting_hr_norm`: normalized deviation from 7-day median.
- `activity_balance`: weekly load vs training readiness (acute:chronic ratio).
- Output 0–100 with band labels (Low/Moderate/Good/Excellent).

### 2.3 Anomaly detection

- Per-metric z-score vs 14-day rolling window.
- Anomalies: |z| ≥ 2 sustained ≥ 2 days, or single-day |z| ≥ 3.
- Each anomaly becomes an alert with trigger ids and rationale; never imputes gaps.

### 2.4 Morning brief

Card: readiness, sleep summary, one recommendation (e.g., "protect an early night —
heavy afternoon ahead"), one open question.

## 3. Alert radar

### 3.1 Watchlist

Items: `{kind: topic|project|person|product|company, value, sources[], priority}`.
Sources (permission-gated): calendar, reminders, bills, RSS/email mentions,
GitHub/repo events, price watch.

### 3.2 Priority scoring

```text
priority = 0.4·urgency + 0.3·user_importance + 0.2·proximity_to_deadline
         + 0.1·pattern_relevance
```

Tiers: urgent (act now), useful (today), background (digest). Notification budget:
default ≤5 actionable/day + digest; quiet hours 22:00–08:00.

### 3.3 Dedup & lifecycle

Fingerprint `(kind, value, source, window)`; statuses `pending → delivered →
dismissed | snoozed | resolved`. Dismissal feeds the pattern engine (e.g., recurring
ignored alerts lower that source's priority).

## 4. EV Sense (predictive layer)

### 4.1 Prediction candidates

Generated from:

- Calendar: next event → briefing, travel time, prep reminders.
- Patterns: repeated behaviors with temporal anchors (renewals, late nights,
  deadlines).
- Health: readiness trends → schedule suggestions.
- Alerts: recurring dismissed alerts → proactive fix suggestion.

### 4.2 Ranking

```text
usefulness = 0.35·predicted_impact + 0.25·context_timing
           + 0.20·past_response_rate + 0.20·pattern_confidence
```

Only top-1 per 4-hour window is delivered by default (Balanced mode); Quiet mode
delivers only urgent; Proactive mode raises to top-2 and widens sources.

### 4.3 "Why now?"

Every prediction carries `trigger_ids` (events/memories) and a rationale sentence
generated from the same signals. `GET /v1/alerts/{id}` expands it.

## 5. Tactical mode

### 5.1 Triggering

- Automatic: calendar events tagged `high-stakes` (negotiation, interview, review,
  first day) or derived from past outcomes (event type historically followed by
  stress/readiness dip).
- Manual: "brief me on X" / "tactical" command.

### 5.2 Pipeline

```text
trigger → retrieve (people, decisions, outcomes, preferences, patterns)
       → assemble briefing context (≤6k tokens)
       → generate ev.hud.briefing.v1
       → validate schema → render target(s)
```

Pre-event briefs run T-24h and T-1h; the T-1h brief is cached for quick cards.

### 5.3 Risk engine

- Risks from past outcomes: memory edges `(situation → outcome)` with sentiment.
- Likelihood: frequency of similar situations with negative outcomes.
- Impact: user-tagged stakes or derived from decision importance.
- Mitigation: generated from options in prior decisions.

### 5.4 Latency design

- Pre-event: full pipeline, ≤3 s (background job; no user wait).
- Quick card: cache + retrieval of delta only, ≤800 ms.
- Voice one-liner: summary field of the briefing, ≤1.5 s TTS.

## 6. Maker companion

### 6.1 Project state machine

```text
idea → planning → sourcing → building → testing → done | paused
```

EV tracks the current step; "next step" returns from project plan or learned sequence.

### 6.2 BOM & inventory

- Items: `{name, qty, unit, location, reorder_at, cost}`.
- Reorder alert at threshold; purchase links optional.
- Build log links each step to a memory (learned outcomes for future projects).

### 6.3 Print queue

- OctoPrint-compatible adapter: submit, status (`queued → printing → done | failed`),
  failure alerts with error logs; estimated time; filament use per job (deducts
  inventory).

