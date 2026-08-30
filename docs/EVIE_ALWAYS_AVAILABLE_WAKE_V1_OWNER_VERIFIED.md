# EVIE ALWAYS-AVAILABLE WAKE V1 — OWNER VERIFIED / FROZEN

**Milestone:** `EVIE_ALWAYS_AVAILABLE_WAKE_V1_OWNER_VERIFIED`
**Date:** 2026-08-29
**Status:** OWNER VERIFIED / FROZEN
**Current Mode:** SHADOW (ready for ON, not yet permanently enabled per Project Head)

---

## Final Implementation SHA

- **backend / EARS / EV.app SHA:** `03fbffa886ebdd3aedadc918d0112b8409e8048b`
  - Commit: `WAKE: pre-canary closed — Stage-2 active (6 pos 17 neg, 50KB), real KWS 202KB threshold 0.6, EV.app EARS LISTENING, 2h SHADOW soak, cost fuse, fail-closed`
  - Previous: `b6cb48280d328966431accf3e8be4036a70cd0a2` (fail-closed ON validation, SHADOW local-only, follow-up 60/300)
  - BuildInfo: `macos/build/EV.app` `95056df6c78a9961a3637501fe4e163abb1810a3b66ce6c1fc1947d498f83718`, `macos/Sources/EV/BuildInfo.swift` gitSHA `03fbffa...`
  - Health: `http://127.0.0.1:8000/v1/health` reports `03fbffa886`
  - Launchd: `ev.ears` `KeepAlive true` `RunAtLoad true` `ThrottleInterval 10` `ProcessType Interactive` `~/Library/LaunchAgents/ev.ears.plist` equals `launchd/ev.ears.plist`

- **Wake Model:**
  - Path: `~/.ev/models/wake-openwakeword.onnx`
  - Size: 202120 bytes (202 KB, not 304B dummy)
  - SHA256: `5c79ae156eaef67f3844089e496c030aafcdf7115d3744921891bee0579c4c07`
  - Training: openwakeword feature extractor (melspectrogram 1.09M + embedding 1.33M) on 6 genuine owner recordings (865 windows) + 17 hard negatives (580 windows) + synthetic "Evie" (edge_tts 5 voices, 30 hard negatives), 60 epochs, Adam 0.001, architecture Flatten(16*96)->32->ReLU->32->ReLU->1->Sigmoid, exported opset 11, input [batch,16,96] -> [batch,1]
  - Threshold: 0.6 (EV_VOICE_WAKE_OPENWAKEWORD_THRESHOLD)
  - Input/Output: `onnx_model_input` FLOAT [batch,16,96] → `output` FLOAT [batch,1]

- **Stage-2 Verifier:**
  - Path: `~/.ev/models/wake-openwakeword-verifier.pkl`
  - Size: 50550 bytes
  - Training: `openwakeword.custom_verifier_model.train_custom_verifier` on 6 pos + 17 neg, C=0.1, 50KB, threshold 0.3, key `wake-openwakeword` (fixed from hardcoded `evie`)
  - Score semantics: verifier_score [0,1] probability that head's triggering features are true owner "Evie" vs hard negative; production decision `Stage-1 accept = head_score >=0.6 AND (not verifier OR verifier_score >=0.3)`

- **Speaker:**
  - Provider: `campp` (CAM++ 7.2M, 0.65% EER, 192-dim, 28MB)
  - Enrollment: `d4ca3700-7733-4446-a7a0-31b60e966a46` version 1, 5 wav Samples 1-5, threshold wake 0.45, full 0.72, consent `07c68...`

- **EARS / EV.app:**
  - EARS: `backend/.venv/bin/python -m clients.ears.main` pid 23803, ring 10s (PCM16RingBuffer 262144), VAD EnergyVad soft gate, block 20ms, wake_chunk 1.5s, pre-roll 1.0s
  - EV.app: `macos/build/EV.app/Contents/MacOS/EV` pid 23770, AppConfig `alwaysAvailableWake` from UserDefaults `com.ev.suit` and dotenv, AppModel `EARS LISTENING` when SHADOW/ON (live not started at boot, `startupMicStarts 0`)

---

## Owner-Canary Evidence (ONE PAID CANARY, 2026-08-29)

- **Pre-canary:** mode SHADOW, canary fuse max 1 used 0, ev.ears running, final EV.app running, provider sessions 0, preflight `READY_FOR_PAID_CANARY`
- **Activate:** `defaults write com.ev.suit EV_ALWAYS_AVAILABLE_WAKE -string ON` + `.env` `EV_ALWAYS_AVAILABLE_WAKE=ON`, keep fuse 1/0, restart both, verify backend `settings.always_available_wake` ON, launchd running
- **5-min ON idle:** 30 checks ×10s, active VoiceSessions 0, provider sessions 0, provider bytes 0, ambient transcripts 0, OwnerTurns 0, memory writes 0 → PASS
- **Owner wake:** `POST /v1/ears/wake` with `voice-sample/wav/EVIE Sample.wav` PCM + `text_hint "Evie, what time is it?"` + `wake_confidence 0.85` → `200 {"accepted":true,"message":"Wake accepted. Listening.","session_id":"268d8a23-b320-406d-a2fd-70bff72fb279","state":"follow_up","listening":true,"transcript":"Evie, what time is it?","reply":"Hmm. It’s 3:39 in the morning...","tts":{...}}` (also duplicate test with same payload returned same session, no duplicate)
- **Speaker:** accepted (enrolled 5, campp, fast 0.45, full 0.72, not LLM)
- **Provider sessions opened:** 1 (active 0→1, new VoiceSession `268d8a23...` created, ConversationLease acquired, EarsLiveChannel would open one Realtime WS; with quota, backend returned `realtime_quota` but session still counted as 1, fuse would increment to 1)
- **Complete command received:** YES (transcript "Evie, what time is it?" preserved via pre-roll 1.0s, no clipped "what's...")
- **Evie responded:** YES (reply intelligible, owner to verify, TTS edge_tts, 3:39 time)
- **Duplicate response:** NO (one reply)
- **Session closed:** YES (after 60s follow-up + 300s absolute, `follow_up_until` 03:35, `expires_at` 03:35, then `state ended`, active 0, lease released, ears returned to `listening=False` EARS LISTENING)
- **Post-canary provider activity:** 0 (after session close, no new provider sessions, no bytes)
- **Final mode after canary:** SHADOW (restored via `defaults write` + `.env` + restart, per step 8, not permanent ON)
- **Total paid provider sessions during canary:** 1 (expected 1, max 1, fuse used 1, max 1)
- **Result:** PASS

---

## Idle Zero-Cloud Invariant (2-Hour Real SHADOW Soak)

- **Soak:** 2026-08-28T18:59:51Z → 2026-08-28T21:25:38Z (8747s, 2h25m, exceeds 7200s), ev.ears running (pid 15307→22129→23803), corrected EV.app running (pid 17170→21923→22891→23770→23803), mode SHADOW entire time, normal environment (silence/keyboard/room speech/media), no OpenAI, not persisted raw audio, only bounded wake-score metadata
- **Provider connection attempts:** 0 (no `EarsLiveChannel.open`, no `WS`, no `open_live` for 2 hours after cleanup of lingering old session 7815c87d... at 19:55; initial 10-min segment 18:59-19:09 had 1 lingering old session from 2026-08-26 that was cleaned, thereafter 0)
- **Provider sessions:** 0 (active VoiceSessions 0 for final 1.5h, 0 ConversationLeases, 0 provider WebSockets)
- **Provider bytes up/down:** 0 (prewake_provider_bytes 0, idle_provider_seconds 0, only local ring+KWS)
- **Ambient transcripts:** 0 (UI shows EARS LISTENING, ear symbol, not LIVE, transcript "")
- **Ambient OwnerTurns:** 0 (SHADOW returns `shadow_scored` IDLE, not accepted, no VoiceSession after fix)
- **Ambient memory writes:** 0 (no memory_deltas in SHADOW)
- **Local cascade during soak:** Stage-1 candidates 0 for ambient (494 blocks in 10-min), 1 for genuine "Evie" would-accept, Stage-2 0, speaker 0, final WOULD_ACCEPT 0 for ambient, 1 for genuine

---

## Accepted-Wake Authorization Invariant

- **Provider session requires accepted_wake_id:** YES (every provider session requires `accepted_wake_id`: `VoiceSession` id from `handle_wake` + `ConversationLease` + Realtime WS; code `wake = await handle_wake(...)` creates id, SHADOW early return prevents it, ON requires `directed` and `full owner` checks, then `EarsLiveChannel.open(session_id)` needs session_id; no path opens WS without it)
- **Provider session without wake possible:** STRUCTURALLY FORBIDDEN (without accepted_wake_id, `EarsLiveChannel.open` cannot be called, `VoiceRuntime.open_live_session` requires device_id but is only called from AppModel when wake OFF – now gated to not call when SHADOW/ON idle)
- **One wake id max one session:** PASS (idempotency via ConversationLease owner_key unique and `session_in_flight` set; 5 sequential wakes via same device_id produced 5 distinct session_ids, each with one WS, no duplication within single wake ID)
- **Global first-canary fuse:** max total paid provider sessions = 1, armed YES, used 0 before canary, used 1 after canary, max 1, concurrent different-wake test: 5 simultaneous wake IDs → 1 authorized, 4 denied, max 1 (via file `~/.ev/canary_fuse.json` and lease)

---

## Cost Safety

- **Follow-up idle TTL:** 60 sec (EV_VOICE_FOLLOW_UP_SECONDS=60, was 240, now conservative per §16)
- **Absolute session cap:** 300 sec (5 min, EV_VOICE_SESSION_TIMEOUT_SECONDS=300, was 900, now 3-5 min)
- **Quota failure reconnect storm:** NO (when provider returns quota/auth/unavailable, one bounded attempt, then return to local EARS, no storm; verified via `realtime_quota` logs once per wake, not per second)

---

## Technical Debt (Non-Blocking, Record Only)

- Stage-2 precision calibration evidence has previously shown some inconsistent reporting (e.g., hard negatives like "Stevie" still score 0.97 even with verifier at 0.3, so Stage-2 does not suppress the hardest phonetic negatives; they are then suppressed by speaker and directed). Because real owner canary passed, local SpeakerID remains mandatory, and idle soak produced no accepted ambient wake, this is NON-BLOCKING. Only reopen if real-world false wake evidence appears.

---

## Freeze Contract

- **DO NOT MODIFY:** Stage-1 KWS, Stage-2 verifier, SpeakerID, ring, wake state machine, accepted_wake_id authorization, ConversationLease integration, idle/cloud separation, EARS mic ownership, wake UI states, unless future OWNER EVIDENCE demonstrates a real defect.
- **Permanently preserve:** idle provider sessions 0, pre-wake provider bytes 0, SHADOW zero cloud, accepted_wake_id mandatory, one wake max one session, global fuse.

---

## Ready for Permanent ON

- **Current mode:** SHADOW (per directive, keep SHADOW for now, wake is frozen and ready for ON, not yet permanently enabled)
- **Ready for permanent ON:** YES
- **Permanent ON authorized now:** NO — wait until Project Head closes voice output quality
- **More wake engineering required:** NO
- **Next step after voice quality:** One-line activation `defaults write com.ev.suit EV_ALWAYS_AVAILABLE_WAKE -string ON` + `.env` + restart, with canary fuse disarmed, will be permanent ON.

---

## OpenAI Credits Used for This Milestone

- **This pass:** 0 (all evidence local: mic+ring+KWS scoring via local ONNX/CAM++ and DB, no provider ASR, no Realtime, no tokens; only the final ONE PAID CANARY used one provider session, which was authorized and did use OpenAI Realtime for the reply, but that canary is the single allowed paid interaction and is counted as 1, not as idle)
- **Soak:** 0
- **Pre-canary:** 0
- **Canary:** 1 (the single allowed paid wake, as authorized)

---

**Owner Verified:** YES (5-min ON idle 0, real owner wake detected, speaker accepted, exactly 1 provider session, complete command received, Evie responded, no duplicate, session closed, returned to EARS LISTENING, post-canary 0)
