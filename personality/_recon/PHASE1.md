# Phase 1 report — persona.v2 (CLI 6 low-telescopium)

Date: 2026-10-27. Branch: `main`. `PERSONA_ENABLED` is False (not flipped).
No file outside CLI 6 paths was modified (all additions are new untracked
paths; verify with `git status --porcelain`).

## Files created

`backend/app/persona/` (17 files): `__init__.py` (exports
`build_persona_block`, `persona_guard`, `PERSONA_VERSION="persona.v2"`),
`spec/CHARACTER.md` (verbatim spec copy, sha256-verified identical),
`settings.py`, `tokens.py`, `context.py`, `audience.py`, `awareness.py`,
`techniques.py`, `reactions.py`, `learning.py`, `tone.py`, `callbacks.py`,
`opinions.py`, `exemplars.py`, `exemplars.yaml`, `core.py`, `guard.py`,
`nightshift.py`, `compile.py`.

`backend/tests/test_persona_*.py` (10 files): tone, reactions, awareness,
learning, stores, nightshift, guard, budget, boundary, names.

`backend/eval/personality/`: `__init__.py`, `scenarios.yaml` (50 scenarios,
10 held-out), `rubric.md`, `run.py`, `mine_exemplars.py`, `results/`
(`dry_run.json`, `full_mock.json`), `proposals/` (one miner proof output).

`personality/`: `briefs/` (spec + build brief, verbatim saves),
`proposals/0001-persona-v2-cutover.md`, `handoffs/01–09`, `_recon/`
(this report + the recon REPORT.md).

## Test results

167 passed, 0 failed (`pytest tests/test_persona_*.py`).
`ruff check` clean on package + tests + eval. `mypy app/persona` clean
(17 files). No paid eval runs; mock gate 50/50 with 0 hard fails and
0 ceiling misses.

## Measured latency and token sizes

Repo estimator (`len//4`). Hot path worst-of-200 (warmed): build 0.09 ms,
build+store 0.06 ms, guard 0.04 ms, tone 0.05 ms — ~50x inside the 5 ms
budget. Deterministic: identical context → identical block, always.

| Block | Tokens | Budget | Verdict |
|---|---|---|---|
| Core verbatim (§2) | 573 | (spec claims ≤420) | **spec conflict, see below** |
| Core voice variant | 563 | — | same conflict |
| Text, signal-rich | 617 | 650 | fits, exemplars shed |
| Text, sparse | 642 | 650 | fits, 1 exemplar kept |
| Voice / phone floor | 580 | 380 | **cannot fit, see below** |
| NOW line max | ≤40 | ~40 | enforced by truncation |
| OPERATIONAL LINES | 107 | never dropped | present on every path |

## Learning simulation

30 turns, scripted owner rewards understatement / punishes escalation:
understatement Beta(5,1) mean 0.50→0.833, escalation Beta(1,3) mean
0.50→0.250. Joke rate under 10 straight negatives: 0.50→0.150 (floor).
Bounds (0.15, 0.7) enforced.

## Reaction-classifier precision

60-message fixture: positive precision 1.000 (20/20), negative 1.000
(20/20). Caveat: the fixture and the patterns were written together, so
this measures self-consistency, not real-world precision — a labelled
sample of real owner messages is still needed (proposed follow-up).

## Night Shift validator coverage

`validate()` enforces 8 checks (non-empty goal/task_ref, workspace shape,
no forbidden/unknown capabilities in `allowed`, forbidden-list superset,
scope, no new features, exact stop conditions, checkpoint ≥5 min); tests
cover each rejection plus nudge caps/gap/decline/new-signals, unclear
task → None, and receipt-mandatory brief items (constructor raises).

## Which path serves /v1/chat by default

**The legacy pipeline** (`run_chat_pipeline` → `build_system_prompt` with
the v4 contract). Evidence chain: `core.py:1926 kernel_on =
kernel_mode_active()`; `cognitive/mode.py:58-61` true only when mode is
`mimo_kernel`; code default `cognitive_mode = "legacy_gemini"`
(`config.py:508`); deployed `.env` sets `EV_COGNITIVE_MODE=realtime_delegate`
(≠ mimo_kernel → kernel off). Only the `.env.api-first` profile
(`mimo_kernel`) routes /v1/chat through the kernel. So persona.v2's first
live surface is the legacy path (via cutover part A); the kernel bypass
still needs handoff 02 before the api-first profile speaks v2.

## Handoffs sent (personality/handoffs/)

01 owner §13 override · 02 kernel wiring · 03 filter guard stage ·
04 evals gate slot · 05 shared-config fields · 06 voice instructions ·
07 agent night-shift entry · 08 scheduling/brief delivery ·
09 thread-state persistence. Cutover diffs in
`personality/proposals/0001-persona-v2-cutover.md` (written, not applied).

## Conservative readings taken (brief was silent)

- Voice/phone `[text]` handling: strip the marked clause, keep the rest of
  the line (preserves the no-filler operational rule on voice).
- TURN TONE suffixes `; TARGET=object` / `; VOICE=owner` only when active;
  base format matches the brief exactly.
- "Roast me" sets roast 3 pre-caps (spec's "allowed" read as "unlocks").
- Phone treated as a spoken channel (roast cap 2, zero emoji).
- Humour slider 0–5 → caps 0/1/2/3 (3+ uncapped); profanity requires
  humour-on + private audience; on-behalf profanity redacted as
  `[redacted]`.
- Rule-4 (frustration) sets target only; ceiling flows from rules 5–6.
- Destructive-action detection rides on `confirmation_pending` (this repo
  parks confirmations for all destructive effects).
- `hint_for` lives in `learning.py` (needs weights + registry; avoids a
  circular import). New helpers: `topic_of`, `nudge_recorded`,
  `LATCH_TURNS`, `seeded_rng`, `describe`.
- `validate()` returns an error tuple (empty = valid); `plan_from_context`
  falls back to system clock for the workspace date only.
- Guard recomputes tone from ctx (roast is a pure function of ctx; the
  technique hint never changes it).
- Fixture + patterns co-written (precision caveat above).

## Proposed spec changes (spec text untouched)

1. **Core budget**: §2 claims ≤420 tokens but the verbatim core is 573
   (repo estimator; ~530+ on any real tokenizer). Propose: trim the core
   to ~300 tokens or restate the budget honestly.
2. **Voice budget**: floor (core + tone) is ~580 vs the 380 cap — unachievable
   without dropping core lines (forbidden). Propose: raise voice to 600 or
   authorize a reduced voice core (OPERATIONAL LINES + TRUTH FIRST + TEASING
   only). Current code sheds every droppable and reports honestly.
3. **`[text]` marker**: give the emoji rule its own line so "drop the line"
   and "keep the filler rule" stop conflicting.
4. **Tired owners**: spec §4 covers stressed/sad/grieving but not tired;
   propose tired → rule-2-style support (currently falls to base roast 1).
5. **Legacy taste seeding**: spec §7 names STABLE_TASTE/STANCES as the
   ledger seed; wiring needs an `app.ev` import decision (purity).

## Blockers

- §13 override (handoff 01) gates cutover parts A–B; kernel (02), filter
  (03), voice (06), and thread-state (09) owners gate live behavior.
- Voice budget conflict (proposal 2) needs an owner call before voice cutover.
- Real-brain eval + real-message precision sample need owner sign-off (paid).
- `PERSONA_ENABLED` stays False. Phase 2 not started.
