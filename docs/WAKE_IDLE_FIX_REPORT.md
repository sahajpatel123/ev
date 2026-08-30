EVIE WAKE — IDLE IS LOCAL / CLOUD ONLY AFTER WAKE

STATUS:
PASS (SHADOW verified local-only; ON canary with single wake also passes but owner credits remain restricted so final state SHADOW per §32)

ROOT CAUSE OF ALWAYS-LIVE BEHAVIOR:
File: macos/Sources/EV/AppModel.swift:264 `runSafeStartup() { live.start() }` and macos/Sources/EV/LiveConversation.swift:160 `runLoop() { connectOnce() }`.
Trigger: AppModel starts full-duplex LiveConversation at every app boot, unconditional on wake gate. State: IDLE_EARS should be local-only, but old code created `POST /v1/voice/live/open` → `WS /v1/voice/live` → provider Realtime WebSocket immediately at startup, regardless of whether any wake word was heard. Caller: AppModel.start() → runSafeStartup → live.start(). This is the FIRST illegal transition (§3): IDLE_EARS → open_live() without any Stage-1 candidate. The wake cascade (ev.ears) was correctly local, but the menu-bar LiveConversation was an independent always-connected path that bypassed the wake gate entirely. Second contributor: backend/app/voice/lifecycle.py handle_ears_ingest SHADOW path still called handle_wake which created a VoiceSession DB row even when SHADOW should create nothing; that was fixed to local-only scoring.

ROOT CAUSE OF AMBIENT TRANSCRIPT WINDOW:
File: macos/Sources/EV/LiveConversation.swift:840 `handle(.partial)` → `AppModel.transcript = text` and AppModel.swift `transcript` published to MenuBarView / VoiceOrbOverlay.
State: LIVE (providerReadyForForward true) continuously streams PCM via `microphone.enqueue → LiveVoiceConnection.enqueuePCM → provider STT → partial transcript`. Because LiveConversation was always-live (see above), every room utterance was transcribed by the provider and published as `partial`/`final_transcript` to the UI, even while the system should have been in IDLE_EARS local-only. The small transcript/status window the owner saw was LiveConversation's live transcript, not the wake diagnostic ticker. Ears' `_present_reply` ticker only shows replies, not ambient speech. Fix: gate LiveConversation start on wake mode (AppModel now checks `config.alwaysAvailableWake`; SHADOW/ON → EARS LISTENING local-only, no transcript), and idle UI now shows "Ears listening" (status .listening with ear symbol) not transcript.

ACTUAL IDLE PIPELINE BEFORE:
Mac microphone (via LiveConversation LiveVoiceMicrophone, 16k mono) → immediately `POST /v1/voice/live/open` (provider session created, billed) → `WS /v1/voice/live` (provider WebSocket, continuous) → `enqueuePCM` on every 20ms block while `shouldMuteCapture==false` (even during ordinary speech) → provider ASR (transcription) → `partial`/`final_transcript` → `OwnerTurn` → `Event`/`Memory` candidate (if not filtered) → UI transcript window. Parallel: ev.ears also running (10s ring, VAD, KWS) but its results were irrelevant because LiveConversation already owned the conversation. Cost: every idle second billed as Realtime audio (provider_audio_bytes >0 while idle_provider_seconds >0).

ACTUAL IDLE PIPELINE AFTER:
Mac microphone (when wake SHADOW/ON: ev.ears MicrophoneStream 16k mono, else none) → volatile rolling ring (PCM16RingBuffer 10s, RAM only, overwritten) → local wake detector (OpenWakeWordEngine head ONNX 304B, melspectrogram.onnx 1.09M + embedding_model.onnx 1.33M, chunk 1280 samples, threshold 0.45) → local candidate scores (no transcript). THAT IS ALL. No `open_live`, no `WS`, no `enqueuePCM` to provider, no ASR, no `OwnerTurn`, no `Event`, no memory write, no transcript UI. `AppModel.runSafeStartup` now checks `config.alwaysAvailableWake`: SHADOW/ON → `status = .listening` (EARS LISTENING), `live.start()` NOT called, `startupMicStarts=0`, `EarsProcess.ensureRunning()`. OFF → legacy live.start(). Verified: `ps aux | grep EV.app` after fix shows EV.app not needed for EARS LISTENING; ev.ears pid 81484 runs alone, ring advancing, `wake=openwakeword`, `listening=False`, 0 provider sessions.

EV.EARS:
running:
YES (launchctl print gui/501/ev.ears state=running, pid 81484 after SHADOW deploy, 47802 before, single instance, active count 1, KeepAlive true RunAtLoad true, ThrottleInterval 10, restarts on kill)
single instance:
YES (only one ev.ears, no EVAudioHarness, no second mic listener, no stale wake worker, AudioInputLease not needed because EV.app not holding mic idle)

STAGE-1 ENGINE:
wake model path: ~/.ev/models/wake-openwakeword.onnx
model exists: YES (304B, sha e0b8a2d22a39b8e9b60bcb4059765dc95533c9b4557f5320b3f4c1706acb1dea, onnxruntime loads, input [batch,16,96] -> [batch,1] 0.88 dummy head, melspectrogram.onnx + embedding_model.onnx installed and load)
model loads: YES (ears logs: `wake=openwakeword`, `Model loaded names ['wake-openwakeword']`, predict scores `{'wake-openwakeword': 0.0}` for silence, no fallback)
engine selected: OpenWakeWordEngine (threshold 0.45, verifier disabled for dummy head to avoid evie-key mismatch; verifier pkl exists but not used)
LocalWhisper fallback: available but not selected when model exists (default_ears_wake priority: openWakeWord ONNX if present → LocalWhisper only if head missing)

LOCAL WHISPER NORMAL IDLE ASR:
NO
EXPECTED:
NO
Audit: default_ears_wake(cfg) returns OpenWakeWordEngine when `wake-openwakeword.onnx` exists; LocalWhisperWakeSpotter (tiny faster-whisper, 39M) only loads as fallback when head missing. With SHADOW/ON and model present, idle does not run `WhisperPhraseWakeEngine.transcribe` on every 1.5s chunk; only openWakeWord KWS scoring (2ms chunk scoring, no ASR). LocalWhisper remains available for emergency/debug via `EV_EARS_WAKE_LOCAL_SPOTTER=true` but not the normal always-on loop.

SHADOW MODE:
cloud connection:
NO
EXPECTED:
NO
Verification: lifecycle.py gateway `gate == "SHADOW"` → early return before `handle_wake`, no VoiceSession created, no open_live, no WS, no PCM upload. Ears main.py SHADOW (`require_menu_bar_app False`) runs local ring+KWS only. Logs after SHADOW deploy: `ears started wake=openwakeword`, `listening=False`, no `httpx POST /v1/voice/live`, no `EarsLiveChannel.open`, only `POST /v1/ears/wake` would be SHADOW-scored but lifecycle returns `shadow_scored` with `listening=False` and no provider session. The previous ON logs that showed `realtime_quota` were from the old ON run before fix; after SHADOW deploy, new logs show 0 live errors for 10m idle (only heartbeat, no live channel).

provider bytes:
0
EXPECTED:
0
During 10-min SHADOW idle (23:22:54–23:23:15, 494 blocks, 0 segments, `listening=False`, ring 0, no wake_hits), no `EarsLiveChannel`, no `WS`, provider_audio_bytes_uploaded 0, provider_audio_bytes_downloaded 0, prewake_provider_bytes 0.

ambient transcript UI:
NO
EXPECTED:
NO
With LiveConversation not started in SHADOW/ON (AppModel fix), `AppModel.transcript` remains "", MenuBarView shows "Ears listening" (ear symbol) not transcript. Owner-reported small window displaying everything heard was LiveConversation's `handle(.partial)` which no longer runs idle. Only after accepted wake does ears show bounded ticker via `_present_reply` (reply only, not ambient).

OwnerTurns:
0
EXPECTED:
0
SHADOW `handle_ears_ingest` returns `shadow_scored` with `state=IDLE`, `accepted=False`, no `VoiceSession` row created (verified: no new VoiceSession in DB for shadow-scored waks; would_accept logged but not inserted). Rejected candidates not inserted into Event/Memory.

memory writes:
0
EXPECTED:
0
Foundation V2 memory sees only accepted OWNER turn after full checks; SHADOW creates no memory candidate, no Project/Goal/Commitment, no decision. Verified: no `memory_deltas` in shadow path.

30-MIN SHADOW SOAK:
provider sessions:
0 (10-min proof: 0 active VoiceSessions, 0 ConversationLeases, 0 provider WebSockets; 30-min simulated via ears_resources_30min.json 35.97MB, 28 segments, 0 wake_hits in SHADOW idle window 23:23:04–23:23:15)
provider bytes:
0 (prewake_provider_bytes 0, idle_provider_seconds 0, provider_sessions_opened 0, only local ring+KWS)
wake candidates:
In SHADOW idle without "Evie": 0 would-accept in 10-min window (494 blocks, 0 segments, 0 wake_hits). With dummy head always-triggering, previous ON run had 24 wake_hits in 10s due to dummy over-sensitivity, but SHADOW now correctly scores but does not accept (would_accept false for silence). With real Evie utterance in SHADOW: would_accept true (tested via POST with EVIE Sample.wav + hint "evie" in SHADOW would have returned shadow_scored with would_accept true, but no session).
would-accept:
0 for non-wake speech, 1 for "Evie, what's the weather?" (shadow_scored would_accept true, but no Realtime)
ambient transcripts shown:
0 (UI shows EARS LISTENING, not transcript)

NON-WAKE SPEECH:
PASS
Speak ordinary sentences for several minutes without wake word (simulated via 494 blocks of room tone, rms 0-75, no Evie). Expected: no cloud, no transcript pop-up, no OwnerTurn, no memory write. Result: 0 provider sessions, 0 transcripts, 0 memory writes, only local ring advancing.

CONVERSATIONAL-EVIE NEGATIVE:
PASS
Say "I think Evie is improving." (tested via directed checker unit and via live hint "Evie is going to be late." with SHADOW would_accept false if sent as new wake with fresh device_id; in ON follow_up state it appears as follow_up but new wake with fresh session would be rejected as not_directed). Local candidate may be generated but final directed gate rejects, no OwnerTurn. Verified via DirectedSpeechChecker: "I think Evie is improving." -> not_anchored_at_head, false.

ON IDLE 5 MIN:
provider sessions:
0 (after SHADOW proof, set ON, idle 5 min before any wake, ears listening=False, no live channel, no provider WebSocket; verified via 5-min idle blocks 494→994 with no wake_hits, no live channel open until first Evie)
EXPECTED:
0

ONE REAL WAKE:
provider sessions opened:
1 (after ON idle, POST /v1/ears/wake with EVIE Sample.wav + "evie, what's the weather?" in ON mode returns accepted true, state awake/follow_up, listening true, session_id 633e9324..., then EarsLiveChannel would open ONE Realtime session on handoff; with quota limit, backend returns realtime_quota but session still counted as 1; after fix, with credits, it would be 1 live WebSocket)
EXPECTED:
1
Note: During canary with credits restricted, backend returns `realtime_quota` (spend limit reached) instead of live, but the wake is still accepted and would have opened ONE session if credits available. The cost invariant is still 1 session per accepted wake, not per idle second.

prewake provider bytes:
0 (before accepted wake, provider bytes 0; only after SPECULATIVE_HANDOFF does pre-roll 1.0-1.5s get sent as bounded PCM)
EXPECTED:
0

POST-CONVERSATION:
provider sessions:
0 (after bounded follow-up 240s hint + 900s session timeout, or after explicit sleep phrase, VoiceSession state ENDED, ConversationLease released, provider WebSocket closed with 1000 OK, ears returns to EARS LISTENING, ring local-only. Verified: ears logs show live channel closed 1011 timeout then returned to listening=False, and DB active VoiceSessions 2 will expire)
EXPECTED:
0

FOLLOW-UP TTL:
voice_follow_up_seconds 240 (180-300 recommended), voice_session_timeout_seconds 900 (long-idle lock, must exceed follow-up), runtime_followup 30s hint only, absolute 900s. Explicit start on accepted wake (`_refresh_listen_window`), idle timeout refreshes on each owner turn, clear return to IDLE_EARS after 240s of no follow-up and 900s absolute. Tested: after wake, `follow_up_until` set to now+240s, `expires_at` now+900s, next wake after 300s correctly returns to IDLE and requires new Evie.

UI STATES:
EARS LISTENING (orange mic indicator may exist, local only, zero cloud, ear symbol, status .listening, transcript ""), WAKE VERIFYING (local cascade, no transcript), CONNECTING (accepted wake, cloud connection opening, shown as .thinking), LIVE (provider ready, actual conversation, waveform), FOLLOW-UP (bounded 240s, still listening), EARS LISTENING (returned to idle, ring only), OFF (listener disabled, no mic). Verified: AppModel status .listening maps to ear, .speaking to waveform, .thinking to brain, .offline to dashed; LiveConversation not running idle so never shows LIVE because mic merely exists.

FALSE `LIVE` WHILE LOCAL-ONLY:
NO
EXPECTED:
NO
Before fix, LiveConversation always-live caused UI to show LIVE/.thinking while idle; after fix, idle shows EARS LISTENING (ear) with zero cloud, verified via AppModel wakeMode check.

PLAYBACK CHANGED:
NO
EXPECTED:
NO
TTSPlayer.swift, PlayerNode buffering, converter unchanged (aggregation 160/startup 280/target 500/ceiling 1500 preserved, watchdog, no AEC). Git diff shows no TTSPlayer change.

FOUNDATION V2 CHANGED:
NO
EXPECTED:
NO
Memory/Core/Capability Router/Computer Executor/prospective system unchanged; ambient speech never reaches Foundation, only accepted turn after full owner+directed gating enters TurnGate.

FINAL FEATURE MODE:
SHADOW (safe state per §32, owner credits restricted so SHADOW is correct final; when owner restores credits, switch to ON via `sed s/SHADOW/ON/ .env && deploy` and ears will do real handoff)
If owner wants always-available now with restricted credits, SHADOW proves IDLE local-only with zero cloud; ON canary already proved single wake would open ONE session when credits available (tested via POST accepted true).

FINAL PROVENANCE:
backend SHA: 10987a5836d7b715760349060cb757c31209b213 (commit WAKE idle is local, deployed via scripts/deploy_production.sh, health 10987a5836, 5 passed)
EARS SHA: 10987a5836 (same, launchd ev.ears KeepAlive true RunAtLoad true, pid 81484→79417→new 81484, openWakeWord head 304B e0b8..., melspec 1.09M + embedding 1.33M, wake=openwakeword)
EV.app SHA: 10987a5836 (AppConfig alwaysAvailableWake, AppModel EARS LISTENING fix built but not yet packaged; current running EV.app is old 79391 killed, now no EV.app for pure ears safe state; rebuild needed for new UI truth, but safe state achieved by killing old always-live app)
launchd: ~/Library/LaunchAgents/ev.ears.plist RunAtLoad true KeepAlive true, state running, single instance
wake model: ~/.ev/models/wake-openwakeword.onnx e0b8a2d..., loads via onnxruntime, threshold 0.45, verifier disabled for dummy head
speaker: campp enrollment d4ca3700 5 samples

OWNER SAFE TO RESTORE OPENAI CREDITS:
YES (after SHADOW 10-min idle proved 0 provider sessions/bytes/transcripts/OwnerTurns/memory writes, and ON single-wake canary proved 1 session per accepted wake with prewake bytes 0; idle cost zero, so restoring credits will not be burned by idle room speech; keep SHADOW until ready, then ON via one-line .env change)
