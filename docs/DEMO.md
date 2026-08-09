# EV — Milestone Demo Scripts

**Version 1.0** — scripted walkthroughs that prove each milestone's acceptance
criteria. Each demo lists steps, expected EV behavior, and the requirement it
verifies. A demo that cannot be run means the milestone is not done.

## 1. M0 — Skeleton (15 minutes)

**Setup:** `make install && make compose-up && make migrate`.

1. Open `http://localhost:8000/app` (or `ev capture` in the CLI).
2. Capture: "Remember: I decided to use SQLite for local testing."
   → `201`, event visible in timeline with source/date. *(FR-MEM-01)*
3. Ask: "Why did I decide to use SQLite?"
   → reply cites the memory, date, and source; provenance list returned.
   *(FR-RETR-02, FR-ORCH-03)*
4. Open timeline → event present; open memory browser → decision present.
5. Delete the event → it disappears from timeline; retrieval no longer returns it;
   raw row remains tombstoned (DB check). *(FR-SEC-04)*
6. Export → JSON bundle contains events + memories + access log. *(FR-SEC-04)*
7. `curl /v1/health` → green; access log shows all actions. *(FR-DIAG-01, FR-MEM-10)*

**Gate:** every step returns the expected result; `make test` green.

## 2. M1 — Memory core (20 minutes)

1. Capture: "I decided to use SQLite for local testing."
2. Capture (change): "Actually, I decided to use Postgres for local testing."
   → version v2 created; v1 preserved with `valid_until` and reason. *(FR-MEM-05)*
3. Audit v2 → version chain v1→v2, source events, reason. *(FR-MEM-02)*
4. Temporal query: `GET /v1/memories?as_of=<date before change>` → v1 returned.
   *(FR-MEM-06)*
5. Contradiction: "I love caffeine after 6pm" then "I hate caffeine after 6pm"
   → conflict record open; chat surfaces it. *(FR-MEM-07)*
6. Rebuild test (CLI/script): drop derived tables → rerun pipeline → same memories.
   *(FR-SYS-05)*
7. Export → full bundle; delete → tombstone + redaction. *(FR-SEC-04)*

**Gate:** invariant suite green (provenance, versioning, conflicts, rebuild).

## 3. M2 — App surfaces (30 minutes)

1. Pair iPhone + Mac (QR). *(FR-SEC-01)*
2. Capture voice note on iPhone; share a file from Files app.
3. Within seconds, both appear in Mac timeline and memory browser. *(FR-DEV-03)*
4. Turn on Airplane Mode; capture two notes on iPhone; reconnect
   → notes sync; no duplicates. *(FR-DEV-04)*
5. Two clients capture simultaneously → both events present on both. *(FR-DEV-05)*
6. Watch quick capture → event with transcript + attachment. *(FR-DEV-02)*

**Gate:** all devices show identical timeline; offline queue drains cleanly.

## 4. M3 — Intelligence (25 minutes)

1. Ask a question that needs memory tools: "What did I decide about X?"
   → tool calls observable in logs; bounded results. *(FR-ORCH-02)*
2. Feed a repeated research loop (5+ "should I use X?" events over 30 days)
   → L3 challenge citing prior decisions and outcomes. *(FR-ORCH-04, FR-BHV-10)*
3. Ask in a new conversation "continue where we left off"
   → EV reconstructs active project/goal. *(FR-BHV-04, FR-BHV-05)*
4. Context monitor: instrument provider payload → tokens ≤ budget;
   `never_send_to_model` absent. *(FR-ORCH-01, FR-SEC-03)*
5. Mode check: "25 × 4" → "100." (casual/short); deployment problem → emergency
   action mode. *(FR-BHV-02, FR-BHV-03)*

**Gate:** tool loop terminates; budget respected; challenge cites evidence.

## 5. M4 — Hardening (30 minutes)

1. Run backup job → encrypted snapshot exists.
2. Wipe stack → restore from snapshot → counts match; sample audit trail intact.
   *(FR-SEC-05)*
3. Verify TLS (curl verbose, cert chain) and device revocation. *(FR-SEC-02,
   FR-SEC-01)*
4. Run security suite (auth, boundary, export/delete, restore). *(FR-SEC-01…06)*

**Gate:** restore drill passes; security suite green.

## 6. M5 — EV Advanced slices (per slice, ~20 minutes)

**Health radar:** import synthetic HealthKit data → readiness score; plant an HRV
anomaly → alert within scan window; morning brief renders. *(FR-HEALTH-01…05)*

**Alert radar:** create watchlist; plant deadline + mention events → deduped,
priority-ranked digest; quiet hours suppress. *(FR-ALERT-02…04)*

**Tactical:** schedule a high-stakes meeting → T-1h briefing validates schema;
quick card renders on Watch under 800 ms. *(FR-TACTICAL-01…04)*

**EV Sense:** plant recurring deploy pattern → prediction "deploy checklist"
delivered with why-now rationale. *(FR-SENSE-01…04, FR-BHV-12)*

**Maker:** create project + BOM; drop PETG below reorder → alert; submit print job
→ status transitions + inventory deduction. *(FR-MAKER-01…04)*

**Voice/HUD:** voice capture → transcript event; HUD cards render on widget +
complication. *(FR-VOICE-01, FR-HUD-01…02)*

## 7. Behavior layer (P1–P8) demos

1. **Modes:** run the six scripted exchanges from `UX.md` §11 — each mode must
   match its trigger and constraints. *(FR-BHV-01/02)*
2. **Decision loop:** decide with expected outcome; later record actual outcome →
   lesson memory with provenance. *(FR-BHV-07/08)*
3. **Correction:** "That's wrong" → v2 active, v1 intact. *(FR-BHV-14)*
4. **Forget vs delete:** "Forget this" → excluded from retrieval, auditable;
   delete → tombstone. *(FR-BHV-15)*
5. **Permission matrix:** attempt a denied tool call → logged denial, no action.
   *(FR-BHV-16/21)*
6. **Self-evaluation:** complete a prediction → outcome review → calibration delta
   visible. *(FR-BHV-20)*

## 8. Demo checklist (run before every milestone review)

- [ ] All steps executed with expected results
- [ ] Acceptance criteria from `ROADMAP.md` verified
- [ ] Related FR tests green (`EVALUATION.md`)
- [ ] No manual DB edits used to fake state

