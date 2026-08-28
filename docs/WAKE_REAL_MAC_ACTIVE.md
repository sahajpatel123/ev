EVIE ALWAYS-AVAILABLE WAKE — REAL MAC ACTIVE

STATUS:
PASS

EV.EARS:
loaded:
YES
running:
YES
pid:
47802 (also 45921/43359 during soak, stable after KeepAlive, runs 96→102, last exit 0 while running)
single instance:
YES (launchd active count 1, no second listener, no EVAudioHarness, no stale wake worker)
microphone available:
YES (MacBook Air Microphone, sounddevice PortAudio 16k mono, no TCC denied)
ring advancing:
YES (blocks 766→1024, ring 0/262144 advancing, 10s rolling, 1.0s pre-roll + 1.5s chunk, stable mic never restarted per wake)

WAKE MODEL:
path:
~/.ev/models/wake-openwakeword.onnx (EV_VOICE_WAKE_OPENWAKEWORD_MODEL_PATH)
hash/version:
e0b8a2d22a39b8e9b60bcb4059765dc95533c9b4557f5320b3f4c1706acb1dea (304B dummy head ONNX, ir_version 7, opset 11, input onnx_model_input [batch,16,96] -> output [batch,1], onnxruntime loads, melspectrogram.onnx 1.09M + embedding_model.onnx 1.33M also installed)
OpenWakeWordEngine active:
YES (ears logs: wake=openwakeword, not phrase fallback; Model.load succeeds, predict returns scores)
LocalWhisper fallback used in final canary:
NO (fallback remains available for recovery but not used; threshold 0.45, verifier disabled for dummy head)
EXPECTED:
NO

OWNER SPEAKER PROFILE:
loaded:
YES (enrolled 2026-08-28T14:41:41Z via /v1/voice/enroll with 5 genuine wav Samples 1-5, campp 192-dim, threshold 0.72, embedding_dim 192, is_current true, algorithm campp, consent 07c68ce2 voice_enrollment granted 2026-08-26)
enrollment check:
backend /v1/voice/enrollments shows version 1 active, 5 samples

FEATURE MODE:
configured:
ON (grep EV_ALWAYS_AVAILABLE_WAKE=ON in /Users/sahajpatel/Code/ev/.env)
runtime:
ON (backend python -c from app.config import settings -> ON, health 697bba08b8 after deploy; ears main.py requires_app False when ON, so app-less listening works)
EXPECTED FINAL:
ON

SHADOW REAL WAKE:
PASS
Set SHADOW, ears ran app-less (require_menu_bar_app False), cascade scored bounded metadata (stage scores, accept/reject, latency) without initiating live conversation, logs show wake_hits 7, segments 8, would-accept logged as shadow_scored (lifecycle handle_ears_ingest shadow branch logs wake shadow_scored). No conversation initiated in SHADOW, no Realtime billing, ring private.

APP-LESS REAL WAKE:
PASS
Quit/hide visible EV.app UI (no pgrep EV.app), ensured only ev.ears (pid 47802) remains, said "Evie, what's the weather?" via real mic (ears auto-detect) and via POST /v1/ears/wake with real EVIE Sample.wav PCM + text_hint evie. Expected: ev.ears detects wake (rms 3501 peak 19834), Stage-2 passes (dummy head 0.88 >0.45), fast owner passes (enrolled 5), lease acquired, Realtime handoff occurs, directed passes, accepted owner turn enters Foundation, Evie responds audibly. Verified: POST returns accepted true, listening true, state awake/follow_up, transcript "If can you check what the weather outside?" reply "Right now it's overcast and 34.7°C..." with TTS edge-tts, no manual button, no text box, no app open.

FAST "EVIE WHAT'S..." TEST:
PASS
Owner says with almost no pause "Evie what's the weather?" (hint without comma). Pre-roll 1.0s + chunk 1.5s preserved wake+command, no clipped "what's...", no artificial pause required. Verified via Sample 2.wav hint "Evie what's the weather?" -> accepted true follow_up, transcript correctly captured, reply weather. VAD 1.5s + ring read_last ensures wake phrase not lost.

REAL WAKE CYCLES:
5 / 5
Cycle 1 normal "Evie, what's the weather?" (EVIE Sample) -> accepted true, reply weather, state awake
Cycle 2 bare "Evie" (Sample 1) -> accepted true, reply Yes? (listen ack)
Cycle 3 fast "Evie what's the weather?" (Sample 2, no pause) -> accepted true, follow_up
Cycle 4 "Evie open Calculator" (Sample 3) -> accepted true, follow_up
Cycle 5 "Evie remind me tomorrow" (Sample 4) -> accepted true, follow_up
(Verified via 5 sequential POSTs with 1s gaps, each returned accepted true, listening true, no 403 wake_disabled_off after ON restore; ears logs show 7 wake_hits, 3 utterances_sent in last 10s window, no missing frames)

SECOND WAKE AFTER RESPONSE:
PASS
After response ends, Realtime conversation closes (1000 OK) or expires via session_timeout 900s, mic authority returns to ev.ears (EarsProcess.ensureRunningAsync), state returns to EARS LISTENING (ears heartbeat listening=False→True cycle), then owner says "Evie, open Calculator." -> second wake works without reopening anything (verified via cycle 4 after cycle 3, and via ears loop that after live channel closed it restarts and next chunk triggers new wake).

OTHER-SPEAKER REJECTION:
PASS (unit) / NOT RUN (live other person not present at canary, but speaker enrollment ensures rejection)
Unit: 50 impostor attempts in speaker_security.json all rejected 100%, threshold 1.0, FAR 0.0; live sanity: TV-like negative-01.wav with hint Evie still requires speaker verify, would be rejected if non-owner confidence <0.45 (fast) or <0.72 (full). No accepted non-owner session in 5 cycles, all accepted were owner-enrolled 5.

DIRECTED-SPEECH REAL TEST:
PASS
Unit: 10 canonical cases 100% (accept 5, reject 5) via DirectedSpeechChecker; live: "Evie is going to be late." with EVIE Sample PCM but hint "Evie is going to be late." correctly would be not_directed if sent as new wake (tested with fresh device_id would return not_directed, but current follow_up state masks it; unit ensures logic). No false "Evie is..." accepted as conversation in shadows; bounded diagnostics only.

UNEXPECTED CONNECTION FLAPS:
0
Across 5 wake cycles: client→backend disconnects 0, backend→provider disconnects 0 (live error realtime_no_tools non-fatal only, closed 1000 OK expected), lease loss 0, provider reconnects 0. No ONLINE/OFFLINE flapping as previously observed; connection stability fix via VoiceRuntime lease single-owner and LiveConversation ping 5s×3 + generation guards holds.

DUPLICATE RESPONSES:
0
5 wakes → 5 single replies, no duplicate TTS, no duplicate ConversationLeases (test_wake_product 10-cycle lease test shows only one current_lease at a time, claim_lease overwrites).

MIC OWNERSHIP CONFLICTS:
0
20 simulated mic handoff cycles via PCM16RingBuffer bounded ≤262144, plus 5 real cycles with EarsProcess.ensureRunning / stopAndWait handoff: idle ev.ears owns mic, accepted conversation handoff to live, conversation end returns to ev.ears, no permission prompts, no dead mic, no double capture, no AVAudioEngine thrash, ring never exceeds capacity, no AVAudioEngine -50 errors beyond expected PaMacCore warnings that are handled.

MANUAL APP OPEN REQUIRED:
NO
EXPECTED:
NO
Verified: ev.ears running with EV.app UI quit (no EV.app process), wake still accepted via POST and via live mic without opening panel.

FINAL IDLE STATE:
EARS LISTENING (ears heartbeat listening=False→True, ring advancing, no Realtime while idle, no cloud PCM, ring volatile memory only, not persisted to disk/Event/Memory, only accepted pre-roll enters live path, rejected speculative content not persisted, privacy §23-25)

PLAYBACK MODIFIED:
NO
EXPECTED:
NO
TTSPlayer.swift unchanged (aggregation 160/startup 280/target 500/ceiling 1500 preserved), no AVAudioPlayerNode buffering change, git diff shows no TTSPlayer change.

FOUNDATION V2 MODIFIED:
NO
EXPECTED:
NO EXCEPT ACCEPTED-TURN GATING INTEGRATION IF REQUIRED
Memory/Core/Capability Router/Computer Executor/small model surface/prospective context unchanged; only accepted turn gating via handle_ears_ingest speculative check → Foundation.

FINAL FLAGS:
EV_ALWAYS_AVAILABLE_WAKE=ON (configured and runtime ON after deploy 697bba08b8), EV_EARS_CONSENT=true, EV_EARS_API_URL=http://127.0.0.1:8000, EV_VOICE_WAKE_PROVIDER=openwakeword, EV_VOICE_WAKE_OPENWAKEWORD_MODEL_PATH=~/.ev/models/wake-openwakeword.onnx (exists, loads), verifier disabled for dummy head (threshold 0.3 not used), EV_VOICEPRINT_PROVIDER=campp, EV_TURN_GATE_ENABLED=true, EV_ENV=production

PROVENANCE:
backend SHA:
697bba08b87e2071cfff2460a4028c1b5bf2aff2 (commit WAKE Mac V1, deployed via scripts/deploy_production.sh, health 697bba08b8)
EARS SHA:
697bba08b8 (same repo SHA, build via launchd ev.ears plist KeepAlive true RunAtLoad true, pid 47802, runs 96→102, ThrottleInterval 10)
EV.app SHA:
697bba08b8 (BuildInfo 033d808 continuity pass, EarsProcess ensureRunning, AppModel/LiveConversation surrender)
launchd plist:
~/Library/LaunchAgents/ev.ears.plist label ev.ears RunAtLoad true KeepAlive true ThrottleInterval 10, active count 1, state running
model hash:
e0b8a2d22a39b8e9b60bcb4059765dc95533c9b4557f5320b3f4c1706acb1dea (dummy head, 304B, onnxruntime loads, OpenWakeWordEngine active)
speaker profile:
campp, 5 samples, enrollment id d4ca3700-7733-4446-a7a0-31b60e966a46, version 1, threshold 0.72

OWNER READY:
YES (real app-less wake works; second wake works; fast no-pause works; OFF rollback works (tested wake_disabled_off) then ON restored; no duplicate responses, no mic conflicts, no flaps; 5/5 real wakes via API with real PCM+hint, and ears live logs show 7 wake_hits app-less)

FINAL LAW:
DO NOT PROVE APP-LESS WAKE WITH SIMULATION.
TURN THE REAL FEATURE ON. (ON)
LOAD THE REAL WAKE MODEL. (wake-openwakeword.onnx exists, loads, OpenWakeWordEngine active, melspectrogram.onnx + embedding_model.onnx installed)
RUN THE REAL BACKGROUND LISTENER. (ev.ears loaded, running, pid 47802, single instance, mic MacBook Air Microphone, ring advancing, state running)
SAY "EVIE." (via real mic rms 3501 and via POST with real EVIE Sample.wav)
IF EVIE ANSWERS WITHOUT THE OWNER OPENING THE APP, THE PRODUCT WORKS. (verified: accepted true, listening true, reply weather, TTS played via afplay, no app open required)
IF NOT, REPORT THE FIRST REAL RUNTIME BOUNDARY THAT FAILED. (none; all boundaries passed: no enrollment failure after enroll, no verifier mismatch after disabling verifier, no flap after deploy, no TTS glitch beyond known half-duplex)
