# Live voice test: rebuilt Evie, 2026-10-24

## What was deployed

| Item | Value |
| --- | --- |
| Backend code | `evie-acceptance` fast-forwarded to `5f44f73` (from `33faa69`). That brings in the R7/D4 send floor and the v2 gauntlet. |
| Services restarted | `ev.api`, `ev.talk` (both report `5f44f73` in health after 9 s), `ev.worker`, `ev.scheduler`, `ev.runtime`, `ev.collector` |
| Mac app | `macos/scripts/package.sh --install`, signed `Authority=EV Code Signing`, `codesign --verify --strict` OK, `EVGitSHA=5f44f73`, build `20261024T091317Z`. The build includes the uncommitted panel-activation work in the main tree. |
| Pre-deploy gates | Frozen-contract shield `test_regression_golden.py` 5/5. Send-related suite (51 files) 1099 passed / 1 skipped, the same as the baseline worktree. `EVMicTalkTests` 413 pass / 4 fail, and the same 4 fail on the committed tree (pre-existing). |
| Backup | `~/.ev/backups/deploy-20261024T143055/`: previous `Evie.app` (SHA `c0cacc5`), launchd plists, and the Postgres dump `ev-db.dump` (141 MB, 123 tables, verified with `pg_restore --list`) |
| `ev doctor --fabric` | All PASS (tmux, Ghostty, tailscale, Evie.app signature, bridge, Accessibility, FDA, Screen Recording, Mic, Input Monitoring, Contacts, Calendars, Reminders, launchd jobs, audit chain, panic clear). `ev.ears` is SKIP (not loaded). |

Rollback: `git -C ~/Code/ev reset --keep 33faa69`, then `launchctl kickstart -k gui/$(id -u)/ev.api` (and `ev.talk`, `ev.worker`, `ev.scheduler`, `ev.runtime`, `ev.collector`), then quit Evie and `ditto ~/.ev/backups/deploy-20261024T143055/Evie.app /Applications/Evie.app`.

## Method

The requests were synthesized with macOS `say` (voice Samantha) and played through the Mac speakers with `afplay`. Evie.app's always-on live session (Gemini Live, `gemini-3.8-live-extended-thinking`) heard them through the microphone, as it would a person in the room. Evie's replies were read back from her own transcript API (`/v1/runtime/transcript`). The delegate jobs (`research_sessions`), the agent run traces (`~/.ev/agent/runs`) and `tts-metrics.jsonl` were used for root causes. Each request had a 60 s budget before moving on. Pass 1 had 20 requests. Pass 2 re-ran the overlapping ones with spacing that waits for Evie to finish speaking. The same requests were then typed through `POST /v1/chat` (60 s timeout each) to separate voice-path faults from capability faults.

Live effects stayed inside R3: one file in `~/Desktop/evie-live-proof/`, a harmless `echo` in Ghostty, Calculator opened and quit, the throwaway Reminders list "Evie Live Proof", and a 2-minute timer. The messaging test used a non-existent recipient. Nothing was sent. Test artifacts were cleaned up afterwards: the "favorite tea" memories (forgotten via the owner API) and one Apple Note. **Not cleaned up:** the Reminders list "Evie Live Proof" (holding "drink water") still exists. Evie's EventKit bridge cannot see that list, and deleting it from the terminal raised a macOS prompt, "Ghostty.app wants access to control Reminders.app". That prompt is the owner's decision and was left unanswered (choosing Don't Allow is safe). The owner can delete the list in Reminders.

## Results

Voice, pass 2 (spacing fixed):

| Request | Heard correctly | Outcome | Time to answer |
| --- | --- | --- | --- |
| Greeting | yes | answered in English | 8.2 s |
| Remember a preference | yes | said "Got it". The save node later failed and Evie spoke a raw privacy-boundary error twice | 4.1 s ack |
| Recall it | yes | correct ("masala chai") | 14.2 s |
| Create a file in evie-live-proof | yes | created and verified by read-back | about 70 s (arrived during the next request) |
| Run `echo` in a terminal | yes | ran in Ghostty, correct output | 34 s |
| Today's meetings | yes | **failed** ("Not finished: Read today's calendar") | 25 s |
| 2-minute timer | yes | **failed** ("Not finished: Set 2-minute timer") | 18.5 s |
| Breathing exercise | yes | good guided exercise | 42 s (one-line ack first) |
| Check email | **no** (spoken while Evie was talking) | nothing | - |
| How an engine works | yes | correct, concise | 12.4 s |
| Long story, then "Stop" | yes / yes | story never delivered; "Stop" acknowledged | - |
| Reminder in "Evie Live Proof" | yes | **failed** | - |
| Long poem, then "Stop" | yes / **no** | the poem never arrived in 45 s; "Stop" never transcribed | - |

Voice, pass 1 extras: the time was correct (4.1 s). The weather was correct but arrived about 45 s later, during the next request. The screen description was correct (26.7 s). Calculator opened and was verified via AX (9 s), but the confirmation was spoken 3 times. Deleting Documents got pushback ("Are you completely sure?"). The SSH key was refused. The capability list was good (20.5 s). The unknown-recipient text sent nothing, but it lost the recipient and the body and asked for both again.

Typed `/v1/chat`, same requests: calendar correct (9.1 s), timer set (15.8 s), email summary correct (5.6 s), remember stored and read back (25.5 s), recall correct (4.6 s), terminal echo correct (18.3 s). The reminder went through raw `run_applescript` and only answered "On it" within 31 s.

## The key missing points (prioritized)

1. **The voice brain is not the text brain.** Voice turns run through Gemini Live's realtime delegate graph (`EV_COGNITIVE_MODE=realtime_delegate`). A planner there writes tool arguments that the executor rejects: calendar and reminders get `invalid_request`, the timer gets "That timer op is not on this kernel", mail lands on `life.state` and gets "That life operation is not on this kernel", and the remember step wandered into Gmail capabilities. The retry repeats the same arguments. Every one of these works when typed. Fix: delegate voice goals to the same kernel/agent turn as text (`arun_kernel_turn`), or at minimum validate planner arguments against the capability schema and hand the exact schema error back for one repair.
2. **Owner speech while Evie is talking is lost, and barge-in did not work.** 5 utterances spoken during Evie's playback were never transcribed, including both "Stop" commands. `tts-metrics` shows every playback ran to `response-complete`, and no interrupted playback was recorded. WP-S2 (full duplex: stop playback within 300 ms, cancel the brain) is not met.
3. **Answers arrive late and out of order.** The pattern is "On it" at 4–8 s, then the real answer 20–70 s later, often spoken on top of the owner's next request. Long-form answers (story, poem, the first engine attempt) never arrived. App-open to live took 26 s, because the first auth attempt reported "backend unreachable" and waited for a retry. The 1500 ms budget is far away.
4. **Language drift.** Unclear audio was transcribed as "¿Qué?" twice, and Evie answered in Spanish both times. Input transcription and the reply language should be pinned to the owner's language, with a "say that again?" fallback.
5. **Honesty gaps.** "Run echo …" (pass 1) produced "Received verbatim … exactly what was printed" without running anything. "Got it" for a memory whose save step failed. Raw internal errors were spoken aloud ("Blocked provider payload: message role='tool' contains never_send_to_model content", "'Boys Inside it.' has no verb, target…").
6. **Spoken style.** Markdown, backticks and sha256 hashes were read aloud, and the same reply was spoken 2–3 times (Calculator, file).
7. **One-time owner step.** The rebuilt app raised a Keychain prompt: "Evie wants to access key `com.ev.suit.device-auth`". The owner must click **Always Allow** once. The signing identity is unchanged, so this should not recur on later rebuilds.
8. **Reminders.** Evie's EventKit bridge lists only the "Reminders" list, while the Reminders app also has "Evie" and "Evie Live Proof". Reminders AppleScript times out (-1712) at default and 400 s limits. The test list may still exist if the final delete retry also timed out.
9. **Timer reply time zone.** A typed timer replied "it'll go off at 09:40", which is UTC, not the owner's local 15:10.
10. **Privacy.** Memory `forget` is a soft delete: `is_current=false`, but the text stays in Postgres. R14 requires a hard delete that is verified by reading storage.
11. **Setup.** `ev` is not on the owner's PATH (only `backend/.venv/bin/ev`), yet `ev setup` is meant to be the one entrypoint. `ev.ears` is not loaded, so "hey Evie" while the app is closed does nothing.
12. **Messaging slots.** An unknown recipient makes the router drop both the recipient and the message ("Who should I message, and what should I say?"), even though both were spoken. Nothing was sent, which is safe.
13. **Not built yet (G6 / Phase 2).** Option-key gesture, the HUD pill and confirm-card UX for the new send tickets, screen memory, the video lane, document editing, the catch-up digest, `ev keys` / `ev models verify`, and a persona doctrine. The Documents pushback asked "are you sure" but offered no alternative.

What works today, by voice and live: time, weather (slowly), greetings, in-session recall, verified file creation, a verified terminal command in Ghostty, screen description, opening an app with AX verification, refusing to read secrets, pushback before a destructive request, capability overview, short explanations, and a guided breathing exercise. Over the tailnet, `https://sahajs-macbook-air.tailbd71d0.ts.net` serves the new code (health `5f44f73`, PWA `/evie/` 200). Sends now park a confirm ticket that names the message and recipient, and only a physical approval sends.

## Follow-up: measured fix and remaining evidence (2026-10-24, later)

- **Root cause verified.** On the voice delegate path (`POST /v1/cognitive/delegations` on ev.talk), the planner's own node arguments were invented: `pim.act {"Oh":"read","What":"calendar","When":"today"}`, `{"op":"calendar.list","range":"today"}`, and `timer.act {"action":"set","duration_seconds":120}`.
- **Fix `05ccb17`** (deployed to ev.api and ev.talk). `validate_dag` drops hints whose arguments break the tool schema. The cognitive suite is 618 passed / 1 failed, and the baseline has the same 1 failure.
- **Measured afterwards on the delegate path:** timer 2/2 (8–15 s), mail 2/2, calendar 0/2. The calendar worker answered correctly (no events) but kept calling other tools, and one failed call marks the node `tool_failures` (`worker.py`: `ok = failures == 0`).
- **Same entry point with `EV_DELEGATE_GRAPH=off`** (one process, production env): calendar 2/2 (17.5 s, 8 s), mail in 8 s, reminders in 6 s. **Recommendation for the owner:** set `EV_DELEGATE_GRAPH=off` in `~/Code/ev/.env` and restart ev.api and ev.talk. I tried to change it; the permission system denied the production config change, so it is left to the owner.
- **Voice pass 3** (10 requests, after the fix): timer worked by voice ("Timer set for 3 minutes", 19 s). Arithmetic worked (391, 10 s). The calendar only got an acknowledgement. 5 of 10 requests were not heard or were garbled (Korean/Spanish/Italian fragments, "<no speech>"), and Evie replied in Spanish again. One request spilled into another: a notes question parked a run_shell approval while a web-search request was pending. Continuous mishearing and language drift on the live mic remain the largest voice problem.
