# MAC_VOICE_BASELINE_20260822_p0fix

The single authority for "which Mac voice implementation works." Update this
file whenever the Mac voice-critical set changes and passes the acceptance
gates again.

## Identity

| Field | Value |
| --- | --- |
| Baseline name | `MAC_VOICE_BASELINE_20260822_p0fix` |
| Commit | `9a7141a` + uncommitted working-tree P0 fix (TTSPlayer/LiveConversation/SmokeTest barge-in control-queue hop) |
| Mac binary sha256 (first 16) | `d407b079cc798757` |
| Packaged | `macos/build/EV.app`, Aug 22 2026 00:13, signed identity `EV Code Signing` |
| Crash-fix marker in binary | `ev.live.barge-in-control` (must be present) |
| Orb build | `ORB_BUILD_20260818_0135_visible` metal-video-quad-v4-presence |
| Bridge fingerprint (running API) | `f141ae96…` |
| Barge runtime id | `ev-barge-runtime-v2` (`local-detector-playback-authority`) |

## What this baseline contains vs the frozen golden voice (`5f4f0cd` / live-working-2026-08-19)

- REQUIRED FIX: barge-in interrupt no longer mutates the AVAudioEngine graph
  on the microphone tap thread (`LiveConversation.controlQueue.async`,
  `SmokeTest` probe hop). Root cause of EV-2026-08-21-*.ips EXC_BREAKPOINT
  chain: `AVAudioPlayerNode.stop()` dispatch-synced the engine's
  RealtimeMessenger queue from inside a tap callback.
- BARGE-IN WIP (experimental): local detector, preroll, VoiceTurnMachine,
  playback reference PCM, backend played-ms bookkeeping.
- Known debt (not blocking): the echo-only false positive still confirms
  during loud speaker playback (`echo_only_false_positive=YES` in probe). A
  false interruption now only stops speech and returns to listening; it can
  never terminate the process.

## Acceptance gates used to mint this baseline

- `swift run --package-path macos EVMicTalkTests` — all passed (incl.
  `wired-LiveConversation-no-render-thread-stop`,
  `wired-BargeInProbe-no-render-thread-stop`).
- `.build/debug/EV --tts-test` — PASS (local PCM playback).
- `.build/debug/EV --first-audio-test` — PASS (first buffer decoded,
  enqueued, rendered via dataPlayedBack; short answer completes; long
  bounded response stops clean; interrupt confirmed on simulated render
  thread returns immediately; control work hops off-thread).
- `.build/debug/EV --barge-in-probe` — PASS with real mic+speakers;
  detector confirmation during playback survived (this exact event killed
  EV.app before the fix).
- Backend safe contracts: test_mobile_voice_core, test_webrtc_connection,
  test_barge_in, test_device_gateway — PASS.

## Status

MAC NORMAL VOICE: INTERNALLY VERIFIED / OWNER PENDING
MAC BARGE-IN: EXPERIMENTAL (stop-path crash-proofed; echo tuning pending)

---

# LISTENER PRESENCE ENGINE ADDENDUM (2026-08-22, Phase 1–3 complete)

`EvieListenerPresenceEngine` is integrated behind flags, DEFAULT OFF. With
flags off, runtime behavior matches the owner-verified baseline exactly
(the controller's acoustic feed returns immediately; no server gate control
is sent; no visual or vocal path can fire).

| Field | Value |
| --- | --- |
| Canary binary sha256 (first 16) | `2f34150d426c9fd1` |
| Flags | `EV_LISTENER_PRESENCE_ENABLED`, `EV_LISTENER_VOCAL_ENABLED`, `EV_LISTENER_VISUAL_ENABLED` (UserDefaults, all false) |
| Variant cache | `~/Library/Application Support/EV/listener/manifest.json` — 10 Evie-voice variants (EdgeTTS = Talk-reply voice), soft gain 0.30, 16 kHz mono PCM16 |
| Server gate | client sends `listener_presence` control when enabled → bridge sets `engine.backchannel_enabled = False` (cadence lane stands down) |
| Probe | `.build/debug/EV --listener-presence-test` — PASS (capture gate open during nods, response lane not speaking, 100 overlaps + 5 stop-race waves survived, self-reference registered) |

Architecture invariants held: timing is acoustic-opportunity-driven only
(no interval timers); decisions and all playback scheduling run on the main
actor; the realtime mic tap only performs allocation-free RMS arithmetic;
backchannels never touch the assistant-response lane, barge-in lifecycle,
conversation history, tool state, or memory.

---

# LISTENER PRESENCE ROUND TWO — OWNER CANARY RESULT + FIX (2026-08-22)

## Owner physical evidence (Round One, ~60–90 s monologue)

Normal owner speech capture: WORKING. Opportunity detection: working enough
that nods fired mid-monologue. **Backchannel overlap behavior: BROKEN** —
nods were chopped into tiny unclear fragments when the owner kept speaking.
Backchannel clarity: PARTIALLY WORKING.

## FIRST DIAGNOSTIC — proven from `~/Library/Logs/EV/barge-trace.jsonl`

Round One window (03:33–03:44): `detector.confirmed` storms with
`phase=assistantSpeaking` followed by `local_stop` events at
`played_ms = 8–330`. WHO: LiveConversation's mic-tap interrupt path →
`player.stopForBargeIn()`. WHY: nods rode the ROLE C response lane, so
continued owner speech was indistinguishable from a barge-in against real
assistant speech; the stop hit the same node the nod played through.
STATE: `VoiceTurnPhase.assistantSpeaking`; QUEUE: mic tap →
`ev.live.barge-in-control` hop. Not an assumption — measured.

## SECOND DIAGNOSTIC — asset clarity

1. DOUBLE GAIN BUG: clips pre-attenuated ×0.30 at generation AND ×0.30–0.36
   at playback (net ≈ ×0.10, about −20 dB) → "unclear". Fixed: assets are
   RMS-normalized at generation; the manifest per-family playback gain is
   now the ONLY attenuation.
2. DURATION FAMILY MISMATCH: the "warm long" clips rendered 462–695 ms and
   were mislabeled by measured-duration bucketing; no true elongated nod
   existed. Fixed: intended families validated against render windows,
   manifest carries MEASURED truth (relabel honestly when TTS drifts).
3. Fades clean (sample[0]=0, edge discontinuity within body envelope);
   15 ms safety fades only — no "mh—" tail swallow.

## Round Two state of the shipped set (10 Evie-voice variants)

- micro 480 ms ×1 · normal 456–756 ms ×5 · elongated 727–972 ms ×4
  (`mhmmm…`, `hmmm…`, warm `mm-hmm…`). Effective RMS ≈ 0.07–0.10 — clearly
  audible, materially below response level. Elongated ≠ louder: lowest gain.
- PlaybackRole declared per enqueue (`listenerBackchannel` +
  `finishDespiteOwnerSpeech`); physically separate aux player node;
  `stopForBargeIn()` is ROLE-C-only (`auxTeardown:false`);
  NORMAL_RESPONSE preempts via safe main-thread path only.
- Full LP00–LP09 trace set added (opportunity, variant, enqueue, first
  sample, owner-speech-continues, detector state at selection, teardown
  WITH who/why attribution, last sample, completion, capture-continues).

## Status after Round Two engineering (owner verdict PENDING)

| System | Status |
| --- | --- |
| LISTENER PRESENCE ENGINE | INTEGRATED |
| OPPORTUNITY DETECTION | PARTIALLY WORKING PHYSICALLY (round one) |
| BACKCHANNEL OVERLAP | FIXED INTERNALLY / AWAITING OWNER CANARY ROUND TWO |
| BACKCHANNEL AUDIO CLARITY | RETUNED (double-gain removed) / OWNER PENDING |
| ASR CONTAMINATION | OWNER PENDING (detector-level overlap tests pass; end-to-end transcript check not locally deterministic) |
| NORMAL MAC VOICE | OWNER VERIFIED PRESERVED |
| NORMAL MAC BARGE-IN | OWNER VERIFIED PRESERVED |

Internal gates re-run this round: EVMicTalkTests all passed (incl. new
`listener-overlap-owner-still-confirmed`, `listener-self-nod-not-confirmed`,
elongated-contextual policy tests, OFF-baseline wiring checks); backend
`test_listener_presence_gate`, `test_barge_in`, `test_mobile_voice_core`,
`test_device_listener` all passed; overlap stress probe
(`--listener-presence-test`) previously PASS on this lane design.
Phone rollout remains PAUSED until Mac owner approval.

---

# P0 2026-08-22 — MAC REALTIME DISCONNECT DURING LISTENER ROUND TWO CANARY

## Root cause (proven, not inferred)

**UPSTREAM PROVIDER QUOTA**: OpenAI Realtime accepted the dial then closed
with `1013 insufficient_quota.organization_spend_limit_exceeded` — "Your
organization has reached its configured enforced spend limit." Home Station
retried every ~3.7 s, emitting `realtime_disconnect` to EV.app each cycle;
the owner saw exactly that message before testing could begin.

- WHO CLOSED FIRST: UPSTREAM PROVIDER (close code 1013, reason captured).
- Listener Presence: NOT INVOLVED (canary ran flags-OFF; protocol A/B with
  the `listener_presence` control survived identically).
- Aux audio / session configuration / listener control plane: NOT INVOLVED.
- Secondary latent defect found & fixed: a stale backend process (started
  13:05 from a mid-edit tree) crashed any client control with
  `_handle_control() takes 2 positional arguments but 3 were given`,
  killing client sockets. Current reviewed tree is consistent; transport now
  contains handler exceptions (`client_frame.rejected`, never ASGI death).
- Pre-existing unrelated failure: `test_agent4_acceptance.py::…parks_resumes…`
  fails WITHOUT these changes too (mobile-actions domain; untouched here).

## Fixes shipped

1. `grok_voice.py`: close code/reason captured (`upstream.closed`), quota
   classified → truthful `realtime_quota` voice/UI message ("spend limit is
   reached. Raise the limit and just talk to me again."), reconnect slowed
   to 60 s (storm eliminated) instead of futile 3.7 s hammering.
2. `transport.py`: `_handle_client_frame` containment — any handler
   exception degrades to non-fatal `control_rejected`; the client WebSocket
   can no longer be killed by optional-feature handler bugs.
3. `provider.error` trace now logs semantic message text (was type only).

## Verification (live, against restarted Home Station)

Harness A/B/C (idle / listener_presence control / common controls): ALL
SURVIVED; single truthful quota event per session; ZERO disconnect storms;
controls accepted; state events flow. Backend suites: listener gate,
barge-in, mobile voice core, device listener, webrtc connection,
p0-containment — 47 passed.

## Owner action required to restore voice

Raise/remove the OpenAI org spend limit
(platform.openai.com/settings/organization/limits), then talk normally.
Until then realtime voice stays paused BY PROVIDER POLICY — not by Evie.

STATUS VOCABULARY UNTIL OWNER RETEST:
MAC NORMAL VOICE: INTERNALLY VERIFIED / OWNER RE-VERIFY PENDING
LISTENER PRESENCE: INTEGRATED / OWNER PENDING (naturalness NOT re-tested)
REALTIME DISCONNECT: ROOT-CAUSED (provider spend limit) + HARDENED TRANSPORT
ASR CONTAMINATION: OWNER PENDING · PHONE ROLLOUT: PAUSED

---

# P0 2026-08-22 EVENING — SELF-BACKCHANNEL + RESPONSE CHOPPING (ROUND THREE)

## Owner physical evidence (22:46–22:55 window)

Evie produced "nice"/"okay"-style gestures around her OWN answers; her
responses broke every ~2 s. Listener Presence flags were OFF at all three
launches (`listener.runtime enabled:false ×3`) and the local engine emitted
ZERO vocal selections — the local engine was inert.

## Root causes (proven from traces, both sides)

1. SOURCE OF "nice"/"okay": **SERVER backchannel lane** (`BackchannelPolicy`
   cue vocabulary `_EARLY=("Mhm.","Yeah.","Okay.")` …). With local flags off,
   the Mac never sent the stand-down control → a SECOND authority was live.
   Its cues rendered through the client's response lane via
   `case "backchannel"`.
2. NORMAL RESPONSE BREAKS: **false local barge-ins under hollow playback
   reference.** During Evie's speech the reference read play_rms≈0.001–0.004
   while her voice leaked to mic at 0.014–0.028 with corr 0.05–0.23 →
   detector confirmed 9 fake near-end events in 15 s (fast path, persist 3)
   → `local_stop` chopped audio (played_ms 426–2385) + barge_in controls →
   server cancelled / auto-created replacement responses (chunk_index=0,
   118–177 chunk streams) → repeat ≈ the ~2 s break cycle.

## Fixes shipped

1. CLIENT (BargeIn.swift): REFERENCE-UNRELIABLE GUARD — when speakers are
   audibly live but the reference reads silent, fast/soft confirm paths are
   disabled; near-end must show mic_rms ≥ 0.05 (echo measured ≤0.028, real
   owner ≥0.13) and low correlation. Real "Wait." still confirms (tested).
2. CLIENT (LiveConversation.swift): ONE BACKCHANNEL AUTHORITY LAW — Mac sends
   the `listener_presence` stand-down UNCONDITIONALLY. Local disabled =
   SILENCE, never a second server authority.
3. CLIENT (ListenerPresenceController + EVClient): FLOOR EPOCH + HARD
   DORMANCY — opportunity generation fully stops while assistant response is
   pending/rendering and during a bounded post-playback echo tail (0.35 s);
   fresh owner onset required afterwards; stale candidates are dropped on
   epoch change; semantic lexical gestures gated behind
   `EV_LISTENER_SEMANTIC_ENABLED` (default OFF — nonlexical-only canary);
   invariant metric `renderedDuringAssistant` (must remain zero).

## New packaged binary

sha16 `13abd2aece08b048`, signed, macos/build/EV.app. EVMicTalkTests 276/276
PASS incl.: hollow-reference-echo-not-confirmed, hollow-reference-owner-
still-confirmed, dormancy predicate, semantic-disabled-neutral-only,
server-lane-always-down wiring, stale-epoch drop path.

STATUS VOCABULARY UNTIL OWNER RETEST:
MAC NORMAL VOICE: RECOVERY SHIPPED / OWNER VERIFY PENDING (Stage A first)
LISTENER PRESENCE: DISABLED BY DEFAULT; SERVER LANE STOOD DOWN ALWAYS
SELF-BACKCHANNEL PREVENTION: FIXED INTERNALLY / OWNER PENDING
NORMAL BARGE-IN BASELINE: PRESERVED (guard tested both directions)
SEMANTIC LEXICAL GESTURES: OFF until foundation re-verified
PHONE ROLLOUT: NOT STARTED

---

# ROUND FOUR — TURN AUTHORITY / SELF-TURN ELIMINATION (2026-08-22 23:xx)

## Phase-0 proof (owner test 23:36–23:39, new binary, flags OFF)

One long answer produced SIX assistant responses (floor epochs 1→6) in ~100 s
of owner silence. Two chop signatures captured:

1. 23:37:31 — hot-ish ring (`play_rms 0.036`), mic blip 0.018, corr 0.12 →
   confirm → chop at played_ms 11279.
2. 23:38:09–28 — provider streaming gap DRAINED the queue: `audible=false`
   for 8 s mid-answer, ring cold, then mic 0.0138 confirmed via headphones
   path → chop at played_ms **42429**. The round-three guard only covered
   `audible=true`, so the drain state re-opened the door.

Root cause class: instantaneous buffer/reference state carried turn
authority. LISTENER PRESENCE silence in the same window = flags were OFF
(both launches) — no over-suppression bug demonstrated; engine never ran.

## Fixes shipped

CLIENT (baseline behavior, flag-free):
- `PlaybackSnapshot.assistantEpisodeActive` — episode stays active through
  provider pacing gaps (2.5 s tolerance after last response chunk).
- Detector: during an assistant episode ALL confirm paths require ADAPTIVE
  near-end level `max(0.05, playRMS×0.35)` — self audio measured ≤0.028,
  real owner ≥0.05–0.13; loud-playback echo scales with reference. Correlation
  is NOT a discriminator (owner-over-Evie legitimately correlates).
- QUARANTINE: recovering→listening now requires sustained mic quiet (~0.4 s);
  voiced self-tail resets decay; detector armed THROUGH recovery; forwarding
  stays blocked until release; preroll preserved on real interrupts.

BACKEND canary `EV_TURN_AUTHORITY_V2_ENABLED` (default OFF):
- session.update: `create_response:false`, `interrupt_response:false`,
  server_vad retained as SENSOR; effective ack logged
  (`turn_detection.effective`).
- Bridge owns response.create: speech_stopped → bounded grace (0.6 s);
  continuation inside grace cancels commit; expiry finalizes the logical
  owner turn and sends exactly ONE response.create (idempotent per turn id).
  Acoustic chunks keep aggregating into one UserAudioTurn (chunk ≠ turn law).

## Tests

Mac EVMicTalkTests all-pass incl. new: turn-episode-drain-blip-not-confirmed,
turn-episode-real-owner-still-confirmed, turn-episode-warm-ring-blip-not-
confirmed, turn-episode-loud-echo-not-confirmed, turn-quarantine-holds-under-
voiced-blips, turn-quarantine-releases-on-decay.
Backend 133 passed incl. new tests/test_turn_authority_v2.py (continuation
cancel + single create + idempotency + V2-off inertness + session shape).
Packaged EV.app sha16 `3eee362a2c32890b`.

STATUS UNTIL OWNER VERIFIES:
TURN AUTHORITY V2: INTERNALLY VERIFIED / OWNER PENDING (flag OFF = baseline)
FALSE SELF-TURN: FIXED INTERNALLY / OWNER PENDING
LISTENER PRESENCE OWNER-FLOOR: READY — owner must ENABLE flags for Test 2
NORMAL MAC VOICE / NORMAL BARGE-IN: OWNER RE-VERIFY PENDING
PHONE ROLLOUT: PAUSED

---

# ROUND FIVE — REAL BARGE-IN RESTORATION (2026-08-23, P0)

## Owner physical result

TEST 1 long assistant speech: PASS (>30 s continuous; self-chop gone).
TEST 2 real barge-in: **FAIL** — repeated normal-volume "Wait" during a
response produced NO interruption.

## WHY "WAIT" WAS REJECTED — measured from the failing session trace

Owner attempts at mic RMS **0.016–0.063** (typical sustained 0.02–0.04;
single-frame peak 0.061). The round-four gate required
`max(0.05, playRMS×0.35)` during assistant episodes:
- every sustained frame → `BELOW_ABSOLUTE_LEVEL` (bar ≈0.05);
- the one 0.061 peak lasted a single window → `INSUFFICIENT_PERSISTENCE`;
- the only confirms occurred AFTER the queue drained and episode protection
  switched off (`mic=0.0397 @ play_rms=0.0005`; `mic=0.0159` in recovering) —
  the owner accidentally waited for playback gaps.
Session variance discovery: self-audio measured 0.0032–0.0097 THIS session vs
0.014–0.028 earlier — fixed RMS floors are structurally wrong.

## BARGE-IN V2 SHIPPED (defaults ON; rollback flag EV_BARGE_IN_V2_ENABLED)

Evidence fusion replaces amplitude gating during assistant episodes:
1. STAGE 1 candidate: mic ≥ max(0.012, 2.2 × selfEchoEma), speech-like.
   selfEchoEma = session-adaptive leak calibration updated ONLY from
   non-candidate episode frames (owner speech cannot contaminate it).
2. STAGE 2a echo veto: delay-aware WAVEFORM matched filter (lags 0–200 ms,
   closed-form gain fit, plausible-gain window) — if Evie's own delayed audio
   explains the frame (residual ≤ max(0.0045, 0.20×mic)), it is SELF however
   loud. Correlation deliberately not used as discriminator.
3. STAGE 2b short-word persistence: ≥2 consecutive voiced frames (~40 ms) —
   "Wait"/"No" confirm fast; isolated room transients never accumulate.
4. Drained-reference fallback: when the ring is too sparse for matched
   filtering, calibrated confirm floor max(0.016, 3×selfEchoEma) applies.
5. Rejection reasons recorded per frame (BI10): BELOW_ABSOLUTE_LEVEL,
   BELOW_CALIBRATED_ECHO_FLOOR, HIGH_ECHO_MATCH, INSUFFICIENT_PERSISTENCE,
   NOT_SPEECHLIKE, INSUFFICIENT_RESIDUAL.

## Verification matrix (synthetic, deterministic; live A/B pending quota+owner)

- drain-state room blip (measured ≤0.0138): NOT confirmed ✓
- soft "Wait" in drained state (measured 0.02–0.04 class): CONFIRMED ✓
- calibrated clause-pause room noise (1.6× ambient): NOT confirmed ✓
- loud-playback direct echo (22% leak of 0.35 ref): NOT confirmed ✓
- SOFT owner UNDER hot playback (echo 0.18 + owner 0.05 amp mix):
  CONFIRMED within ≤6 frames (≤120 ms) ✓
- all prior suites green: EVMicTalkTests 285 checks PASS; backend
  turn-authority/barge/mobile suites 32 PASS (backend untouched this round).
Local stop latency: control-queue hop measured 0.7–3 ms in prior live traces;
perceived latency dominated by confirmation ≈ ≤120 ms synthetic onset→confirm.

Packaged EV.app sha16 `198545a4af614996`.

STATUS UNTIL OWNER TEST:
REAL BARGE-IN: FIXED INTERNALLY / OWNER PENDING
SELF-ECHO PROTECTION: PRESERVED (freeze constraints re-tested green)
BARGE-IN V2 ROLLBACK: defaults write EV_BARGE_IN_V2_ENABLED -bool NO
LISTENER PRESENCE: PAUSED · TURN AUTHORITY V2: PAUSED (flag OFF)
PHONE ROLLOUT: PAUSED

---

# ROUND FIVE RECOVERY — CONTINUOUS VOICE BASELINE (2026-08-23)

## Owner physical result that triggered recovery

Barge-In V2 canary (sha `198545a4`, V2 ON) restored soft "Wait" detection
but introduced random self-stopping during owner-silent long responses.

## Immediate safe action taken

Barge-In V2 **DISABLED by default** (`v2EpisodeGate` default `false`;
`defaults write com.ev.suit EV_BARGE_IN_V2_ENABLED -bool YES` to re-enable).
The proven round-four fixed-floor episode gate
`max(0.05, playRMS×0.35)` is now the active baseline again.

- Long assistant response continuity: OWNER VERIFIED (>30 s smooth) in the
  canary immediately before V2 — this baseline restores that exact behavior.
- Real "Wait" interruption: known to be broken under this baseline
  (measured 0.016–0.063 rejected as BELOW_ABSOLUTE_LEVEL); accepted
  tradeoff per directive: CONTINUITY > INTERRUPTION for now.

Packaged EV.app sha16 `1fb60eabcaba2080`, signed.

Verification: EVMicTalkTests 285/285 PASS (3 V2-specific soft-Wait tests now
explicitly enable V2 via `cfg.v2EpisodeGate = true` and keep passing; baseline
behaviour verified). No backend changes.

STATUS UNTIL OWNER TEST:
NORMAL MAC VOICE: INTERNALLY VERIFIED / OWNER PENDING
CONTINUOUS LONG RESPONSE: INTERNALLY VERIFIED / OWNER PENDING
BARGE-IN: PAUSED / DISABLED (V2 OFF — will not interrupt at normal volume)
LISTENER PRESENCE: OFF · TURN AUTHORITY V2: OFF · PHONES: PRESERVED
MAC_CONTINUOUS_VOICE_BASELINE: `1fb60eabcaba2080`

OWNER HANDOFF — ONE TEST ONLY:
"Open rebuilt EV.app and ask: 'Explain the solar system in detail for about
one minute.' Stay completely silent while Evie answers."
Report: did it complete? any pause/stop/restart? did she stay silent after?


---

# ROUND SIX — PERIODIC 12–15 s SPEECH STALL (2026-08-23, P0)

## Owner evidence

Long normal answers stall every ~12–15 s for ~1–2 s, then continue. V2 OFF,
Listener OFF, TA-V2 OFF — the failure exists on the recovered baseline.

## Isolation evidence (live provider, capacity restored)

Five live captures through the REAL backend→WebSocket path via a bare WS
client: solar 779 chunks/77.8 s (max gap 153 ms), jet engine 711/71.1 s
(max 134 ms), apollo 794/79.4 s (one 1064 ms outlier), re-runs smooth; one
energy-instrumented capture found NO multi-second silent-PCM spans.
⇒ Provider delivery and backend forwarding are effectively continuous;
bare-client playback never stalls.

The stalls occur ONLY inside EV.app — whose mic tap runs on the SAME
AVAudioEngine I/O proc that renders assistant audio (TTSPlayer.bind(to:)).
BargeInTrace.log performed SYNCHRONOUS disk I/O (open/seek/write/close +
NSLog) on that tap thread for every heartbeat (~200 ms) and candidate.
Blocking I/O in a realtime audio callback stalls the shared I/O proc →
audible output underrun. V2 worsened it by logging per-frame reasons.
This also explains harness-vs-EV.app divergence (no tap, no trace writes).

## Fix

`BargeInTrace` rewritten as non-blocking telemetry: in-memory ring →
dedicated background writer (batched flush every 0.5 s), overflow drops
oldest; NSLog removed from the hot path. No other layer touched. All prior
invariants (episode gate OFF baseline = round-four fixed floor, quarantine,
preroll) unchanged.

Packaged EV.app sha16 `dc538ce945cfa08b`.
MAC_CONTINUOUS_STREAMING_BASELINE_dc538ce945cfa08b pending owner physical pass.

STATUS:
NORMAL MAC VOICE / LONG-FORM CONTINUITY: FIX SHIPPED / OWNER PENDING
BARGE-IN: PAUSED (V2 OFF) · LISTENER: OFF · TA-V2: OFF · PHONES: PRESERVED
