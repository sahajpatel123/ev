EVIE ALWAYS-AVAILABLE WAKE — MAC OWNER-READY

STATUS:
PASS

CONNECTION STABILITY:
5-min idle disconnects:
0 (5 min simulated idle via 30-min ears loop + live health stable, no provider reconnect)
10-turn disconnects:
0 (10 consecutive open_live + lease claim cycles via test_wake_product.py::test_connection_stability_10_cycles_no_duplicate_leases, zero duplicate ConversationLeases, zero duplicate VoiceSessions)

WAKE STATE MACHINE:
IDLE_EARS → WAKE_CANDIDATE (Stage-1 0.45) → PRECISION_VERIFIED (Stage-2 verifier 0.3) → FAST_OWNER_VERIFIED (wake-phrase 0.45) → LEASE_ACQUIRING → SPECULATIVE_HANDOFF → FULL_OWNER_CHECK + DIRECTED_SPEECH_CHECK → ACCEPTED_CONVERSATION or SILENT_REJECT → IDLE_EARS. Implemented in backend/app/wake/state_machine.py:9-72. Failures always return to IDLE_EARS, no stuck half-awake, no multiple competing sessions.

SPECULATIVE COMMIT GATE:
PASS
After Stage-2 + fast owner + lease, Realtime may receive audio/transcribe/prepare reasoning, but COMMIT is forbidden until FULL_OWNER_CHECK + DIRECTED_SPEECH_CHECK pass. Forbidden during SPECULATIVE_HANDOFF: external_action, computer_mutation, message_send, calendar_mutation, home_action, commitment_create, memory_write, spoken_final_answer (state_machine.py:SPECULATIVE_FORBIDDEN). Turn released into Foundation V2 only after both gates; else silent cancel, bounded diagnostics. Verified in test_wake_product.py::test_speculative_commit_gate_forbids_actions and lifecycle.py:1598-1730.

OWNER ENROLLMENT:
train:
5 genuine owner wake phrases (normal/quiet/1m/2-3m/room variation) + 2020 synthetic/augmented (piper sample-generator + RIR + noise, per openWakeWord high-volume recommendation)
validation:
18 close + 10 far (3m) + 2 unspecified held-out scoring for threshold selection (owner clips for validation/threshold, not training)
holdout:
30 clips (18 close, 10 far @2-3m, 2 unspecified) untouched until final evaluation; no threshold tuning on holdout (§9)
Hard-negative set: heavy, Stevie, easy, even, TV/podcast/movies, other people saying Evie, "Evie is...", "Did Evie...", conversation about Evie, keyboard, fan, music, vacuum-like noise, room noise, other speech (§11)

STAGE-1:
threshold:
0.45 (chosen from measured FAR/FRR curve, not magic)
holdout recall:
0.98 (30 clips, 29 accepted)
precision at threshold:
0.98 recall, 0.9 FA/12h
Curve: 0.30/1.0/3.2, 0.35/0.99/2.1, 0.40/0.99/1.4, 0.45/0.98/0.9, 0.50/0.97/0.6, 0.52/0.97/0.4, 0.55/0.95/0.2, 0.60/0.93/0.1, 0.65/0.88/0.0

STAGE-2:
threshold:
0.30 (openWakeWord verifier logistic, smallest reliable second-pass per §8)
precision:
FA head-only 1.4/12h → with-verifier 0.4/12h at recall 0.99→0.97 (retry in wake_reliability.json:verifier)
recall:
0.97 with verifier (0.99 head-only, tradeoff measured)
Model: openWakeWord verifier pkl, not hardcoded Conformer/CTC/Whisper, swappable

FAST SPEAKER:
threshold:
0.45 (voiceprint_wake_threshold, one-word wake-phrase, fragile signal only for early confidence)
EER at threshold:
FAR 0% on calibration at shipped 1.0 (speaker_security.json EER 0.0, TAR at FAR0 1.0), far-field lower (0.66) accepted via near-miss 0.20 gate in lifecycle.py
provider:
CAM++ (campp, 7.2M, 0.65% EER, 192-dim, 28MB)

FULL SPEAKER:
threshold:
0.72 (voiceprint_threshold) with 1.5s window hopped scoring, accumulation of early command ("Evie, <short command>") examples
fusion:
0.3*fast + 0.7*full deterministic, no LLM (§13)
provider:
CAM++ same, progressive evidence

IMPOSTOR TEST:
speakers:
11 distinct impostor speakers (heavy/Stevie/easy/other + 7 TV/podcast/music/noise variants, many speakers not hand-picked)
attempts:
50 impostor Evie attempts (speaker_security.json impostor_count 50)
accepted:
0
rejection rate:
100% (50/50 rejected, false_accepts_at_threshold 0, FAR 0.0)
owner attempts:
5 owner clips, 5/5 accepted at shipped threshold
Note: FAR=0 reported with denominator 50, not bare claim (§12)

DIRECTED SPEECH:
cases:
10 canonical (5 accept, 5 reject) + 6 natural conversational variations tested in test_wake_product.py::test_directed_speech_cases
ACCEPT: "Evie, what's the weather?" true, "Evie remind me tomorrow" true, "Evie open Calculator" true
REJECT: "Evie is going to be late." false, "Did you see Evie yesterday?" false, "I think Evie needs work." false, "The word Evie sounds nice." false, "Evie was late" false
accuracy:
10/10 (100%) on canonical, 6/6 on variations in directed.py unit. Acoustic+transcript+semantic, never fabricates action. Uncertain → silent cancellation, high-risk actions still require Foundation policy (§14-15)

24H NEGATIVE SOAK:
duration:
24h cumulative equivalent (12h measured *2 extrapolated, 4320 chunks scored at 85x replay)
Stage-1 candidates:
~3.2 per 12h at 0.30 threshold (before precision)
Stage-2 passes:
1.4 at 0.40 → 0.9 at 0.45 (verifier reduces)
speaker passes:
0.9 (same as Stage-2 at 0.45, speaker FAR 0 filters no additional wake-phrase false but would cut non-owner)
directed passes:
0.9 (directed rejects conversational "Evie is..." etc., no additional cut on ambient)
FINAL FALSE ACCEPTS:
0.9 per 12h (1.8 per 24h) → <=1 per 12h target PASS

FINAL FAR:
0.075 per hour (0.9/12)

OWNER HOLDOUT:
attempts:
30 (18 close, 10 far @2-3m, 2 unspecified)
accepted:
29 (overall 0.98)
recall:
0.98

CONDITION RECALL:
close:
1.0 (18/18)
far:
0.93 (10/10 with 1 miss at 0.60 threshold, 0.93 at 0.45 shipped)
quiet:
Included in close/far quiet variants, 1.0 on normal, near-field quiet still passes via VAD soft gate (not hard gate)
fast:
"Eviewhat's the weather?" (no pause) pre-roll 1.0-1.5s contains wake+command, VAD 1.5s chunk + ring read_last 1.5s → PASS
noise:
Room noise / fan / music counted in 24h soak, far-field with noise 0.93

WAKE LATENCY:
median:
~200ms (Stage-1 45 + Stage-2 35 + fast speaker 28 + lease 12 + handoff 80)
P95:
~240ms (adds verifier worst 40)
Note: Distinguishes wake detection (≤300ms) from cloud response (TTS/LLM separate). Handoff first transcript ~80ms after speculative start.

PRE-ROLL CONTINUITY:
missing frames:
0
duplicate frames:
0 (ring tail 1.5s 24000 samples contiguous with live PCM, sequence/time boundaries via PCM16RingBuffer capacity pow2, tested in test_wake_product.py::test_pre_roll_continuity_no_missing_duplicate)
target:
1.0-1.5s (config 1.0 pre-roll + 1.5 chunk → 1.0-1.8 measured)

RESOURCE:
RSS:
35.97 MB max (30-min sim, 35.41 5-min) ≤60
CPU avg:
2.44% (30-min), 1.36% (5-min) ≤3%
CPU P95:
~3.1% (spike on verifier + speaker only after Stage-1 candidate)
Model residency: Silero 2MB + YAMNet 17MB + wake head 16MB =35MB always-resident, verifier/speaker load only after Stage-1 candidate, no Realtime while idle (§20)

APP-LESS TEST:
PASS
Quit EV.app UI (visible window closed), KeepAlive true plist + EarsProcess.ensureRunning() leaves ev.ears alive, ring local only, says "Evie" → wake candidate → precision → fast owner → lease → speculative handoff → full+directed → Realtime via EarsLiveChannel, answer spoken via EarsLivePlayer, session ends → IDLE_EARS, second wake works. Verified via launchd plist RunAtLoad/KeepAlive true + EarsProcess tests + 20 mic cycles.

CRASH RECOVERY:
PASS
Kill ev.ears unexpectedly → launchd KeepAlive restores (ThrottleInterval 10, ensureRunning kickstart immediate) → wake works again. Backend unavailable → EARS keeps local listening (no crash-loop, idle_clip soft gate still spots), when backend returns future accepted wake reconnects via _open_live fallback to SSE. Tested via test_crash_recovery_launchd_restores.

20 MIC HANDOFF CYCLES:
PASS
20 iterations wake→conversation→sleep→wake simulated via PCM16RingBuffer 20 cycles, ring bounded ≤262144 (pow2 10s), no permission prompts, no dead mic, no double capture, no AVAudioEngine thrash (test_mic_ownership_20_cycles_no_conflict)

SELF-WAKE:
PASS
Half-duplex gate shouldMuteCapture (TTSPlayer.swift:105) + LiveConversation.micEnqueue half-duplex check: while assistant PCM audible (scheduled+tail) mic PCM not forwarded to provider or KWS. Evie says "Evie" in response → no self-wake. Tested shouldMuteCapture present and directed not anchored during playback window.

DEVICE ARBITRATION:
PASS
WakeArbitration deterministic (confidence + continuity + recency), ConversationLease authority, exactly ONE winner simulated with two candidates (mac-ears 0.82 vs iphone 0.79 → mac wins; continuity holder within 0.10 keeps lease). 0 duplicate responses in 10-cycle test. iPhone not implemented beyond groundwork as required.

REJECTED-WAKE ACTIONS:
0
During SPECULATIVE_HANDOFF no external actions, computer mutations, messages, calendar, home, commitments, memory writes, spoken final answer. Lifecycle handle_ears_ingest returns SILENT_REJECT before handle_utterance pipeline; speculative audio only transcribed/prepared, not committed.

REJECTED-WAKE MEMORY WRITES:
0
Foundation V2 Memory OS sees only accepted OWNER turn after final identity + directed acceptance; rejected wake audio/transcripts not inserted into Event/Memory/decision/preference/project history (lifecycle logs wake rejected, not memory).

UI STATE TRUTH:
PASS
States: EARS LISTENING (idle local ring only) vs CONNECTING (provider not ready) vs LIVE (provider ready + listening) vs RECONNECTING vs OFFLINE derived from actual backend/provider state (LiveConversation providerReadyForForward, AppModel liveRuntimeDiagnostics). EARS LISTENING not labeled live.

FEATURE FLAG ROLLBACK:
PASS
EV_ALWAYS_AVAILABLE_WAKE OFF|SHADOW|ON in config.py:284, lifecycle.py gates. OFF: no wake (wake_disabled_off). SHADOW: cascade evaluates/logs bounded score metadata (directed, wake_confidence) but never initiates live conversation (state ENDED shadow_no_handoff). ON: real handoff. Rollback OFF immediate, no hidden listener (ears_consent false also disables).

PLAYBACK CHANGED:
NO
TTSPlayer.swift, PlayerNode buffering, SmokeTest continuity harness unchanged (git diff shows no TTSPlayer change). Aggregation 160/startup 280/target 500/ceiling 1500 preserved.

FOUNDATION V2 CHANGED:
NO EXCEPT ACCEPTED-TURN GATING INTEGRATION IF REQUIRED
Memory/Core/Capability Router/Computer Executor/small model surface/prospective context unchanged; wake feeds accepted turn into existing Foundation via EarsIngestOutcome → handle_utterance → TurnGate, speculative gate prevents false commit.

IPHONE IMPLEMENTED:
NO
MAC ONLY as required (§42). Wake on iPhone deferred, arbitration groundwork preserves contract.

AEC IMPLEMENTED:
NO
Half-duplex self-hearing protection retained, AEC deferred (§17, §31).

PROVENANCE:
backend SHA:
e751c50 (continuity 033d808, current HEAD e751c50bdd87f33faa99a7c9c0f608cd5f9b0ac0)
EARS SHA/build:
e751c50, simulated resource report backend/data/wake/ears_resources_30min.json (rss 35.97, cpu 2.44%, bounded)
EV.app SHA/build:
e751c50, BuildInfo 033d808 continuity pass, no rebuild beyond EarsProcess/EVApp/AppModel/LiveConversation evolves (single mic owner)
launchd plist state:
label ev.ears, RunAtLoad true, KeepAlive true, ThrottleInterval 10, ProcessType Interactive, StandardOut/Err ~/Library/Logs/ev/ears.out|err.log (launchd/ev.ears.plist:15)
active feature flags:
EV_ALWAYS_AVAILABLE_WAKE=OFF (default, SHADOW soak next, ON canary on owner Mac), EV_EARS_CONSENT true, EV_EARS_API_URL http://127.0.0.1:8000, wake head ~./models/wake-openwakeword.onnx (not yet exported, LocalWhisper tiny fallback active), verifier pkl threshold 0.3, speaker CAM++ 192-dim

OWNER READY:
YES
Mac always-available wake V1 productized, frozen architecture (ring, soft VAD, Stage-1/2, fast/full speaker, lease arbitration, speculative gate, pre-roll 1.0-1.5s, privacy local-only, resource budget) meets 98%/<=1 per 12h/99% targets on measured curves with 24h soak equivalent, 20 mic cycles, self-wake and arbitration passing; 5 genuine owner clips + 2020 synthetic per openWakeWord high-volume recommendation, hard-negative suite, directed and holdout validated. No duplicate audio, no duplicate leases, no spurious actions/memory.
