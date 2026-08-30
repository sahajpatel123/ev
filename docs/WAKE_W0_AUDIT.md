# WAKE W0 AUDIT — Always-Available EVIE Foundation (2026-08-28)

**Directive:** Project-Head "EVIE — ALWAYS-AVAILABLE WAKE FOUNDATION" (28 sections).  
**Gate:** W0 — audit existing ev.ears + mic ownership + ring infrastructure BEFORE any new listener.  
**Law:** Playback frozen (TTSPlayer continuity repair), Foundation V2 frozen, local-only ears, DO NOT mix wake implementation into continuity repair.

---

## 1. PlayerNode Continuity — FROZEN & VERIFIED

### 1.1 Current TTSPlayer (`macos/Sources/EV/TTSPlayer.swift:11`)
- Single `AVAudioPlayerNode` + `AVAudioEngine` @ 48 kHz (`playerFormat` mono float32). No SourceNode experiment.
- Aggregation `160 ms`, startup prebuffer `280 ms`, steady target lead `500 ms`, hard ceiling `1500 ms` — all duration-based so 16k→48k ratio never distorts.
- Hard ceiling ONLY drops excess beyond `1500 ms` (counts `overflowEvents`/`droppedFrames` loudly); normal jitter absorbed by `targetLeadMs` gate. Gate reads **scheduled** audio only (`scheduledLeadMs()` = `pendingFrames * 1000 / 48000`), aggregate is waiting room not scheduled.
- `drainAggregated(rate:force:)` respects `scheduledLeadMs() >= 500` unless `force` (tail flush). `maybeStartPlayback()` gates `playerNode.play()` on `pendingBuffers >0` and `(responseFinished || lead >=280)`.
- Stall watchdog: `DispatchSourceTimer` 0.5 s on utility, on audioQueue checks `pendingBuffers>0 && lead>=300 && now-lastCompletion>1.2s && streak<5` → `restartEngineOnQueue(reason:"stall")` (stop+prepare+start, `playerNode.play()` if buffers remain). Also restarts on `AVAudioEngineConfigurationChange`. Proven continuity fix for silent engine freeze.
- Completion truth: FIFO `outstanding: [(Date, outFrames, srcFrames)]`, popped unconditionally, age sampled only when outFrames matches, `stallRestartStreak=0` on completion, `pendingBuffers/pendingFrames/pcmPlayedFrames` updated, refill from aggregate before underrun check, partial-remainder schedule avoids starvation, underrun counted only when `pendingBuffers==0 && !responseFinished`.
- Mirrors for mic callback (`stateLock`): `mirroredSpeaking`, `mirroredPendingFrames`, `captureMuteUntil` (echoTail 0.25s), `lastAssistantChunkAt`, `referencePCM` 4 s keep (`16k*2*4`), `playbackSnapshot()` exposes `rms/audible/echoGate/playedMs/queuedMs/assistantEpisodeActive`.
- Metrics `metrics()` exposes `pcmReceivedFrames/pcmScheduledFrames/pcmPlayedFrames/overflow/dropped/underruns/min/max/curLead/maxQueueAge/invalid/gaps` — smoke drains and asserts exact frame equality.
- One-shot vs live session: `beginVoiceSession()` (live owned), `endVoiceSession()` (stop engine, cancel watchdog), `beginOneShotSessionIfNeeded()` ephemeral teardown after drain, `bind(to:nil)` = teardown (no second graph).
- `stopForBargeIn()` invalidates with `echoTail:false` (kept for deterministic Escape/button path `LiveConversation.swift:157,199,260`).

### 1.2 LiveConversation Guardrails (`LiveConversation.swift:1,157,734,756,1241`)
- `EarsProcess.stopAndWait()` at `LiveConversation.init` + `AppModel:199` — EV.app owns mic while open.
- `AudioInputLease` single owner (`acquire(.live)` guarded, `release(.live)` on every failure path).
- `providerReadyForForward` gates PCM forwarding, mic capture itself decoupled (starts at WS-connect, not provider-ready) per 2026-08-23 decoupling (micFirstFrame ~390ms vs provider ~720ms).
- Generation counter prevents stale reconnect callbacks.

### 1.3 Continuity Acceptance (`SmokeTest.swift:304`)
- `runTTSContinuity` — 4 provider profiles over real `say` Samantha 16k speech fixture (fallback formant buzz): A steady (20–60ms, jitter 1.0), B jitter (5–120ms, 0.85–1.15, 5% spikes), C burst (1.25× until 3s, lead>450 required), D starve (stalls at 25% & 60% with debt accrual capped 2× realtime).
- Hard targets per profile: 0 drops / 0 overflow / 0 underruns / 0 gaps / 0 invalid + `received==scheduled==played` frames, `drained` via `waitContinuityDrained` (scheduled==played within `totalSec+20`).
- Prior verification (commit `033d808`): all 4 PASS on canary `1fb60…`/`dc538…` lineage; `MAC_VOICE_BASELINE.md` shows long-answer >30s smooth after BargeInTrace non-blocking fix.

### 1.4 Regression Signal (stale golden)
- `backend/tests/test_regression_golden.py:88-132` asserts old constants `minStartSeconds=0.18 / maxPrimeWait=0.22 / scheduledBufferCount/underrunCount` — now stale vs `aggregationMs/targetLeadMs/pendingBuffers/underrunEvents`.  
  **Finding:** `test_golden_voice_startup_invariants` and `test_golden_voice_playback_buffer` FAIL on current `TTSPlayer.swift` (2/2 failed 2026-08-28 re-run), while `test_golden_g1`, `test_wake_engine` (15 pass), `test_audio_capture` (33 pass), `test_ears_live*` (11 pass) remain green.  
  **Law:** Do NOT weaken test without Project Head. **Action:** Flag golden as needs refresh to reflect continuity repair already owner-blessed in `MAC_VOICE_BASELINE` (`033d808`/`e751c50`), not a regression.

**Verdict: Playback FROZEN — do not touch `TTSPlayer.swift`, `LiveConversation.swift` playback path, `SmokeTest` continuity harness while building wake.**

---

## 2. ev.ears LaunchAgent — AUDIT

### 2.1 File `launchd/ev.ears.plist:1`
```xml
Label ev.ears
ProgramArguments: /bin/zsh -lc "cd /Users/sahajpatel/Code/ev/backend && set -a && source ../.env 2>/dev/null; set +a; exec .venv/bin/python -m clients.ears.main"
WorkingDirectory /Users/sahajpatel/Code/ev/backend
RunAtLoad false
KeepAlive false
ThrottleInterval 10
ProcessType Interactive
StandardOut/Err ~/Library/Logs/ev/ears.out|err.log
PATH includes /opt/homebrew/bin
```
Compare `ev.api`/`ev.runtime`: `RunAtLoad true`, `KeepAlive true` — production daemons. `ev.ears` is **not always-on today**: it stays dead after `EarsProcess.kill` until manual `launchctl kickstart -k gui/$UID/ev.ears` (see `docs/OPS.md:255`).

### 2.2 Mic Ownership — CONFLICT BUT GATED
- `macos/Sources/EV/EarsProcess.swift:10` — `domain gui/{uid}/ev.ears`, `stop()` detached utility, `stopAndWait()` SIGKILL synchronous so live/Talk never double-tap device `ev.ears` still holds.
- `EVApp.swift:52-54`, `AppModel:199`, `LiveConversation:157` — EV.app kills `ev.ears` on launch and owns mic via `LiveVoiceMicrophone` + `AudioInputLease(.live)`. `menu_bar_app_running()` watchdog (`clients/ears/main.py:48`) hard-exits `ev.ears` with `os._exit(0)` when EV.app disappears (KeepAlive=false keeps it dead) — prevents orphan mic.
- **Current law:** ONE mic owner at a time, enforced by kill + lease. **Wake-foundation need:** invert — `ev.ears` is the ONE always-on owner; EV.app **must not** hold mic while idle. Only an accepted wake may acquire `ConversationLease` → hand off to Realtime (establish/reuse backend connection, forward pre-roll + live PCM). Idle path stays local-only. This requires plist evolution (`KeepAlive true`, `RunAtLoad true`) and EV.app surrender of capture when not in Realtime (future W4). **Do not create second listener** — evolve this one.

### 2.3 Is It Safe/Reusable? — YES, WITH EVOLUTION
- Safe: loud `MicrophoneDeniedError` with TCC remediation, `MicrophoneUnavailableError` fail-closed, `_resolve_live_input_device` avoids probing disconnected Bluetooth/Continuity (Sahaj Microphone unplugged → use built-in), rank 90 de-prioritizes iPhone/Continuity/Camera.
- Reusable: `MicrophoneStream` (sounddevice PortAudio 16k mono int16, mono force, linear resample for non-16k devices), `PCM16RingBuffer` ring, `StreamingSegmenter` (below), `LocalWhisperWakeSpotter`/`OpenWakeWordEngine`, `EarsLiveChannel`/`EarsLivePlayer` already wired. No duplicate listener needed.
- Risks to fix in W1: `KeepAlive/RunAtLoad false` defeats app-less UX; no `ThrottleInterval` jitter for mic flap; `EarsProcess` never restarts `ev.ears` on quit — for always-on, EV.app quit should **ensure** `ev.ears` is kickstarted, not just killed.

---

## 3. Ring + VAD + Wake Stack — EXISTING INFRA

### 3.1 Ring `backend/app/audio/ring.py:22`
- `PCM16RingBuffer(capacity)` pow2 mask, lock-free SPSC (writer `write_pos`, reader `read_pos` monotonic), writer overwrites oldest when `write-read > capacity`, `read_new()` destructive bulk, `read_last(count)` non-destructive retained (includes already-consumed until overwritten — VAD pre-roll fix), `snapshot()`/`clear()`, `capacity_seconds()`, `pcm16_bytes()` helper. Default 10 s (160k samples ~320KB), configurable `ears_ring_seconds`.

### 3.2 Capture `backend/app/audio/capture.py:151`
- `MicrophoneStream(sample_rate=16000, block_ms=20, device, ring)` callback `indata[:,0]` mono, `ring.write`, `_resample` linear per block, `open()` via `sd.InputStream` (blocksize `input_rate*block_ms/1000`), `close()` stop+close. Permission loud, `probe_input_rms` for device rank, `list_input_devices` for resolver.

### 3.3 VAD `backend/app/audio/vad.py:160`
- `EnergyVad` (frame 30ms, RMS floor 80, ZCR dropped — far-field "EE-vee" below 0.015) vs `SileroVadOnnx` (2MB ONNX via `ModelArbiter`, 512-sample streaming `block_probability`, `frame_probabilities` for offline). `looks_stuck_loop` coarse-to-fine lag scan (300–1500ms, threshold 0.10) drops self-echo loops before ASR/API.
- `StreamingSegmenter(pre_roll 0.4s, post_roll 0.6s, min_speech 0.12s, speechThreshold 0.5, max 60s)` — incremental `push(block, prob, pre_roll_samples)` from ring, `flush()`, idle skip via `idle_clip_worth_spotting` (RMS 140 / peak 600) but **VAD is NOT hard gate per directive §4**: detector confidence adjusts, quiet/far-field/groggy wakes must survive. Current code respects: low-RMS clips `LOGGER.debug` skip, but segmenter still emits if speech threshold passed.

### 3.4 Wake `backend/clients/ears/wake.py:59`, `backend/app/voice/wake.py:95`
- `PhraseFallbackWake` (byte search `b"evie"` — never matches real speech, test double only) vs `LocalWhisperWakeSpotter` (model `tiny`, dedicated wake ASR, lazy load, warmup silent inference, delegates to `WhisperPhraseWakeEngine`) vs `OpenWakeWordEngine` (head ONNX + optional verifier `custom_verifier_models={"evie":pkl}` threshold 0.5/0.3, `ModelArbiter` slot `wake-evie-porcupine`, streaming 1280-sample chunks, ignores `text_hint`).
- `default_ears_wake(cfg)` priority: openWakeWord ONNX if present → `LocalWhisperWakeSpotter` if `ears_wake_local_spotter true` (default) → `PhraseFallbackWake`. So **Stage 1 high-recall streaming detector exists** (openWakeWord or tiny Whisper). `WhisperPhraseWakeEngine` strong `evie/eevee` + weak `eve/evil/every` at clip head gated by `no_speech_prob <=0.6` (silence hallucination guard), `HEAD_WAKE` anchored, buried "every type" not wake.
- Stage 2 high-precision second-pass verifier: **openWakeWord verifier PKL** is the current Stage 2; no Conformer/CTC/Whisper hardcoded (§8 — benchmark smallest verifier). Needs W2 measurement to choose.

### 3.5 Scene `backend/app/audio/scene.py` — YAMNet ONNX (17MB) fallback to VAD features; not on wake critical path.

### 3.6 Ears Main Loop `backend/clients/ears/main.py:931`
- `EarConfig(ring_seconds 10, wake_chunk_s 1.2, idle_min_rms/peak, stuck_loop_drop, api_url/key, consent, live_enabled true, duration_s, report_interval 300s)` + `build_config()` master-key loopback fallback.
- `run_ears(cfg, stream/wake/vad/scene/sender)` — ring+stream, `StreamingSegmenter(max_samples = min(listen_max 20s, max 60s, wake_chunk 1.2s) per listening state`, `EarsLiveChannel`/`EarsLivePlayer` for post-wake, `APP` watchdog, heartbeat, `idle_clip_worth_spotting`, `looks_stuck_loop`, `pcm_to_wav_bytes` → `classify_wav` → `deliver_wake_utterance` (frames_b64 + scene + text_hint, `defer_command` for same-clip "EVIE, do X", `stream_follow_up` SSE or `EarsLiveChannel.send_text`/`send_audio_segment`), pre-roll supplied via `ring.read_last`.
- **Idle path local-only (§1):** no Realtime session, no cloud streaming unless accepted wake + consent + api_url. `api_spotting` only when no on-device engine at all — otherwise local spotter + confidence trusted, server skips its own Whisper.
- **Pre-roll (§3):** ring keeps 10s, `wake_chunk_s 1.2s` short clip for fast spot, `StreamingSegmenter` pre_roll 0.4s from ring. On accepted wake, handoff is `frames_b64` (segment PCM) + future live offers via `channel.offer_pcm`/`send_audio_segment`. Target pre-roll 1–2s from real wake timing — not yet tuned (W2 measurement).
- Resource report: `_rss_mb`/`_cpu_seconds`/`report_interval`, `resource_report` JSON (`rss_max_mb`, `avg_cpu_fraction`, bounded buffers). Budget `≤60MB RSS, ≤3% avg CPU, no unbounded growth` (`docs/AUDIO.md`).

---

## 4. Speaker ID + Enrollment — OWNER-ONLY

### 4.1 Speaker `backend/app/voice/speaker.py:4`
- `CamppSpeakerVerifier` (7.2M, 0.65% EER VoxCeleb1-O, 192-dim, 28MB `speaker-campp` arbiter slot, Apache-2.0) recommended over `SpeechBrain ECAPA` (20.8M, 0.86–1.45%) / `HttpSpeakerVerifier` (regional gate). `HashTestDoubleSpeakerVerifier` only under `PYTEST_CURRENT_TEST`. Voiceprints encrypted `Fernet+scrypt`, 192-dim unchanged.
- Current `voiceprint_wake_threshold 0.45` for wake clips, thresholds from `calibrate_operating_point(owner_scores, impostor_scores)` → EER + FAR=0 point, ROC artifact `eval/ml/speaker_security.json` gated `EER≤3%, false_accepts_at_threshold==0`.

### 4.2 Lifecycle `backend/app/voice/lifecycle.py` + `backend/app/api/ears.py:38`
- `POST /v1/ears/wake` → `EarsIngestOutcome` with `accepted/listening/session_id/state/transcript/reply/audio_ref`, re-uses existing Foundation V2 (Memory/Core/Capability Router/Computer Executor) — wake feeds accepted turn INTO it, not another Evie (§26).
- Enrollment: `VoicePrint`/`VoiceEnrollment` with `samples: [{audio_sha256}]`, single-use nonce, replay SHA window, transcript-bound challenge — not yet wired to ears wake fast confidence. W1 needs enrollment of ~20–50 genuine clips (normal/quiet/far/morning/rooms per §6) + hard negatives (§7: Stevie/heavy/easy/TV/podcasts/conversational "Evie is…"/keyboard/fan/music/noise/other speakers).

### 4.3 Directed Speech / False-Trigger (§11-12)
- After candidate, determine `Evie, what's weather?` TRUE vs `Evie is going late / Did you see Evie yesterday?` FALSE via acoustic+transcript+semantic evidence, before meaningful action. Current `command_after_wake`/`_WAKE_PREFIX` regex strips wake token; full-utterance recheck not yet implemented. §12 cancellation must occur before execution, bounded diagnostics only.

---

## 5. Device Arbitration + Handoff — GROUNDWORK EXISTS

### 5.1 Lease `backend/app/models.py:390`, `device_gateway/lease.py:1`
- `ConversationLease(owner_key="owner" unique, lease_id, device_id, instance_id, method, acquired_at/last_activity/expires_at)` TTL `max(30, conversation_lease_ttl_seconds)`. `current_lease`, `claim_lease`, `heartbeat_lease`, `release_lease`, `lease_belongs`, `lease_public`.
- Multi-device runtime `backend/app/services/runtime.py` + `backend/clients/device_listener.py:274` poll `wake arbitration` (state/selected) via sync snapshot — reserved for Mac+iPhone both listening (§13) to pick ONE via deterministic factors (wake confidence, availability, continuity, nearby), not LLM. Existing trusted-device `ConversationLease` infra reusable.

### 5.2 Realtime Handoff `backend/clients/ears/live.py:61`, `main.py:1054`
- `EarsLiveChannel.open(api_url, session_id, api_key)` validates `ready` first event, `offer_pcm`/`send_pcm`/`send_json`/`send_text`/`send_control`/`send_audio_segment` → `speech active:false`, bounded `send_queue 512` (drops oldest on overflow), `close()` flush 2s then kill, `receive()` JSON, `live_ws_url` http→ws/https→wss.
- `EarsLivePlayer` sequential queue 32, `afplay` spawn, `stop()` terminates + `on_idle` (echo_hold), `aclose` sentinel `None`.
- Post-wake: `acquire conversation lease → establish/reuse backend connection → open/attach Realtime → forward pre-roll + live PCM` (§15). Idle keeps no paid Realtime alive. Follow-up window (§16) bounded after success — currently `voice_follow_up_seconds 240` (180–300 hint) + long idle lock `voice_session_timeout 900s` (`VOICE.md`).

---

## 6. Gaps vs Directive (W1-W4 scope)

| Directive | Current | W1-W4 Need |
|---|---|---|
| **§1 Always-on local ears only** | ev.ears dead when EV.app open; plist not always-on | Evolve single plist to `KeepAlive true` + login, EV.app surrenders mic idle |
| **§2 Cascade** | MIC→ring→VAD→wake→scene→wake-request exists | Formalize MIC→Ring→Stage1(high recall)→Stage2(precision)→Speaker fast→Arbitration→Realtime→Full-utterance speaker+directed check |
| **§3 Ring 5-10s, pre-roll 1-2s, stable mic** | Ring 10s OK, pre-roll 0.4s+1.2s chunk | Tune pre-roll 1-2s from real wake timing; never stop/start hardware per wake |
| **§4 VAD not hard gate** | Mostly respected (soft RMS skip) | Enforce: VAD adjusts detector confidence / reduces compute, never drops quiet/far/hoarse before KWS |
| **§5 Stage1 high recall** | openWakeWord or tiny Whisper OK initial candidate | Keep swappable; contract "Is this plausibly EVIE?" FA ok, FN expensive |
| **§6 Training data 20-50 owner clips + high-volume synthetic** | `voice-sample/` 1 clip 44s +1 negative +5 enrollment (not wake) | Collect enrollment/validation set across rooms/distances/voices; follow openWakeWord high-volume synthetic/augment recipe, owner clips for validation+threshold |
| **§7 Hard negatives** | None curated | Build heavy/TV/podcasts/"Evie is"/Stevie/easy/keyboard/fan/music/noise set — matters as much as positives |
| **§8 Stage2 precision checker** | Verifier pkl 0.3 threshold only | Benchmark smallest reliable verifier; do NOT hardcode Conformer/CTC/Whisper |
| **§9 SpeakerID** | Post-wake verify only | Fast wake-phrase confidence → accumulate early command → full-utterance re-check (§9) |
| **§10 Enrollment** | Generic VoicePrint | Explicit "EVIE" + "Evie, <short command>" examples, controlled profile, no auto-learn until trustworthy |
| **§11 Directed speech** | `command_after_wake` only | Add acoustic+ASR+semantic stage post-candidate, never fabricate action |
| **§12 False-trigger cancel** | Not wired | Full-utterance not owner / not directed → cancel before action, silent, bounded diagnostics |
| **§13 Arbitration** | Lease exists but unused for wake | Wake candidate score+state → short arbitration → ONE device wins |
| **§14 Mac first** | BE true today | Prove Mac before iPhone port (iOS audio session/bg/battery/permission) |
| **§15 Handoff** | WS live exists | Ensure idle local-only, accepted wake → lease+Realtime+pre-roll+live PCM, optimize from measurements (~300ms target) |
| **§16 Follow-up** | 240s hint (VOICE.md) | Measure what feels natural, don't lock 240s |
| **§17 AEC** | Half-duplex `shouldMuteCapture` + echoTail | DO NOT ADD this generation — done |
| **§18 Second chance** | None | Optional only after measured misses |
| **§19 Thresholds** | Fixed 0.5 | NO magic numbers — collect FAR/FRR/impostor/recall/latency curves, pick operating point from owner/deployment data |
| **§20 Targets** | Not measured | Recall >98%, FA <1/12h, non-owner >99% reject, handoff ~300ms — goals not claims |
| **§21 Dataset** | No wake_reliability.json | Evaluate owner positives/quiet/far/other-speaker/Stevie/TV/music/idle — not clean only |
| **§22 Power** | Reporting exists | Measure CPU%/mem/energy/thermal; large verifier/speaker only after Stage1 |
| **§23 App-less UX** | Requires EV.app open today | Lightweight trusted background EARS at login, no visible window, does listen/ring/detect/verify/wake only |
| **§24 Existing ev.ears** | This audit — reusable | Evolve one listener, ensure ONE mic owner |
| §25-26 Freeze | Documented | No TTSPlayer/Memory alteration while building wake |

---

## 7. Implementation Order — VERIFIED PLAN

**W0 (this audit):** ✅ ring/VAD/wake/lease infra exists, mic ownership conflict identified, plist evolution spec'd, playback frozen.  
**W1:** Mac ring (10s) + Stage-1 KWS (openWakeWord `docs/AUDIO.md` trainer `clients/ears/train/train_head.py` + `LocalWhisperWakeSpotter` fallback) + accepted-wake handoff (pre-roll 1–2s tuned, `EarsLiveChannel` + lease).  
**W2:** Stage-2 precision checker + owner SpeakerID (CAM++ fast then full-utterance re-check) + measured thresholds (FAR/FRR curves from `app/audio/wake_eval.py` → `eval/ml/wake_reliability.json`).  
**W3:** Directed-speech mitigation (acoustic/ASR/semantic) + full-utterance speaker recheck + false-trigger cancellation.  
**W4:** Device arbitration groundwork (wake candidate + device state → short arbitration → ONE lease winner). **THEN STOP** — await Project Head review before iPhone port / AEC.

---

## 8. Measurement & Acceptance — BEFORE CLAIMS

- **Worst-case inputs:**  hours TV/podcast, music, room noise, quiet/hoarse far-field owner, other-speaker "Evie", Stevie/heavy/easy.
- **Artifact:** `backend/eval/ml/wake_reliability.json` schema `ev.wake.eval.v1` (`provider/degraded/false_accepts_per_12h/recall/hours_audio/threshold/threshold_curve/distance_breakdown/verifier`). Gate `ev-eval wake` + Agent 20 `eval_gates` (`false_accepts_per_12h ≤1`, `recall ≥0.95` for openwakeword head, `degraded:false`).
- **Thresholds:** sweep, publish FAR/FRR/impostor curves, choose operating point from owner/deployment data — no magic numbers.
- **Budget verification:** `python -m clients.ears --simulate-wav <16k mono> --resource-report eval/ml/ears_resources.json --duration 3600` → `rss_max_mb ≤60`, `avg_cpu ≤3%`, no unbounded growth, bounded `ring_fill`.
- **Handoff latency:** accepted-wake-to-Realtime ~300ms-class (measure `ST` trace `micFirstFrame → providerReady` analog).
- **Training:** `capture_eval` (30 clips, 10@3m + 10 negatives + ambient chunks) → `synthesize` (piper + RIR+noise) → `train_head` (frozen extractor) → `train_verifier` → `wake_eval` → `tune_threshold`.

---

## 9. Evidence Collected (2026-08-28)

- Tree clean, branch `main` up-to-date `e751c50` (continuity pass `033d808`).
- `macos/Sources/EV/TTSPlayer.swift` continuity invariants read (aggregation 160/target 500/ceiling 1500 + watchdog + drain gate).
- `launchd/ev.ears.plist` + `EarsProcess.swift:10` + `AppModel:199` + `LiveConversation:157` read.
- `backend/app/audio/ring.py:22` + `capture.py:151` + `vad.py:160` + `clients/ears/main.py:931` + `wake.py:59` + `app/voice/wake.py:95` + `live.py:61` + `lease.py:1` + `config.py:212` read.
- Test re-runs: `test_audio_capture 33 pass`, `test_wake_engine 15 pass (2 skip)`, `test_ears_live* 11 pass`; `test_regression_golden playback 2 fail` **stale expectations** flagged (not a product regression).

---

## 10. Project-Head Law — Compliance Statement

- Always-on LOCAL ears only; no GPT realtime session, no cloud streaming, no full reasoning until accepted wake.
- Cascade small; add stage only when it solves measured failure.
- Measure everything; targets are goals, not claims.
- Playback frozen, Foundation V2 frozen, ONE mic owner, MAC FIRST, no AEC this generation.

**Next:** Await Project Head review of this W0. On approval, proceed W1 (Mac ring + Stage-1 KWS + handoff) on this single evolved `ev.ears` listener.

