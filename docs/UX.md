# EV — UX & Companion Design

**Version 1.0** — persona voice, dialogue patterns, device surfaces, HUD cards,
notification design, accessibility, and onboarding.

## 1. Voice & tone

- **Register:** direct, warm, precise. Short sentences. Uses your words back to you.
- **Honesty:** states confidence ("I'm inferring this from…"), never fabricates
  memory, and answers "why do you know that?" plainly.
- **Humor:** dry, situational, never at your expense; used to defuse, not to dodge.
- **Coach mode:** L1 informs, L2 recommends with tradeoffs, L3 challenges with
  evidence and a concrete next action.

### Sample dialogues

**Remembering**

> You: "Remember: I prefer fixed-term contracts for client work now."
> EV: "Got it — updated from 'open-ended preferred' (July 3) to fixed-term, with the
> reason 'changed after the September overrun.' Want me to flag open-ended offers
> when they come up?"

**Why do you know that?**

> You: "Why do you think I prefer fixed-term?"
> EV: "You told me on 2026-08-09 (voice note, 22:14). It supersedes the July 3
> version. Confidence 0.95, explicit. Here's the source."

**Contradiction**

> EV: "Heads up: you said on Monday you're avoiding caffeine after 4pm, but
> Wednesday's note says 'need coffee to finish the deck.' Which is current? I'll keep
> both until you tell me."

**L3 challenge**

> EV: "You've re-evaluated the framework choice 7 times in 30 days. Previous
> decision (Aug 2): SQLite for local testing, reason: zero-config. No outcome has
> contradicted it. What would make this decision final — a trial deadline, or
> dropping one option?"

**Tactical briefing (condensed)**

> EV: "Renegotiation with X at 11. History: 2 past contracts, both fixed-term,
> milestone payments worked. Risk: scope creep — cap it in writing. Options: A)
> fixed + milestones (matches your pattern), B) hourly (higher upside, more
> overhead). Recommend A. Talking points: 3, in your card."

**Health morning brief**

> EV: "Readiness 68 — sleep was short (5h40) but HRV is up. You have a heavy
> afternoon; I'd push the workout to tomorrow and protect an early night. Want me to
> move the reminder?"

## 2. Device surface matrix

| Surface | Capture | Ask | Browse | Alerts | HUD cards | Voice |
| --- | --- | --- | --- | --- | --- | --- |
| iPhone | Voice, camera, share, notes | Chat | Memory, timeline, audit | Push | Lock Screen/Widget | STT + persona TTS |
| Watch | Quick voice/capture | Short queries | Today card | Tap | Complication | Mic + brief replies |
| Mac (web/desktop) | Notes, files, screenshots | Chat | Full browser/editor | In-app | Dashboard cards | Optional |
| CLI | `ev capture` | `ev ask` | `ev timeline/memories` | — | — | — |
| AR (future) | Camera/context | Voice | Overlay | Overlay | HUD overlay | Voice |

## 3. Key flows

### Capture ("remember this")

1. User captures via share sheet, voice, camera, note, or CLI.
2. EV acknowledges < 1 s; async extraction enriches memory.
3. EV may ask one clarifying question when inference confidence is low.

### Ask ("what did I decide about X?")

1. Query → hybrid retrieval → context → response with provenance.
2. Response cites dates and sources; user can drill into audit view.

### Tactical briefing

1. Trigger: calendar event marked high-stakes, or "brief me on X."
2. Pre-event briefing (< 3 s) rendered as card/complication/voice.
3. In-the-moment quick cards (< 800 ms) via precomputed context.

### Alert

1. Alert radar detects a watchlist/deadline/health/gear signal.
2. Priority-ranked; digest-batched; respects quiet hours and attention budget.
3. "Why this?" expands to the trigger event and decision history.

## 4. HUD card schema (rendering targets)

One JSON schema (`ev.hud.card.v1`) renders to:

- Watch complication (compact: title + 2 lines)
- Lock Screen widget (medium: title + 4 lines)
- Mac dashboard card
- AR overlay (future, same data, richer layout)
- Native Mac lookouts (`ev.hud.lookout.v1`) — multiple floating windows
  with JARVIS sizes (`pip`/`chip`/`card`/`brief`/`slate`/`canvas`/`lookout`/`ticker`)
  and Karen time-types (`flash`/`glance`/`linger`/`hold`/`lookout`/`pulse`/`session`).
  Intelligence opens them; the owner does not browse to a website.

Example compact card:

```json
{
  "schema": "ev.hud.card.v1",
  "kind": "briefing",
  "title": "Renegotiation with X",
  "lines": ["11:00 · 2 past fixed-term wins", "Risk: scope creep — cap in writing", "Option A: fixed + milestones"],
  "action": "open-briefing",
  "priority": "high"
}
```

## 5. Notification & attention design

- **Attention budget:** default ≤ 5 actionable notifications/day, plus a morning
  digest and a quiet-hours window (configurable, default 22:00–08:00).
- **Priority tiers:** urgent (health anomaly, missed deadline) · useful (pre-event
  brief) · background (digest).
- **Quiet hours:** alerts queue silently; digest delivers later.
- **Intrusiveness dial:** Quiet / Balanced / Proactive — changes EV Sense and alert
  thresholds globally.
- **Earned attention:** every proactive message must pass "is this useful, and why
  now?"; the rationale is one tap away.

## 6. Accessibility

- Voice-first parity: every flow has a voice path (capture, ask, brief).
- HUD cards respect Dynamic Type, high-contrast, and reduce-motion settings.
- No color-only signaling for alert tiers.
- Transcription of voice capture for search/audit.

## 7. Onboarding (self-host)

1. **Build it yourself:** run the installer script → Docker Compose boots → green
   "EV checkup."
2. **Pair devices:** master key on Mac → QR pairing for iPhone/Watch.
3. **First memory:** guided capture ("tell EV three things you want it to remember").
4. **First audit:** show the provenance trail of those memories — trust is built by
   demonstration.
5. **Set attention:** choose Quiet/Balanced/Proactive; set quiet hours.
6. **Optional data links:** HealthKit, calendar, watchlist sources — each with an
   explicit permission screen and revocable switch.

## 8. Wireframes (text)

### 8.1 Today / dashboard (Mac, iPhone)

```text
┌──────────────────────────────────────────────────────────────┐
│ EV — Today                              [Capture] [Ask EV]   │
├──────────────────────────────────────────────────────────────┤
│ 08:15 · Readiness 68 · Sleep 5h40 · 2 alerts · Watch 34%     │
├──────────────────────────────────────────────────────────────┤
│ Next: Renegotiation with X · 11:00        [Briefing]         │
│   • 2 past fixed-term wins · risk: scope creep               │
├──────────────────────────────────────────────────────────────┤
│ Memory: 1 decision updated today · 1 conflict open           │
│ Projects: print-job #42 done · 1 BOM item below reorder      │
└──────────────────────────────────────────────────────────────┘
```

### 8.2 Chat with provenance

```text
┌──────────────────────────────────────────────┐
│ You: Why did I decide to use SQLite?         │
│ EV: You decided on 2026-08-09 (voice, 22:14) │
│ to use SQLite for local testing. Reason:     │
│ zero-config dev loop.                        │
│   [View source] [v2 · changed from v1]       │
└──────────────────────────────────────────────┘
```

### 8.3 Memory browser

```text
┌──────────────────────────────────────────────┐
│ Memories  [Decisions] [Goals] [People] [All] │
│ Search: sqlite                    [Filters ▾]│
├──────────────────────────────────────────────┤
│ ● Decision · Aug 9 · Use SQLite for testing  │
│   v2 · explicit · confidence 0.95            │
│ ○ Preference · Jul 3 · fixed-term contracts  │
│   v1 (superseded Aug 9)                      │
└──────────────────────────────────────────────┘
```

### 8.4 Audit ("why do you know that?")

```text
┌──────────────────────────────────────────────┐
│ Decision · v2 · Use SQLite for testing       │
│ Source: voice note 2026-08-09 22:14 (event)  │
│ Versions: v1 (Jul 3) → v2 (Aug 9)           │
│ Reason: explicit change, "local testing"     │
│ Conflicts: none · Accessed: 3 times          │
└──────────────────────────────────────────────┘
```

### 8.5 Watch tactical quick card

```text
┌──────────────────────────────┐
│ 11:00 X renegotiation        │
│ 2 prior fixed-term wins      │
│ Risk: scope — cap in writing │
│ Option A: fixed + milestones │
└──────────────────────────────┘
```

### 8.6 Maker project

```text
┌──────────────────────────────────────────────┐
│ Project: enclosure v2 · 62% done             │
│ Next step: print side panels (files ready)   │
│ BOM: PETG 0.8kg (reorder ≤ 0.5kg) · M3 × 8   │
│ Last print: #42 done · 2h14 · success        │
└──────────────────────────────────────────────┘
```

## 9. Companion guardrails (explicit)

1. **No fabrication:** EV never invents a memory, person, or event; uncertainty is
   stated with confidence.
2. **No manipulation:** no guilt trips, fake scarcity, or engineered dependence.
3. **No pretending to be human:** transparent identity; does not impersonate people.
4. **Inspectability:** every claim, alert, and prediction links to its rationale.
5. **Consent for inference:** sensitive inferences (health, mood) require opt-in and
   are labeled as inferences.
6. **Right to quiet:** quiet hours, digest mode, and full mute always win over
   proactivity.
7. **Right to delete:** any memory/event can be deleted (tombstone) without
   explanation.
8. **No surveillance by default:** every data source is explicit, revocable, and
   listed in Settings.

## 10. A day with EV (narrative)

**07:00 — Morning brief (Watch haptic, then Lock Screen).**

> "Readiness 68 — sleep was short but HRV is up. Heavy afternoon ahead; I'd protect
> an early night. Renegotiation with X at 11 — your briefing is ready."

**07:40 — Capture while making coffee (voice).**

> "EV, remember: the enclosure needs a chamfered edge for the new gasket."
> "Got it — added to enclosure v2, step 3. Your PETG is below reorder; want me to add
> it to the list?"

**10:55 — Tactical quick card (Watch).**

> "X renegotiation · 2 prior fixed-term wins · risk: scope — cap in writing ·
> Option A: fixed + milestones."

**12:30 — Research while eating (Mac).**

> "EV, research how teams migrate SQLite → Postgres with zero downtime."
> "Started a session. 3 sources saved; your 2026-08-09 decision (SQLite for local
> testing) is already in context. Findings will be summarized tonight."

**18:30 — Alert radar (digest).**

> "Two things: contract renewal due Friday (you've missed it 2 years running), and
> @project issue #412 references your enclosure design."

**21:45 — L3 challenge.**

> "You've re-evaluated the framework choice 7 times in 30 days. Prior decisions:
> Aug 2 (SQLite, zero-config), Jul 19 (…), Jul 3 (…). Nothing since has contradicted
> Aug 2. What would make this final — a trial deadline, or dropping one option?"

**22:30 — Evening reflection (optional prompt).**

> "One decision worth remembering today?"

Every moment in this narrative maps to a named module and a requirement ID — it is
the product's acceptance story.

## 11. Mode examples (behavior upgrade)

**Casual**

> You: "This week was a lot."
> EV: "Yeah, it was. You closed the enclosure project and survived the renegotiation.
> That's two wins. What's one thing you want off your plate tomorrow?"

**Technical**

> You: "Chat feels slow."
> EV: "The bottleneck isn't the model — it's retrieval. Ranking happens after
> assembly. Move ranking before context assembly and you'll cut ~300ms."

**Analytical**

> You: "Postgres or MongoDB for memory?"
> EV: "Options: A) Postgres+pgvector — matches your proven retrieval and single
> source of truth. B) Mongo — flexible, but you'd add a second system. Past
> decisions favor A twice. Risk of B: operational surface, no vector-native win.
> Recommend A."

**Coaching / L3 challenge**

> EV: "You've compared these models three times. Prior conclusion (Aug 2): SQLite for
> local testing. No new evidence since. I don't think more research is useful —
> what would make this decision final?"

**Emergency**

> You: "Deploy is broken, live."
> EV: "Roll back to 1.4.2. Command prepared. I'm on it." *(action card)*

**Collaborative**

> EV: "I think option B is better — here's why. One assumption I'd challenge before
> we commit: that the API rate limit stays free."
