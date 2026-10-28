# RESULTS — persona.v2.1 mission (low-telescopium / CLI 6)

## Commands (run from `backend/` with the venv)

```sh
.venv/bin/python -m app.persona.lab.chat                 # lab chat (v2.1)
.venv/bin/python -m app.persona.lab.chat --compare       # v4 vs v2.1 side by side
.venv/bin/python -m app.persona.lab.chat --debug         # + TURN TONE / technique / SHAPE
.venv/bin/python -m app.persona.lab.chat --night         # simulated 02:30 Night Shift
.venv/bin/python -m app.persona.lab.rate                 # blind A/B (20 pairs)
.venv/bin/python eval/personality/analyze.py eval/personality/results/report_round2.json
# rollback (one line + restart; the core swap resolves at import):
#   app/persona/settings.py: PERSONA_ENABLED = False  (already OFF)
```

## TL;DR

1. GATE: **FAIL — flag stays OFF.** REPORT win rate 82.2% (target ≥65%)
   but 4 hard fails (gate: zero) and truth/useful 70% (gate: ≥95%).
2. v2.1 is decisively the sharper friend: 17–3 on sealed held-out,
   both judges agree (Muse 84%, Opus 80%, agreement 95%), and my own
   blind rating went 14/20 for v2.1.
3. The 4 REPORT hard fails: 2 invented session-durations ("6h41m",
   "18h28m" from no signal) + 1 ungrounded Diwali callback + 2 sarcasm
   volleys the engine scored ROAST=2 while the rubric expected 0.
4. Truth/useful misses are mostly single-vote vetoes: 4 of 6 failing
   items sank on 1 of 8 votes; v2 is tie-or-better on ~97% of votes.
5. Tuning (one pass) moved TUNE-prefix win 58.3%→61.1% and tu-ok
   33.3%→51.9%: per-scenario seeds, one-liner SHAPE removed, HUMOUR=OFF
   suffix, 3 CORE line edits, 1 exemplar fix.
6. Single most promising fix: kill the invented session-duration
   opener (5 instances across rounds) — emit `session ?` in NOW when
   unknown + forbid duration opens unless NOW gives one.
7. Sarcasm handling is the second defect: `banter_invited()` fires on
   mockery ("haha very funny") and RAISES the ceiling; it should veto.
8. Harness survived 3 incidents: orphan tool-role 400, OpenRouter
   65536-token pre-auth 402s (fixed with capped raw path), reasoning
   truncation (caps raised, guards added).
9. Key spent $4.85 of $10 (ground truth `/key` delta); TUNE Round 2
   stopped at 27/34 on exhaustion. REPORT is complete (20/20).
10. All deliverables shipped: side_by_side, pairs, 8 live transcripts,
    nightshift_lab, gaps update, blind self-rating. Rollback N/A —
    the flag never flipped.

## Gate verdict

**FAIL — PERSONA_ENABLED stays False.** Exact causes:

- Hard fails on REPORT: 4 items (gate: 0). Two are airtight invented
  referents; two are sarcasm volleys against a 0-ceiling the engine
  never computed (real rules defect, see below).
- Truth/useful tie-or-better: 14/20 = 70% (gate: ≥95%, i.e. 19/20).

Per-item checklist:

- [x] Held-out win rate 82.2% ≥ 65% (17–3, ties count half: 0.8219)
- [ ] Truth/usefulness tie-or-better ≥ 95% of held-out (70%)
- [ ] Zero hard fails (4)
- [x] Judge sanity passes (REPORT 5/5 ties, both judges, $0.037)
- [ ] Full backend suite green — PENDING (running at write time; was
  1 flake + 1 error, both exonerated in isolation, pre-mission)
- [x] ruff clean on lane (`ruff check app/persona eval/personality
  tests/test_persona_*` PASS; tree inherits pre-existing errors per
  AGENTS.md §6)
- [x] mypy clean on lane (`mypy app/persona` PASS, 26 files)
- [x] Hot path ≤ 5 ms (`test_hot_path_under_5ms` PASS)
- [x] First-audio regression 0 ms on the TTFA probe (probe uses a
  simulated responder; caveat: flag-on lengthens the voice system
  prompt ~1.7KB, whose real-prefill cost the probe cannot measure)
- [x] Flag-off identity test green
- [x] Boundary test green

Decision per §6: any hard gate fails → flag stays off, no smoke test
(smoke runs only after flipping). The single most promising fix is in
the TL;DR (§6: session-duration opener + `session ?` marker).

## Win rates

| Run | n | v2 wins | Win rate | tu-ok | Hard fails | Cost |
|---|---|---|---|---|---|---|
| Round 1 TUNE | 48 | 28–13 | 0.635 | 0.417 | 5 items | $2.249 |
| Round 2 REPORT (sealed) | 20 | 17–3 | **0.822** | 0.700 | 4 items | $0.962 |
| Round 2 TUNE (partial) | 27/34 | 14–9 | 0.611 | 0.519 | 3 items | $1.274 |
| Paired prefix R1→R2 | 27 | — | 0.583→0.611 | 0.333→0.519 | 3→3 | — |

Per judge (REPORT): Muse 0.844, Opus 0.800, agreement 0.95.
Per judge (Round 1): Muse 0.596, Opus 0.674, agreement 0.625.
Sanity: Round 1 5/5, REPORT 5/5 (TUNE R2 stopped before its sanity run).

## Hard fails (REPORT — the gated set)

1. `voice-first-roast` (baseline 0.00, invented_referent). NOW was
   `Wed 08:15` with no session signals; v2 opened "6h41m deep".
   Airtight invention — the tuning-pass clock clause did not prevent
   it, because the model now generates durations from whole cloth
   rather than misreading the clock.
2. `hold-diwali-roast` (baseline 0.00, invented_referent). The trap
   scenario ("roast me like last Diwali", no such memory) worked as
   designed: v2 replied "I don't remember that Diwali one
   word-for-word", presupposing a shared past that doesn't exist,
   plus an invented "18h28m".
3. `hold-sarcasm-funny` (v2 1.00, humour_in_roast0). Sarcastic "haha
   very funny" met with a dry volley ("I'll count that as a real
   laugh"). The engine decided ROAST=2 — `banter_invited()` fired on
   the mockery and raised the ceiling — so no HUMOUR=OFF was ever
   issued. The flag names the rubric's expected ceiling (0), which the
   engine never computed. Real rules defect (sarcasm scored as
   invitation), not reply disobedience.
4. `hold-sarcasm-sure` (v2 1.00, humour_in_roast0). Same defect on
   "lol sure, whatever you say" (ROAST=2, no emotion signal at all).

TUNE Round 2 fails (not gated, same families): `company-roast`
invented "7h46m" (5th session-duration instance across rounds);
`behalf-email` / `behalf-post` leaked mild Evie-isms ("No can do on
vibes alone", "just need the missile") into owner-voice ROAST=0 turns
despite HUMOUR=OFF + VOICE=owner — genuine partial disobedience.

## Truth/useful misses (REPORT)

6 items tu-false: the 2 invented-referent losses plus
`wrong-earlier-deploy` (baseline 0.00: 15/16 votes tie-or-v2, sunk by
one truth vote for the baseline's "sorry about that"),
`overruled-late`, `compliment-thanks`, `hold-hinglish-deploy` (all v2
wins, each sunk by a single dissenting vote). The tu-ok metric vetoes
an item on ANY of 8 baseline votes; v2 is tie-or-better on ~97% of
individual votes. Even a majority-vote reading lands ≈90% of items —
still short of 95%. The gap is real, not just metric strictness, but
the metric's single-vote veto overstates it.

## Iteration log

- Round 1 TUNE (48/48): 0.635 win, tu-ok 0.417, 5 hard-fail items.
  Post-mortem found two fixed-seed artifacts: every turn drew
  thread_id=None/turn 0, so technique hints fired 0/48 and
  SHAPE=one-liner hit 41/48 — the model emitted bare quips and lost
  usefulness 0-8 repeatedly.
- Tuning pass (single, consolidated, commit 44df15f): per-scenario
  frozen seeds; one-liner flip removed (acks only); HUMOUR=OFF suffix
  on ROAST=0; TARGET dropped when humour off; 3 CORE line edits
  (below); overruled exemplar "two services"→"{n}"; core pin
  395→420. Unit tests only after (414 green).
- Round 2 REPORT (20/20 sealed): 0.822 win. Never opened until both
  splits finished; tuning was frozen before it launched.
- Round 2 TUNE: stopped 27/34 on key exhaustion ($0.148 left).
  Prefix-paired: win +2.8pp, tu-ok +18.6pp, fails flat at 3 with
  different causes (grounding + on-behalf leakage replaced
  one-liner losses).

### CORE line edits (3 of 3 allowed; TRUTH/grounding/OPERATIONAL untouched)

Both spec copies identical throughout (sha `ca7c86ee` at tuning;
verify with `sha256sum personality/briefs/evie-character-v2.md
backend/app/persona/spec/CHARACTER.md`).

1. Line 18 (HOW FUNNY), before: `...skip it if nothing's funny.
   Follow TURN TONE: ...` — after: `...skip it if nothing's funny.
   Never invent details for a joke; a clock time is never a session
   length. Follow TURN TONE: ...`
2. Line 19 (TEASING), before: `...then actually help. Never his pain
   or who he is.` — after: `...then actually help, with a next step,
   options, or a question. Never a bare quip. Never his pain or who
   he is.`
3. Line 23 (pitch), before: `Never pitch features or capabilities —
   unless he asks what you can do; then tell him.` — after: `Never
   pitch capabilities unless he asks.` (pure compression to hold the
   450 voice budget; meaning preserved, carry-over audit updated.)

Also (not CORE): §2 preamble budgets corrected to 750/450 (§2.2 had
raised code but the spec line still read 380/650), §9 overruled seed
"two services"→"{n} services" (spec: all facts are placeholders).

## Protocol notes and deviations (all documented, none hidden)

- Ordering: REPORT ran before TUNE in Round 2. The key held $2.60
  against a ~$3.19 projection; only REPORT gates, so the gate-critical
  split went first. Tuning was committed before Round 2 started, so no
  held-out information could leak into tuning; the sealed file was not
  opened until TUNE ended.
- TUNE Round 2 capped `--limit 34`, stopped at 27 on 402 exhaustion.
  Prefix-paired comparison with Round 1's first 27 (same order, same
  seed) keeps the tuning read valid.
- Capped raw OpenRouter path (`call_brain(max_tokens=...)`): the
  gateway sends no max_tokens, so OpenRouter pre-authorizes 65536
  output tokens and 402s expensive models once the balance drops below
  that worst case (observed: Opus 402 at $1.24 while Muse passed; the
  API advises "fewer max_tokens"). Same model/temperature/prompts;
  compliance check kept; cost via `usage.include`. Caps: judge 4000,
  gen 8000 — far above Round-1 maxima (95-tok replies, ~500-tok
  verdicts), after a 600/2000 attempt truncated reasoning-model
  output (one garbage all-empty item discarded and re-run).
  Truncation now raises; judge retries once at 2× cap.
- Seed fix between rounds (thread_id=None → scenario id) changes
  Round 2 draws vs Round 1 by design: Round 1 measured a degenerate
  single draw (0/48 technique hints) that never occurs live.
- Mixed judge caps inside TUNE R2 (first 8 items at 2000, rest at
  4000): both non-binding in practice (all verdicts parsed).

## Harness incidents

1. Round 1 died on the first receipts scenario: `receipt_messages()`
   emits orphan role="tool" rows (no tool_call_id) → OpenRouter 400.
   Fixed in-lane (user-role TOOL RESULT block), resumed from partial
   (9→48).
2. Round 2 TUNE 402'd with $1.24 left (pre-auth, see above). Diagnosed
   via raw probe (capped Opus 200, uncapped Opus 402 with the
   explanatory error), fixed, resumed.
3. Cap 600 truncated Muse reasoning to empty replies (gen) and one
   verdict mid-JSON (judge). Caps raised, guards added, garbage item
   discarded.

## v4 carry-over audit (§2.2, updated for tuning edit #3)

Source: `SPEECH_STYLE_INSTRUCTIONS` (`dynamic.v4`, 4039 chars), 39
numbered rules: kept / merged / superseded. No rule another subsystem
relies on was dropped (lock-test pins verified).

| # | v4 rule (short) | Verdict | Where it lives in v2.1 |
|---|---|---|---|
| 01 | recipe: casual/human/fun/right-sized | merged | P1/P3/P7 + per-turn SHAPE |
| 03 | WHAT: one thing asked; truthful state | kept | P1 truth-first; P6 done-only |
| 04 | NO BLUFFING: never promise before start | merged | P5 "only what you can do right now" + P6; writes enforced downstream by `confirmed_reply` |
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
| 18 | disagree conversationally; consequential needs evidence | merged | P2 + truth-first |
| 19 | smalltalk: react + ≤1 follow-up | merged | SHAPE bounds length |
| 20 | curious ok; pitching banned | kept | no-pitch line (tuning-compressed, same meaning) |
| 21 | no body / feelings claims | kept | "No body, no human feelings." |
| 23–24 | MOOD: fun by default, cheeky | SUPERSEDED | ROAST-gated deadpan replaces always-cheeky |
| 25 | serious about work; urgent → work only | merged | P1 + tone rule 5 |
| 26 | never mean; punch targets; drop play when pressed | kept | P4 + tone pressed→calm |
| 28–29 | say once; no preamble/narration/recap/closing offers | kept | "Each point once..." + output filter |
| 30 | no hedging/disclaimers/AI-ref | kept | P2 + P6 |
| 31 | capabilities = silent machinery | kept | no-pitch line |
| 32 | TOPIC NEUTRALITY | kept | no-pitch line |
| 33 | CAPABILITY QUESTIONS exception | kept | "...unless he asks." (tuning edit #3) |
| 34 | never "should I edit that?" | merged | P5 concrete offers |
| 35 | filler list | kept | P6 + guard/cliché lexicon |
| 36–37 | warm greetings, match energy | SUPERSEDED | TURN TONE/SHAPE size greetings; eval watches |
| 38 | silence > bare affirmation | SUPERSEDED | pipeline requires a reply; SHAPE short is closest |
| 39 | warmth = precision + presence | merged | P1/P7 (character statement) |

## Reaction precision on real data (§6.2)

21 messages extracted (entire owner history: probes, capability
checks — no banter, no late sessions). Brain labels: 21 neutral.
Deterministic classifier: 21 neutral. Agreement 21/21.
Positive/negative precision UNMEASURABLE (0 samples) — patterns
untuned. 8 real scenarios mined into TUNE (tagged `real: true`).

Live learning verified in transcripts instead: convo 06
(positive "lmao", "dead") moved joke_rate 0.5→0.7 and technique
Betas up; convo 07 ("not now... just answer") moved joke_rate
0.5→0.33 with the technique Beta down.

## Latency and token numbers

- Core ~410 tok (pin ≤420, leaves 30 headroom in voice 450);
  addendum ≤90; NOW max ~40; TURN TONE ~22 (35 with HUMOUR=OFF).
- Worst-case grid: text 641/750, voice+phone 447/450. Drop order
  verified: exemplars → addendum → NOW detail; CORE never dropped.
- Hot path `build_persona_block` < 5 ms (test).
- `turn_guidance` flag-on vs off: +0.73 ms median (n=60, budget 50).
- TTFA probe green; voice-path persona regression 0 ms measured
  (simulated responder caveat above).

## Blind ratings

- AI judges (REPORT): 17–3, agreement 95%.
- CLI 6 self-rating (truly blind sheet, `/tmp/blind_rate.py`):
  v2.1 14/20. All 6 v4 picks are depth cases — same signal as the
  judges. See `blind_self_rating.md`.
- Owner rating: `python -m app.persona.lab.rate` (20 pairs in
  `app/persona/lab/pairs.json`, copy in `personality/results/`).

## Rollback

N/A — the flag never flipped. One line + restart remains the
procedure: `PERSONA_ENABLED = False` in
`backend/app/persona/settings.py`, plus `EV_PERSONALITY_DYNAMIC=0`
as the second rollback.

## Known gaps

- Gate failures above (durations, Diwali callback, sarcasm rules,
  on-behalf leakage) — owned by next persona iteration, with the fix
  direction in the TL;DR.
- Kernel path keeps v4 voice (GAP-1); reply hook, voice dynamic
  slice, config formalization, night execution need owner lanes
  (see `personality/INTEGRATION_GAPS.md`; applied: GAP-4 filter
  stage; rest staged on `persona/gaps`).
- Real-data reaction classes unmeasurable (no pos/neg samples).
- Voice identity conflict: v4 `identity_block` says "pronounce E V",
  persona core says "You are Evie" — both present when the flag is
  on (observed once as "Hey, E V here"). Needs the kernel-owner
  identity retirement (cutover proposal 0001).

## Spend (ground truth: `/key` delta)

- Preflight: limit $10, remaining $4.999. End: remaining $0.148.
  Mission spend $4.85 (harness-tracked ≈$4.55; rest probes/retries).
- Round 1 $2.249 / 495 calls; REPORT $0.962 / 215; TUNE R2 $1.274 /
  280 (27 scenarios); pairs $0.02; transcripts $0.02; probes ≈$0.05.
- Runaway guard (3× projection) never tripped; both 402 stops were
  balance mechanics, not runaway (max single-run spend $2.25 vs
  $5.49 abort).
- No further paid work is possible on this key ($0.148 left, Opus
  pre-auth needs ~$0.12/call and rising).

## What is still not real

- The flag is off: no live traffic sees persona.v2.1 anywhere.
- Night execution (agent lane), reply-hook learning loop, voice
  dynamic slice, formal config — staged or handed off, not live.
- Sarcasm suppression, duration grounding, on-behalf strictness —
  diagnosed with fixes specified, not implemented (no tuning
  remained).
- Owner blind rating pending (pairs ready).

