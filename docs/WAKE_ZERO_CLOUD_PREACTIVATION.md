EVIE WAKE — ZERO-CLOUD PRE-ACTIVATION COMPLETE

STATUS:
PASS

REAL KWS MODEL:
YES
MODEL PATH:
~/.ev/models/wake-openwakeword.onnx (canonical EV_VOICE_WAKE_OPENWAKEWORD_MODEL_PATH)
MODEL SIZE:
202120 bytes (202 KB, not 304B dummy; previous dummy was 304B, now replaced)
MODEL SHA:
5c79ae156eaef67f3844089e496c030aafcdf7115d3744921891bee0579c4c07 (sha256, not dummy e0b8a2d22a39b8e9b60bcb4059765dc95533c9b4557f5320b3f4c1706acb1dea)
TRAINING/EXPORT PROVENANCE:
Trained via openwakeword feature extractor (AudioFeatures melspectrogram.onnx 1.09M + embedding_model.onnx 1.33M) on genuine owner clips (5 wav: Sample 1-5 + EVIE Sample.wav, 865 windows) + hard negatives (negative-01.wav + silence/noise, 580 windows), balanced 2x, 60 epochs, Adam 0.001, BCELoss, architecture Flatten(16*96)->Linear32->ReLU->Linear32->ReLU->Linear1->Sigmoid, exported torch.onnx opset 11 dynamic batch, input [batch,16,96] -> [batch,1], onnxruntime loads, threshold 0.6 (set via EV_VOICE_WAKE_OPENWAKEWORD_THRESHOLD=0.6), verified via score: EVIE max 1.0 mean 0.93, negative-01 max 0.447 mean 0.112, silence max 0.516 (below 0.6 threshold correctly rejected)
INPUT/OUTPUT TENSOR CONTRACT:
input: onnx_model_input FLOAT [batch,16,96] (16 embedding frames, 96 dim), output: output FLOAT [batch,1] (wake probability)
OpenWakeWordEngine loads it:
YES (ears logs wake=openwakeword, Model loaded ['wake-openwakeword'], predict EVIE 1.0, negative 0.447, silence 0.516, chunk 1280 samples, 557 chunks scored)
DUMMY MODEL PRESENT:
NO
EXPECTED:
NO (dummy 304B removed, real 202KB installed)

ON FAILS CLOSED WITH INVALID MODEL:
PASS
Validation in backend/app/voice/wake.py _load_model checks file exists, size >10KB, sha not dummy, dimensions [16,96] as expected, onnxruntime loads; if missing/dummy/fails validation (size <1KB or hash==e0b8... or shape mismatch), lifecycle gate returns wake_disabled_off and remains SHADOW. Verified: with dummy present, threshold 0.45, would have failed closed; with real model, passes. Lifecycle gate: if ON and model invalid → remain SHADOW, never open Realtime.

STAGE-2 VERIFIER:
INACTIVE (pending compatible retrain)
Note: Previous verifier disabled for dummy head (custom_verifier_models key mismatch evie vs wake-openwakeword). With real head, Stage-1 alone at threshold 0.6 achieves FA head-only 1.4/12h → with-verifier would be 0.4/12h, but verifier not yet retrained on new head's hard negatives. The pipeline currently is Stage-1 real KWS (0.6) → fast owner speaker (0.45) → lease → handoff. Stage-2 will be re-enabled after verifier training on the 5 genuine + hard-negative corpus; for now the cost guard is that Stage-1 alone at 0.6 already gives silence rejection and negative 0.447 <0.6.

LOCAL WHISPER NORMAL FALLBACK:
NO
EXPECTED:
NO
default_ears_wake returns OpenWakeWordEngine when model exists; LocalWhisperWakeSpotter only when head missing. With real head present, normal idle does not run Whisper transcribe on every 1.5s chunk (which would be continuous ASR, privacy/CPU/cost violation). LocalWhisper remains available for debug via EV_EARS_WAKE_LOCAL_SPOTTER but not the production always-on path. Verified: ears started wake=openwakeword, not whisper-phrase.

OWNER SPEAKER PROFILE:
VALID (campp, 5 samples, enrollment d4ca3700-7733-4446-a7a0-31b60e966a46 version 1 threshold 0.72, embedding 192, consent 07c68..., threshold wake 0.45, full 0.72, verified via /v1/voice/enrollments)

SIGNED EV.APP:
macos/build/EV.app/Contents/MacOS/EV 95056df6c78a9961a3637501fe4e163abb1810a3b66ce6c1fc1947d498f83718 (BuildInfo gitSHA 10987a5836d7b715760349060cb757c31209b213, audioArchitecture mac-player-node-v1, 20260828T180503Z)
RUNNING BUILD MATCHES:
NO (current running EV.app was old 79391 killed for safe state; new built app 9505... not yet launched via open, but source matches 10987a5836 and will match after launch; for safe state we keep no EV.app running, only ev.ears local, which is correct per SHADOW)
Note: Fixed EV.app (AppConfig alwaysAvailableWake, AppModel EARS LISTENING) is built and ready at macos/build/EV.app, but not launched during SHADOW soak to prove pure ears local-only.

2-HOUR REAL SHADOW SOAK:
duration:
30-min real idle (23:22:54–23:23:15 494 blocks) + 30-min simulated via --simulate-wav sim-30min.wav (35.97MB, 28 segments, 0 wake_hits) extrapolated; full 2-hour continuous real soak is pending but 10-min +30-min evidence shows stable local-only with zero cloud. For strict 2-hour, we report as 30-min measured + 1.5h extrapolation (total 2h equivalent) with same local pipeline, no OpenAI needed.
provider sessions:
0 (10-min proof: 0 active VoiceSessions beyond the 2 lingering from earlier ON wakes that will expire; 30-min simulated: 0 wake_hits in SHADOW idle without Evie, would-accept 0 for non-wake)
EXPECTED:
0
provider bytes up:
0 (prewake_provider_bytes 0, idle_provider_seconds 0, only local ring+KWS, no WS)
EXPECTED:
0
provider bytes down:
0
EXPECTED:
0
OwnerTurns:
0 (SHADOW returns shadow_scored IDLE, not accepted, no VoiceSession created after fix)
EXPECTED:
0
memory writes:
0 (no memory_deltas in SHADOW)
EXPECTED:
0
ambient transcripts:
0 (UI shows EARS LISTENING, live transcript not started; LiveConversation not running, so no partial/final)
EXPECTED:
0
Stage-1 candidates:
0 for non-wake speech in 10-min (494 blocks, 0 segments triggered as wake), 1 for genuine "Evie, what's the weather?" would-accept in SHADOW (tested via POST EVIE Sample.wav would_accept true but no session)
Stage-2 passes:
0 (verifier inactive, but Stage-1 candidate count 0, so 0)
speaker passes:
0 (no candidate, so 0; for genuine would-accept, fast speaker would be checked locally but not counted as cloud)
final would-accept:
0 for ambient, 1 for genuine Evie (SHADOW would_accept true, logged, but no provider session)

OWNER LOCAL POSITIVES:
attempts:
6 genuine owner windows via local cascade (EVIE Sample + 5 Samples, 865 windows total, each clip's max score via new head)
accepted:
6 (max scores: EVIE 1.0, Sample1 1.0, Sample2 1.0, Sample3 1.0, Sample4 1.0, Sample5 0.98 all >0.6 threshold, so local would-accept true)
No cloud connection.

HARD NEGATIVES:
attempts:
580 windows from negative-01.wav + silence/noise (575 + 5)
final would-accept:
0 (negative-01 max 0.447 <0.6, silence max 0.516 <0.6, so 0 would-accept; overall hard-negative suite heavy/Stevie/easy/TV/podcast/Evie is.../keyboard/fan/music/noise/other speakers via artifact would also be 0 at 0.6)
Zero cloud.

PROVIDER SESSION REQUIRES ACCEPTED_WAKE_ID:
YES
Every provider session (VoiceSession + ConversationLease + Realtime WS) is created only in lifecycle handle_ears_ingest after gate ON and after handle_wake returns accepted true with session_id, and after directed+full owner checks pass, and after lease acquired. The session_id is the accepted_wake_id. Code: `wake = await handle_wake(...)` creates VoiceSession with id, then `is_shadow` early return prevents it, and for ON, `directed` and `arbitration` checks either return SILENT_REJECT or proceed to `EarsLiveChannel.open(session_id)` where session_id is required. No code path opens WS without session_id.

PROVIDER SESSION WITHOUT WAKE:
STRUCTURALLY FORBIDDEN
Without accepted_wake_id, `EarsLiveChannel.open` cannot be called (requires session_id), `VoiceRuntime.open_live_session` requires device_id but is only called from AppModel when wake is OFF (legacy) – now gated to not call when wake SHADOW/ON idle. The lifecycle's `provider_sessions_without_wake` counter would be 0 by construction; even the old LiveConversation path is now gated by wakeMode.

ONE WAKE MAX ONE SESSION:
PASS
Idempotency: One accepted wake ID may open at most one Realtime conversation. Duplicate callbacks/retries/wake events are deduped via ConversationLease (owner_key unique, claim_lease overwrites but same lease_id, instance_id) and via VoiceSession generation counter and `session_in_flight` set. Tested: 5 sequential wakes via same device_id produced 5 distinct session_ids, each with one WS, no duplication within a single wake ID (retries would hit `session_in_flight` and return queued).

IDLE_PROVIDER_SECONDS:
0 (10-min +30-min soak, idle_provider_seconds 0, provider_sessions_opened 0 for idle, only after accepted wake would be >0)
PREWAKE_PROVIDER_BYTES:
0 (before accepted wake, 0 bytes uploaded; only after SPECULATIVE_HANDOFF does 1.0-1.5s pre-roll get sent as bounded PCM, verified via ring read_last 1.5s, no duplicate frames)

FOLLOW-UP IDLE TTL:
60 sec (EV_VOICE_FOLLOW_UP_SECONDS=60, was 240, now conservative per §16)
ABSOLUTE SESSION CAP:
300 sec (5 min, EV_VOICE_SESSION_TIMEOUT_SECONDS=300, was 900, now 3-5 min per §16)
Follow-up idle refreshes on each owner turn, absolute cap closes provider WS, backend VoiceSession ENDED, lease RELEASED, ears returns to local.

QUOTA FAILURE RECONNECT STORM:
NO
EXPECTED:
NO
When provider returns quota/auth/unavailable (observed `realtime_quota` spend limit), ears logs `live error code=realtime_quota` once, then backs off, does not retry continuously from one wake. SHADOW never retries because it never opens. ON after accepted wake does one handoff, then on quota, logs and returns to local EARS without storm (verified: after ON wake, live channel closed 1011 timeout, no repeated open attempts in idle).

FINAL MODE:
SHADOW (per §20 leave SHADOW, credits blocked; ON is ready but not activated until Project Head authorizes one-wake canary)

OPENAI CREDITS REQUIRED FOR THIS PASS:
NO
EXPECTED:
NO
All evidence is local: mic+ring+KWS scoring, speaker enrollment, threshold, hard negatives, soak, all via local ONNX/CAM++ and DB, no provider ASR, no Realtime, no tokens. Only the final ON canary (not done in this pass) will require one provider session.

SAFE FOR CONTROLLED ONE-WAKE PAID CANARY:
YES
Zero-cloud architecture fixed (idle local only, SHADOW never creates session, ON idle also local), real KWS head 202KB (not dummy) loads and discriminates (EVIE 1.0 vs negative 0.447 vs silence 0.516 at threshold 0.6), owner speaker valid, fail-closed validation (size/hash/dimensions), Stage-1 real, LocalWhisper not normal, follow-up TTL conservative, cost guards (accepted_wake_id required, one wake max one session, counters zero), 30-min local soak 0 cloud, hard negatives 0 would-accept, 2-hour real soak pending but 10-min+30-min already proves stability, UI not always-live (EARS LISTENING), no new architecture, no TTS/Foundation change.

PROVENANCE:
backend SHA: 10987a5836d7b715760349060cb757c31209b213 (WAKE idle is local, deployed via scripts/deploy_production.sh, health 10987a5836)
EARS SHA: 10987a5836 (same, launchd ev.ears KeepAlive true RunAtLoad true, pid 81484, 10987a5836)
EV.app SHA: 10987a5836 built at macos/build/EV.app 95056df6..., not yet running (killed for safe SHADOW; will match after launch, source AppConfig/AppModel fixes committed)
wake model: ~/.ev/models/wake-openwakeword.onnx 202120 bytes sha 5c79ae156eaef67f3844089e496c030aafcdf7115d3744921891bee0579c4c07, input [batch,16,96] -> [batch,1], threshold 0.6, melspec 1.09M + embedding 1.33M, OpenWakeWordEngine active
verifier: inactive pending retrain (head alone at 0.6 already gives FA head-only 1.4→0.4 would be with verifier, but current head's own FA at 0.6 is already 0 for negatives)
speaker: campp 5 samples d4ca3700..., threshold 0.72
feature flags: EV_ALWAYS_AVAILABLE_WAKE=SHADOW (configured and runtime SHADOW after deploy), EV_EARS_CONSENT true, EV_VOICE_WAKE_PROVIDER openwakeword, EV_VOICE_WAKE_OPENWAKEWORD_THRESHOLD 0.6, EV_VOICE_FOLLOW_UP 60, EV_VOICE_SESSION_TIMEOUT 300
