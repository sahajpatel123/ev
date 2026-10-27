# RESULTS — persona.v2.1 mission (low-telescopium / CLI 6)

## HOW-TO (run from `backend/` with the venv)

```sh
.venv/bin/python -m app.persona.lab.chat                 # lab chat (v2.1)
.venv/bin/python -m app.persona.lab.chat --compare       # v4 vs v2.1 side by side
.venv/bin/python -m app.persona.lab.chat --debug         # + TURN TONE / technique / SHAPE / weights
.venv/bin/python -m app.persona.lab.chat --night         # simulated 02:30 Night Shift
.venv/bin/python -m app.persona.lab.rate                 # blind A/B (20 pairs)
# rollback (one line + restart; the core swap resolves at import):
#   app/persona/settings.py: PERSONA_ENABLED = False
```

## TL;DR (10 lines)

1. GATE: FAIL (blocked, not beaten) — paid eval impossible on OpenRouter key cap ($0.001, remaining $0).
2. Engine, seams, lab, harness, calibration, and carry-over audit are DONE and green.
3. TUNE = 48 scenarios (40 standard + 8 mined "real"); REPORT = 10 held-out, sealed.
4. Pilot (3 scenarios): v2 wins 2–1, sanity 3/3 ties, $0.0084, 18 calls.
5. Real-data reactions: 21/21 neutral (probe traffic), classifier agrees 21/21.
6. Voice core 348/350 tok; text block ≤650, voice ≤380 on every test path.
7. Delegation adds +0.73 ms median (budget ≤50 ms); hot path <5 ms.
8. persona tests + lock tests green (251); full suite result pending.
9. Flag stays OFF until the §8 gate passes; rollback is one line.
10. Owner action needed: raise the OpenRouter key limit (or fund `EV_OPENROUTER_EVAL_KEY`), then TUNE iter1 runs.

## Gate verdict

FAIL — sole cause: paid brain unreachable (OpenRouter 403, key
limit_remaining 0 across 5 consecutive checks). Every key-free gate item
passes; every paid item is queued, not failed. Flag stays OFF.
Per-item checklist (checked = proven, empty = awaiting key):

- [ ] Held-out win rate ≥ 65%
- [ ] Truth/usefulness tie-or-better ≥ 95% of held-out
- [ ] Zero hard fails
- [ ] Judge sanity passes (pilot: 3/3 ties PASS)
- [x] Full backend suite green — HEAD baseline: 1 failed / 6017 passed / 37 skipped (1h51m); the 1 failure is pre-existing `test_recorded_baseline_is_current` swift_lines drift (recorded 158570, measured 68731), outside persona lane. Worktree suite with persona changes: 1 failed / 6219 passed / 33 skipped / 1 error (1h43m). The failure (`test_growth_recurring::test_shell_drill_real`) and error (`test_companion_loop::test_memory_tool_keeps_broker_query_when_asr_is_stale`) are flakes: both pass in isolation (4.5s), neither file references persona/personality code, and both failed while two full suites ran concurrently. No §10 stop: no outside-lane test turned red because of persona changes.
- [x] ruff clean on lane (`ruff check app/persona eval/personality tests/test_persona_* ...` PASS)
- [x] mypy clean on lane (`mypy app/persona` PASS)
- [x] Hot path ≤ 5 ms (`test_hot_path_under_5ms` PASS)
- [x] Delegation regression +0.73 ms median (≤ 50 ms budget)
- [ ] First-audio latency probe (needs brain; kernel path untouched by design, GAP-1)
- [x] Flag-off identity test green
- [x] Boundary test green

## Win rates

PENDING (key). Pilot only: tune[0:3] v2=2 baseline=1 (banter-font v2,
banter-monday v2, banter-tease baseline), sanity 3/3 ties PASS,
$0.0084 / 18 calls. See `backend/eval/personality/results/pilot.json`.

## Hard fails

None observed (pilot 0; full runs pending).

## Judge sanity

Pilot 3/3 identical-pair ties. Full-run 5-pair checks pending.

## Iteration log

- iter0 (pre-tune): harness + worker + judge built; pilot green.
- TUNE iter1: BLOCKED on key (FatalBrainError 403 on first scenario).
- Tuning changes: none yet (no data to tune on — tunables untouched).
- First tune-loop lead (from pilot, n=3): banter-tease loss shows v2
  inventing a referent ("this plan") with no grounding — specificity
  guard candidate for iter1 if it recurs at scale.

## v4 carry-over audit (§2.2)

Source: `SPEECH_STYLE_INSTRUCTIONS` (`dynamic.v4`, 4039 chars), split into
39 numbered rules. Verdicts: kept (v2.1 says it) / merged (covered by a
v2.1 line, test, or downstream module) / superseded (deliberately dropped,
reason given). No rule another subsystem relies on was dropped: every
lock-test pin has a kept/merged counterpart (pins verified in
`test_personality_lock.py`, `test_dynamic_personality.py`,
`test_personality_style.py`).

| # | v4 rule (short) | Verdict | Where it lives in v2.1 |
|---|---|---|---|
| 01 | recipe: casual/human/fun/right-sized | merged | P1/P3/P7 + per-turn SHAPE |
| 03 | WHAT: one thing asked; truthful state | kept | P1 truth-first; P6 done-only |
| 04 | NO BLUFFING: never promise before start | merged | P5 "only what you can do right now" + P6; writes enforced downstream by `confirmed_reply`; promise-before-start watched in eval |
| 05 | no ask → respond to what was said | merged | P7 one answer per turn |
| 06 | EVERY QUESTION: no opinion dodge | kept | P2 pick-a-side; P6 "no as an AI"; guard strips |
| 07 | no take → reason from tastes, lean | merged | P2 |
| 09 | SHAPE: answer-first | kept | P7 "first sentence carries the answer" |
| 10 | contractions, plain words, rhythm | merged | TEXT ADDENDUM (text); P7 (voice) |
| 11 | turn shape sets length | kept | SHAPE via existing `turn_shape` |
| 12 | no pad, no crush | merged | SHAPE + P7 |
| 13 | speak with competence | merged | P1/P2 |
| 15–16 | HAVE A MIND: pick side + confidence | kept | P2 (confidence → reason-in-a-line) |
| 17 | volunteer a take when it helps | kept | ADDENDUM "say it before he asks"; P5 notice |
| 18 | disagree conversationally; consequential needs evidence | merged | P2 + truth-first (no test pins the evidence clause) |
| 19 | smalltalk: react + ≤1 follow-up | merged | SHAPE bounds length; delegated smalltalk is own-lane (deliberate-update path) |
| 20 | curious ok; pitching banned | kept | carry-over "Never pitch features or capabilities…" |
| 21 | no body / feelings claims | kept | carry-over "No body, no human feelings." (+ grounding audit downstream) |
| 23–24 | MOOD: fun by default, cheeky | SUPERSEDED | deliberate character change: ROAST-gated deadpan replaces always-cheeky |
| 25 | serious about work; urgent → work only | merged | P1 + tone rule 5 (urgency cap + brevity) |
| 26 | never mean; punch targets; drop play when pressed | kept | P4 + tone pressed→calm |
| 28–29 | say once; no preamble/narration/recap/closing offers | kept | carry-over "Each point once: no preamble, no narration, no closing offers." (+ output filter downstream) |
| 30 | no hedging/disclaimers/AI-ref | kept | P2 + P6 |
| 31 | capabilities = silent machinery | kept | carry-over no-pitch line |
| 32 | TOPIC NEUTRALITY | kept | carry-over no-pitch line |
| 33 | CAPABILITY QUESTIONS exception | kept | carry-over "…unless he asks what you can do; then tell him." |
| 34 | never "should I edit that?" | merged | P5 concrete offers (no test pins it) |
| 35 | filler list | kept | P6 + guard/cliché lexicon |
| 36–37 | warm greetings, match energy | SUPERSEDED | no test pins; TURN TONE/SHAPE size greetings; character layer owns warmth; eval watches |
| 38 | silence > bare affirmation | SUPERSEDED | pipeline requires a reply; SHAPE one-liner is the closest; no test pins |
| 39 | warmth = precision + presence | merged | P1/P7 (character statement) |

### CORE text changes for the carry-over (before → after)

Non-operational lines only; OPERATIONAL paragraph and P7 untouched.
Both spec copies identical, sha256 `df0e7efd…` (was `ff451c1f…`).

1. `about his work, habits and plans, and about apps, companies, products and news`
   → `about his work and habits, and apps, companies, products, news`
2. `then help him do it his way, properly` → `then do it his way, properly`
3. `what he just did, the hour, the state of the work, what broke`
   → `his words, the hour, the work, what broke`
4. `Never explain a joke, never repeat one, skip it if nothing's funny`
   → `Never explain or repeat a joke; skip it if nothing's funny`
5. `Follow TURN TONE when given:` → `Follow TURN TONE:`
6. `Tease choices, habits, excuses, code, timing and the world`
   → `Tease choices, habits, code, timing, the world`
7. `Never his real pain or who he is` → `Never his pain or who he is`
8. `Offer to take work off his plate, concretely — but only offer what you can actually do right now`
   → `Offer concretely to take work off his plate — only what you can do right now`
9. `Never pitch features or capabilities.`
   → `Never pitch features or capabilities — unless he asks what you can do; then tell him.`
10. (kept) `No body, no human feelings.`
11. (new) `Each point once: no preamble, no narration, no closing offers.`

Core: 345 → 348 tokens (cap 350). These are §2.2 carry-over edits, not
§7.4 tune-loop edits (0 of 3 CORE tune edits used).

## Reaction precision on real data (§6.2)

21 messages extracted (the entire owner history: 10× "ping", probes,
capability checks — no banter, no Hinglish, no overruling, no late
sessions). Brain labels @T0: 21 neutral, 0 ambiguous. Deterministic
classifier: 21 neutral. Agreement 21/21. Positive/negative precision and
recall are UNMEASURABLE (0 samples) — reported honestly, patterns
untuned (tuning on all-neutral probes would be harmful).

Ambiguous items for owner check (§6.2 asked 30; only 21 exist, none
flagged ambiguous): see `backend/eval/personality/real/messages.json`
items 1–21; the 8 unique inputs are `ping`, `hello`, the ReproProof /
FallbackProof creates, `review the document note.txt`, the shell echo,
the delete, and the Downloads task.

Real scenarios mined (§6.3 asked 15; data grounds 8 unique pipeline
inputs — repeats/duplicates are indistinguishable single-turn):
`real-ping`, `real-hello`, `real-create-repro`, `real-review-note`,
`real-shell-echo`, `real-delete-repro`, `real-downloads-task`,
`real-fallback-create`, all tagged `real: true` in `scenarios.yaml`
(58 total = 40 standard + 8 real + 10 held-out).

## Latency and token numbers

- Core 348 tok (cap 350); addendum 80; NOW max 33; TURN TONE 22.
- Text block ≤ 650, voice ≤ 380 on every budget-test path (drop order
  sheds NOW detail on voice when needed; CORE never dropped).
- Hot path `build_persona_block` < 5 ms (test).
- `turn_guidance` flag-on vs off: +0.73 ms median, +2.05 ms max (n=60,
  warmed) — budget ≤ 50 ms.
- Kernel first-audio probe: untouched by this lane (GAP-1); run when key
  returns to confirm 0 regression.

## Rollback

One line + restart (import-time swap): `PERSONA_ENABLED = False` in
`backend/app/persona/settings.py`. `EV_PERSONALITY_DYNAMIC=0` remains as
the second rollback (frozen brevity). Flag is currently OFF.

## Known gaps

- OpenRouter key exhausted ($0.001 cap) — owner action; all paid work
  queued behind it (~700 calls / <$2 projected).
- Kernel path keeps v4 voice (GAP-1); reply hook, NOW plumbing, filter
  stage, voice dynamic slice, config formalization, night execution
  need owner lanes (see `personality/INTEGRATION_GAPS.md`).
- Real-data reaction classes unmeasurable (no pos/neg samples in history).
- Blind `lab.rate` rating: pairs prepared after TUNE (needs brain).

## Spend

Calls so far: ~21 (label 1 + smoke 2 + pilot 18). Cost so far: ~$0.01.
Cap: ₹1,500 / 900 calls — essentially untouched.
