# HANDS computer-use gauntlet (CUG) results

Tasks: **255** (corpus 34, live 63, live_term 12, sim 146). Each task has four phrasings (canonical, two paraphrases, one noisy spoken variant). Oracles read the world back (page state, Ghostty inventory, files); they were written before the runs. Every rate shows n and a 95% Wilson interval.

## Headline

| Stratum | Pass rate | Target |
| --- | --- | --- |
| simulated machine | 146/146 = 100.0% (95% CI 97.4-100.0%) | >= 90% |
| real screenshots (corpus) | 34/34 = 100.0% (95% CI 89.8-100.0%) | layout >= 90%, ids 100% exact-or-flagged |
| live apps (Chrome, Ghostty) | 75/75 = 100.0% (95% CI 95.1-100.0%) | >= 85% |

* False-success claims (receipt says ok+verified while the oracle fails): **0** of 255 tasks.
* Effects outside the task's scope (owner's Ghostty/Safari state snapshotted before/after each live task): **0**.
* Crashed tasks (counted as failures): 0.

## By domain

| Domain | Mode | Pass rate |
| --- | --- | --- |
| browser | live | 9/9 = 100.0% (95% CI 70.1-100.0%) |
| calls | live | 4/4 = 100.0% (95% CI 51.0-100.0%) |
| coding_sessions | sim | 27/27 = 100.0% (95% CI 87.5-100.0%) |
| editors | sim | 5/5 = 100.0% (95% CI 56.5-100.0%) |
| failure_recovery | sim | 9/9 = 100.0% (95% CI 70.1-100.0%) |
| finder | fs | 11/11 = 100.0% (95% CI 74.1-100.0%) |
| ghostty_live | live_term | 8/8 = 100.0% (95% CI 67.6-100.0%) |
| learning | live | 12/12 = 100.0% (95% CI 75.8-100.0%) |
| media_controls | live | 8/8 = 100.0% (95% CI 67.6-100.0%) |
| media_controls | sim | 18/18 = 100.0% (95% CI 82.4-100.0%) |
| media_t2 | live | 17/17 = 100.0% (95% CI 81.6-100.0%) |
| media_t2 | sim | 27/27 = 100.0% (95% CI 87.5-100.0%) |
| music | sim | 6/6 = 100.0% (95% CI 61.0-100.0%) |
| screenshot_reference | live | 2/2 = 100.0% (95% CI 34.2-100.0%) |
| screenshot_reference | sim | 4/4 = 100.0% (95% CI 51.0-100.0%) |
| system | sim | 13/13 = 100.0% (95% CI 77.2-100.0%) |
| terminal_t1 | corpus | 34/34 = 100.0% (95% CI 89.8-100.0%) |
| terminal_t1 | live_term | 4/4 = 100.0% (95% CI 51.0-100.0%) |
| terminal_t1 | sim | 15/15 = 100.0% (95% CI 79.6-100.0%) |
| ui_primitives | live | 9/9 = 100.0% (95% CI 70.1-100.0%) |
| watchers | live | 2/2 = 100.0% (95% CI 34.2-100.0%) |
| watchers | sim | 11/11 = 100.0% (95% CI 74.1-100.0%) |

## T1 - layout from a screenshot (34 real Ghostty captures: 22 dev, 12 held-out)

* Layout (split tree) exact: 34/34 = 100.0% (95% CI 89.8-100.0%); target >= 90%.
* Ratios within 8% of truth: 34/34; worst error 0.52% (target <= 8%).
* Panes whose outcome matched the intended one (resume the right session, or flag it): 118/118 = 100.0% (95% CI 96.9-100.0%).
* Wrong session ids executed: **0** (target 0). Unreadable/ambiguous/unknown ids and duplicate sessions are flagged, never run.
* The held-out 12 were captured after the detector and grounding were tuned on the first 22; the first blind pass scored layout 10/12 and the two misses (a black theme and an uneven nest) were then fixed and re-scored 12/12 (docs/HANDS_BUILD_LOG.md).
* End to end on real Ghostty from a real screenshot (new tab built, sizes set, resume commands run via shims, every pane read back with the session process present): 4/4 = 100.0% (95% CI 51.0-100.0%).

## T2 - skip the ad (real Chrome, own fixture page)

* Skip pressed after the control became available: n=15, median 21 ms, p95 54 ms (target p95 <= 1500 ms).
* Pages that refuse scripted clicks (real Accessibility press fallback): n=3, 1768, 1647, 1627 ms: **misses the 1.5 s target**, because the scripted click is given 0.6 s to take effect first.
* Single-step media control (command to verified result, receipt time): n=8, median 941 ms (target <= 3000 ms).

## Watchers

* Host CPU while an ad-skip watcher polled for 40 s: 0.10% of one core (target <= 1%). The page-side observer does the work between host wakes; osascript child time is not in this figure.

## Learning and self-healing

* Verified novel tasks that compiled and replayed on a fresh instance: 6/6 = 100.0% (95% CI 61.0-100.0%) (target >= 80%).
* Deliberately changed fixture variants completed by the learned skill (repairing the selector when needed): 4/4 = 100.0% (95% CI 51.0-100.0%) (target >= 70%).

## Routing (deterministic `skill_find`, no model)

* Top-1 769/856 = 89.8% (95% CI 87.6-91.7%); top-3 797/856 = 93.1% (95% CI 91.2-94.6%) over every phrasing of every task. Residual misses are genuinely ambiguous pairs (`system.volume` vs `web_media.volume`, `music.play` vs `web_media.play`, `unmute`): the brain disambiguates with `world_query`; `skill_find` is a prior, not a decision.

## First run versus final run (what the gauntlet found)

The gauntlet is a repair loop, so the final rates above are after fixes. The first complete run of each lane, with its cause:

| Lane | First run | What it found (fixed) |
| --- | --- | --- |
| simulated machine | 145/146 | a watcher re-armed itself after reaching its action cap and skipped a third ad |
| live Chrome | 48/63 | the page's own form labels were not used as element names (5 tasks); an armed in-page click could finish the ad between two reads and the answer said "ended on its own" (2 false successes); a watcher's events were only collected on its next 10 s wake, so cancelling lost them (1); a bad oracle for the call-leave check (1) |
| live Ghostty | 10/12 | commands typed with `input text` never ran (a pasted newline stays literal in zsh; the old read-back saw only the echo), fixed by sending Enter as a key and requiring the session process; plus one oracle that forgot sessions left running by an earlier task |
| real screenshots | 8/75 panes, 5/22 layouts (first baseline) | the old pixel detector found no dividers on real captures; replaced by flat-band detection; OCR eats `--`; wrong-pane pairing |
| real screenshots, held-out 12 | layout 10/12 on the first blind pass | black theme and an uneven nest; fixed, then 12/12 |
| real screenshots, final oracle | 33/34 | my oracle compared panes by list order; the detected tree lists grid leaves in another order. Now matched by position |

## What this does not cover

* All fixtures and screenshots are mine. The owner's real YouTube ad, real Ghostty screenshot and real Chrome/Safari settings are owner-run checks (`docs/HANDS_OWNER_CHECKS.md`).
* Safari is covered by scripted-runtime tests only (it was not launched: Safari restores the owner's session). Arc is not installed.
* Zoom, FaceTime, Music, Spotify and VS Code/Cursor are exercised with a scripted machine, not live (none was started on the owner's Mac).
* No noise/JPEG tolerance in divider detection: macOS screenshots are lossless PNG. A phone photo of a screen will not parse.
* The 100% live rate comes from tasks I wrote, run after fixing what the first run exposed; treat the first-run table as the realistic guide to what a new site or a new layout will do.

## Failures in the final run

* none

## Real-brain mode

Not run. The harness is built and tested with a scripted model (`hands_cug/brain.py`, `tests/hands/test_brain_harness.py`). Evie's OpenRouter key had $0.21 of its $5 limit left when this ran; the harness caps itself at the key's remaining credit minus a $0.25 reserve for the live assistant, which is below zero, so it refuses to spend. **OWNER-ACTION: raise the OpenRouter key limit** (then `python -c "from hands_cug import brain; ..."` runs a stratified sample: 62 eligible single-skill tasks, ~$0.001 per call at the contributor tier). Until then no number here comes from a model choosing skills; the routing figure above is the deterministic `skill_find` prior only.
