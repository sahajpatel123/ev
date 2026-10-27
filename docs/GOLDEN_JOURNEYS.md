# Golden journeys (release captain, rocky-sculptor)

Each journey: status, evidence, trace ids. Live turns ran against isolated
:18005 (frozen rc1 @ 21df36c, echo brain = honest-unavailable) and in-process
skill lanes on scratch state. Real-brain intelligence is covered separately
(gauntlet DEV 535, mutants 25/25).

## J-loop (transport proof, underlies J1/J2/J5/J6/J8/J10/J13/J15)

- Turn ft-cf0d54e16f9b via POST /v1/cognitive/turn → presence frames seq 11-14:
  turn.started → turn.thinking → turn.progress → turn.failed
  (category=provider_error, recoverable, next_step="Ask me again."). PASS.

## J1 hold Option, ask schedule, spoken answer, orb states

- PARTIAL (owner-side): loop→presence→spoken-line path proven by J-loop;
  gesture 0/12005 (D1); orb states benched (E2). Live voice round trip + orb
  observation need the owner (S7).

## J2 latch, multi-step file task, plan, verified, rewind, undo

- PARTIAL: file lane live — create→trash→undo-restore verified byte-identical
  (/tmp/rc-journeys-fs.jsonl); outside-root→CONFIRM; secrets DENY; fabric
  /undo REST lists entries. Latch + rewind-Timeline-UI need the app (S7).
  No brain-planned multi-step turn (budget).

## J3 T1 from corpus screenshot with fixture session stores

- PARTIAL: s02_two_cols_dark → 2-pane plan, live build audit ok (max_err
  0.0007); fixture sessions correctly ungrounded → plain shells + honest
  unverified; undo closes window. evvision built on rc1 (was missing).
  Executable resume vs real stores blocked by frozen window surface (env);
  owner re-run in S7.

## J4 T2 on fixture video page, command from phone channel

- PARTIAL: T2 live 17/17 on fixture pages; phone→pipeline→live-thread route
  19/19 (test_cognitive_phone_text + convergence). Composed phone-spoken
  skip needs real brain + real phone (S7).

## J5 message to fake contact; card required; spoken yes rejected; physical approve sends

- PARTIAL: dispatch parks (test_dispatch_parks…); spoken yes refused, ticket
  stays pending (C2); spoken no cancels (C2); typed yes sends via fake
  background (test_yes_sends_via_linked_background…); unconfirmed sends park
  (comms live). 4/4 green. Live phone/card approve needs device (S7).

## J6 fact stated, recalled, forgotten, deletion verified

- PASS (tombstone semantics): event 53def7dc written → rebuild → recall hits
  memory a84f187a ("…QUILLWORT…") → forget → payload forgotten:true →
  recall excludes it. "200 turns later" approximated by rebuild+recall, not
  a 200-turn session. Hard delete NOT-BUILT (R14, pre-existing).

## J7 barge-in mid-answer + "stop"

- PARTIAL (owner-side): logic tests only by me; stop/cancel paths tested
  (runner cancel); timings unmeasured. Owner run in S7.

## J8 `ev panic` during a running task

- PASS: POST /v1/fabric/panic → status panicked:true → effects denied →
  /panic/clear → false. Engage 26ms (≤2s). Presence panic.state frames live.

## J9 code a small project by voice to passing tests

- OWNER-ACTION (S7): needs voice + real brain + budget.

## J10 scratch SQL DML preview and confirm

- PASS: read AUTO rows [[x]]; DELETE preview affected_rows=1 rolled_back=true
  (row survives); confirm applies (row gone); prod=True DENY. Live receipts
  in /tmp/rc-journeys-fs.jsonl.

## J11 injected email asking to forward mail is blocked

- PASS (defense in depth): R8 live (untrusted provenance escalates even reads
  to CONFIRM); unconfirmed sends park; keyword catcher hits its 5 patterns;
  S28 13/13 + filter/comms 39/39 + policy 56/56. Note: catcher is a shallow
  blocklist; the CONFIRM gate is the real block.

## J12 "skip ads while I'm away for 10 minutes" watcher

- PASS (mechanism): live.watch.skip_ads_watcher — skipped 2 ads, no further
  input, cancelled → page script stopped (8.5s). 10-minute expiry window
  not soaked; expiry logic by code + unit tests.

## J13 keys removed/offline: honest degradation, no double speaks

- PASS: echo-configured turn → kind=unavailable, spoken refusal, persist=false,
  usage={}; presence turn.failed (provider_error, recoverable). No echo text
  presented as brain output. nit: response names configured brain_model while
  unavailable (disambiguated by kind=unavailable).

## J14 long background task reports back on same thread to phone

- PARTIAL: _record_thread_completion writes live thread + background.update
  (code-read + wired at runner.py:400); no-silent-drop tests 16/16. Live
  long-run + phone leg not executed.

## J15 "what did you just do? undo that"

- PARTIAL: undo mechanism live (files byte-identical restore; ghostty.close
  via skill; fabric /undo REST). Conversational "what did you do" needs
  real brain (budget).

## J16 novel composition task

- PASS: SQL read (2 rows) → write results file ("rows: a,b" verified) →
  delete → undo restore, all with receipts (/tmp/rc-journeys-fs.jsonl).
