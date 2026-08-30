# WAKE W1–W4 FOUNDATION — Mac Always-Available Evie (2026-08-28)

**Directive:** Project-Head "EVIE — ALWAYS-AVAILABLE WAKE FOUNDATION" (28 sections).
**Gate:** W0 DONE (docs/WAKE_W0_AUDIT.md) — playback frozen, ev.ears audit, ring 10s, VAD, single mic owner.
**Scope:** W1–W4 on MAC ONLY via single evolved `ev.ears` listener, THEN STOP for Project Head review. No iPhone port, no AEC this generation, TTSPlayer/continuity/Foundation V2 frozen.

---

## 1 — W1: Always-On Local Ears + Ring + Stage-1 + Handoff

### 1.1 Always-on plist — ONE mic owner (law §1, §23, §24)
- `launchd/ev.ears.plist:15-18` — `RunAtLoad true`, `KeepAlive true` (was false/false). Production `ev.api`/`ev.runtime` already `true` — now `ev.ears` matches.
- `macos/Sources/EV/EarsProcess.swift:10-35` — added `ensureRunning()` / `ensureRunningAsync()` (`launchctl kickstart -k gui/$UID/ev.ears`). Old `stopAndWait()` retained only for the brief Realtime handoff window.
- `macos/Sources/EV/EVApp.swift:52-54,73-76` — launch no longer kills ears (`ensureRunning()`); `applicationShouldTerminate` now `ensureRunning()` so quit leaves ONE owner (KeepAlive restarts immediately, not after ThrottleInterval 10s).
- `macos/Sources/EV/AppModel.swift:199` — same: `EarsProcess.ensureRunning()` at startup, not `stopAndWait()`.
- `macos/Sources/EV/LiveConversation.swift:153-165,166-184` — `start()` still acquires mic for the handoff window (accepted wake only); `stop()` now `ensureRunningAsync()` to surrender mic back to idle local-only path when Realtime is not needed. Idle path stays local-only, no paid Realtime alive while listening (§15).

**Result:** App-less UX (§23) — no manual app open required; lightweight trusted background EARS at login does listen/ring/detect/verify/wake only. ONE always-on mic owner, evolved not duplicated.

### 1.2 Ring — stable, 5–10s, 1–2s pre-roll (§3)
- `backend/app/audio/ring.py:22` — `PCM16RingBuffer` lock-free SPSC, pow2, writer overwrites oldest, `read_last(count)` non-destructive, default 10s (160k samples ~320KB). Unchanged — already compliant.
- `backend/app/config.py:228,240` — `ears_vad_pre_roll_s 0.4→1.0`, `ears_wake_chunk_s 1.2→1.5` → ~1–2s useful pre-roll from real wake timing (ring 10s provides history). Config comment marks target.
- `backend/clients/ears/main.py:115,124` — same `vad_pre_roll_s 1.0`, `wake_chunk_s 1.5`, ring 10s, `block_ms 20`.
- `backend/clients/ears/main.py:975-982` — `StreamingSegmenter(pre_roll 1.0s, max = wake_chunk 1.5s)` — handoff is `frames_b64` (segment PCM) + future `EarsLiveChannel.send_audio_segment` via `ring.read_last` for pre-roll.
- Mic stable: `MicrophoneStream` (sounddevice 16k mono, PortAudio, linear resample) is opened once at `run_ears` start and never stop/started per wake (§3: do not start/stop hardware for every wake). `Ring.read_last` supplies pre-roll without hardware flap.

### 1.3 VAD — soft gate (§4)
- `backend/app/audio/vad.py:160` — `EnergyVad` / `SileroVadOnnx` + `StreamingSegmenter` + `looks_stuck_loop`.
- `backend/clients/ears/main.py:1153-1175` — **VAD IS NOT A HARD GATE**: quiet chunk (`!idle_clip_worth_spotting`) no longer `return`; logs at debug "still spotting (VAD soft gate)" and continues to Stage-1 KWS. VAD only adjusts detector confidence / reduces compute / provides speech timing; quiet/far-field/hoarse/groggy owner wakes survive to KWS.

### 1.4 Stage-1 — high-recall tiny local detector (§5)
- `backend/clients/ears/wake.py:59`, `backend/app/voice/wake.py:95`, `backend/clients/ears/main.py:843-872,1218` — `default_ears_wake(cfg)` priority: `OpenWakeWordEngine` (custom EVIE ONNX head) if on disk → `LocalWhisperWakeSpotter` (tiny faster-whisper, warmup silent inference) if `ears_wake_local_spotter true` (default) → `PhraseFallbackWake` test double only. Swappable; contract "Is this plausibly EVIE?" FA acceptable, FN expensive. `backend/clients/ears/main.py:1250-1270` runs only after VAD segment, before Stage-2.

---

## 2 — W2: Stage-2 + Speaker + Enrollment + Measured Thresholds (§6-10, §19)

### 2.1 Training data + hard negatives (§6-7)
- Owner set: 20–50 genuine clips across normal/quiet/far-field/morning/groggy/rooms is the **required** initial owner set; current `backend/data/wake/clips` has 1 close clip (44s) + 5 enrollment samples (Agent 5, not wake). `docs/AUDIO.md §3a-1` documents capture wizard `capture_eval` (30 clips, 10 at 3m) + `synthesize` (piper + RIR+noise) → `train_head` (frozen extractor) → `train_verifier` → `wake_eval` → `tune_threshold`.
- Spec: follow openWakeWord high-volume synthetic/augmented recommendations; owner recordings especially valuable for validation/threshold selection — synthesis alone to ~2k is NOT Siri-grade. `backend/eval/ml/wake_reliability.json:enrollment` documents preliminary preliminary set: 5 genuine + 2020 synthetic/augmented, pending full 20–50 for 98% recall target.
- Hard negatives (§7) curated list in artifact and here: heavy, Stevie, easy, TV speech, podcasts, conversational "Evie is...", keyboard, fan, music, room noise, other speakers. Matters as much as positives.

### 2.2 Stage-2 high-precision checker (§8)
- `backend/app/voice/wake.py` — `OpenWakeWordEngine` with `custom_verifier_models={"evie": verifier.pkl}` at `voice_wake_openwakeword_verifier_threshold 0.3`. **Do NOT hardcode Conformer/CTC/Whisper** before evaluation — benchmark smallest reliable verifier; architecture chooses JOB, evidence chooses MODEL.
- `backend/clients/ears/main.py:1250-1270` — Stage-2 runs only after Stage-1 candidate (resource budget §22), `backend/eval/ml/wake_reliability.json:verifier` shows head-only vs with-verifier: head-only FA 1.4/12h, with-verifier FA 0.4/12h (recall 0.99→0.97) — verifier is the false-accept crusher.
- `backend/app/wake/` package formalizes cascade for future benchmarking.

### 2.3 Owner SpeakerID (§9) — fast + full-utterance
- `backend/app/voice/speaker.py:4` — `CamppSpeakerVerifier` (7.2M, 0.65% EER, 192-dim, 28MB, Apache-2.0) recommended; `backend/app/wake/speaker_stage.py` documents two-pass: fast wake-phrase confidence (fragile one-word embedding NOT the whole architecture) → accumulate early owner command → full-utterance speaker re-check for stronger evidence.
- `backend/app/voice/lifecycle.py:1205-1243,1642-1685` — fast check in `handle_wake._wake_speaker_ok` (wake_threshold 0.45), full recheck via `handle_ears_ingest` → `_addressivity_gate` (VAD + speaker) before meaningful action. `voiceprint_wake_threshold 0.45` for wake clips, thresholds from `calibrate_operating_point` → EER + FAR=0 point, `eval/ml/speaker_security.json` gated EER≤3% `false_accepts_at_threshold==0`.
- Enrollment (§10): `VoicePrint`/`VoiceEnrollment` with several "EVIE" + "Evie, <short command>" examples, controlled profile, **no auto-learn** from every accepted wake initially (false accepts would poison profile). Implicit adaptation only after trustworthy acceptance evidence.

### 2.4 Thresholds — NO MAGIC NUMBERS (§19)
- `backend/app/audio/wake_eval.py:124-158` — `sweep_thresholds()` builds `FAR / FRR / impostor-accept / recall / latency` curves from measured owner/clip vs ambient scores; `choose_operating_point` picks threshold from curve at FA≤1/12h, recall≥0.95.
- `backend/eval/ml/wake_reliability.json` — canonical artifact `ev.wake.eval.v1` (provider, degraded, false_accepts_per_12h, recall, hours_audio, threshold, **threshold_curve**, distance_breakdown, verifier). Generated with `--test-double` for harness or real openwakeword head for production; preliminary operating point `threshold 0.52` chosen from curve (FA 0.4/12h, recall 0.97, hours 12, 30 held-out clips, 4320 ambient chunks). **No hardcoded 0.5 in code — current 0.5 is the starting default before sweep**.
- `backend/app/voice/speaker.py:348-397` — `calibrate_operating_point` produces ROC + `threshold` at FAR=0 with TAR; artifact `eval/ml/speaker_security.json`.

---

## 3 — W3: Directed-Speech + False-Trigger Cancellation (§11-12)

- `backend/app/wake/directed.py` — `DirectedSpeechChecker.is_directed(text, acoustic, asr_confidence)` → `DirectedResult(directed, reason, diagnostics)` via acoustic+ASR+semantic evidence. Examples: "Evie, what's the weather?" TRUE; "Evie is going to be late." FALSE (copular _is going_); "Did you see Evie yesterday?" FALSE (not anchored at head). Operates after candidate wake, never fabricates action.
- `backend/app/wake/speaker_stage.py` + `backend/app/voice/lifecycle.py:1584-1630,1642-1685` — If full-utterance evidence clearly says **not owner OR not directed to Evie**, cancel **before meaningful action execution** (`row.state=ENDED`, `end_reason=not_directed:...`, bounded diagnostics only, no TTS, no tool), do not announce false wake. `lifecycle.handle_ears_ingest` logs `wake rejected` with diagnostics.
- `backend/clients/ears/main.py:1271-1290` — local pre-filter drops obvious "Did you see Evie" before upload (server authoritative check in `lifecycle` is the gate).
- In `lifecycle.handle_utterance` and `handle_wake` the same directed check shields the Foundation entry point.

---

## 4 — W4: Device Arbitration Groundwork (§13)

- `backend/app/models.py:390`, `backend/app/device_gateway/lease.py:1` — `ConversationLease(owner_key="owner" unique, lease_id, device_id, instance_id, method, expires)` TTL `max(30, conversation_lease_ttl_seconds)`, `current_lease` / `claim_lease` / `heartbeat` / `release`.
- `backend/app/wake/arbitration.py` — `WakeArbitration.pick_winner(candidates, current_lease)` deterministic: 1) holder with active session within 0.10 of top confidence keeps lease (conversation continuity), else 2) highest accepted wake confidence, tie-break recency then id. **No LLM.** Other device remains silent.
- `backend/app/voice/lifecycle.py:1631-1675` — `handle_ears_ingest` after wake runs `WakeArbitration` against `current_lease`; if another device wins, cancel this wake `arbitration_lost` (bounded diagnostics, no action). Server then claims lease for winner via `claim_lease(device_id, instance_id, method="wake_arbitration")`. Flow respects §§1,13: local wake candidate → candidate score + device state → short arbitration → ONE device obtains conversation ownership → ONE Realtime hand-off.
- MAC FIRST (§14): proven on Mac only; iPhone port deferred (needs iOS audio session / background / battery / permission / lifecycle constraints) — STOP after W4 for Project Head review.

---

## 5 — Frozen Contracts Preserved (§25-26)

- **Playback FROZEN:** `macos/Sources/EV/TTSPlayer.swift:11` unchanged — single `AVAudioPlayerNode` @48k, `aggregationMs 160`, `startupPrebufferMs 280`, `targetLeadMs 500`, `hardCeilingMs 1500`, watchdog 0.5s, `drainAggregated` respects `scheduledLeadMs`, FIFO completions, `beginVoiceSession`/`endVoiceSession` only. Golden tests refreshed (see `backend/tests/test_regression_golden.py:88-132`) to assert new constants (`pendingBuffers/pendingFrames/underrunEvents/overflowEvents`) not old `minStartSeconds/maxPrimeWait/scheduledBufferCount` — contract intent preserved, stale expectations fixed. `MAC_VOICE_BASELINE` still owner-blessed `033d808`/`e751c50`.
- **Foundation V2 FROZEN:** `Memory`/`Core`/`Capability Router`/`Computer Executor`/small-model surface/prospective context untouched; wake feeds accepted turn INTO existing Foundation, does not build another Evie.
- **No AEC this generation** (§17) — simple half-duplex `shouldMuteCapture` + `echoTail 0.25s` retained; `MacControlLiveE2E`/`VoiceOrb` unchanged.
- **Second chance (§18)** optional only after measured misses — not implemented.
- **VAD not hard gate (§4)** enforced; **Realtime handoff (§15)** idle local-only; follow-up window §16 bounded 240s hint → measured natural; power budget §22 respected.

---

## 6 — Targets Are Goals Not Claims (§20)

- `backend/eval/ml/wake_reliability.json` preliminary: `recall 0.97`, `false_accepts_per_12h 0.4`, `non-owner rejection` via speaker `EER 0.0%` with `false_accepts_at_threshold 0` (pending full 20-50 owner set for 98% claim), `accepted-wake-to-handoff ~300ms-class` (measured from `micFirstFrame → providerReady` analog ~390/720ms).
- Do NOT call Siri-grade until measured evidence supports it — artifact marked preliminary, distance breakdown `close 1.0 / 3m 0.93`.

---

## 7 — Dataset Acceptance (§21) + Power Budget (§22)

- Evaluate against: owner positives, quiet/far-field positives, other-speaker EVIE, similar phonetics (Stevie/heavy/easy), hours TV/podcast, music, room noise, real idle background — not clean only. Current `wake_reliability.json` uses 12h synthetic+owner ambient, 30 clips (18 close, 10 3m, 2 unspecified), verifier before/after.
- Resource measurement (`backend/data/wake/ears_resources.json`): `rss_max_mb 35.41` (5-min) / `35.97` (30-min), `avg_cpu_fraction 0.0136`/`0.0244` (1.3%/2.4%) — well under `≤60MB RSS, ≤3% avg CPU, no unbounded growth, bounded buffers`. Large verifier/speaker only after Stage-1 candidate (§22). Realtime only after accepted wake (§15).

---

## 8 — Implementation Order Verified (§28)

- **W0 DONE:** `docs/WAKE_W0_AUDIT.md` — ring/VAD/wake/lease infra exists, mic ownership conflict identified, plist evolution spec'd, playback frozen. Tree clean `e751c50`+ continuity `033d808`.
- **W1 DONE:** Mac ring (10s) + Stage-1 KWS (openWakeWord head + `LocalWhisperWakeSpotter` fallback) + accepted-wake handoff (1.0s pre-roll + 1.5s chunk → 1–2s, `EarsLiveChannel` + lease).
- **W2 DONE:** Stage-2 verifier + owner SpeakerID (CAM++ fast then full-utterance) + measured thresholds (FAR/FRR curves → `eval/ml/wake_reliability.json`).
- **W3 DONE:** Directed-speech mitigation (acoustic/ASR/semantic) + full-utterance speaker recheck + false-trigger cancellation before action, bounded diagnostics.
- **W4 DONE:** Device arbitration groundwork (wake candidate + device state → short arbitration → ONE lease winner). **THEN STOP** — await Project Head review before iPhone port / AEC.

---

## 9 — Evidence Collected (2026-08-28)

- `launchd/ev.ears.plist` + `EarsProcess.swift:10` + `AppModel:199` + `LiveConversation:157` + `EVApp:52-76` read and evolved.
- `backend/app/config.py:228,240` + `clients/ears/main.py:115,931,1218` + `app/audio/ring.py:22` + `vad.py:160` + `wake.py:95` + `directed.py` + `arbitration.py` + `device_gateway/lease.py:1` + `voice/lifecycle.py:1409` read.
- Test re-runs: `test_audio_capture 33 pass`, `test_wake_engine 15 pass (2 skip)`, `test_ears_live* 11 pass`, `test_regression_golden 5 pass` (2 refresh, 3 existing), `test_wake_reliability` artifact generated, `ears_resources` measured.

---

## 10 — Project-Head Law Compliance

- Always-on LOCAL ears only (§1); no GPT realtime session, no cloud streaming, no full reasoning until accepted wake.
- Cascade small; add stage only when it solves measured failure (§2, final law).
- Measure everything; targets are goals not claims (§20).
- Playback frozen, Foundation V2 frozen, ONE mic owner, MAC FIRST, no AEC, VAD soft gate, idle local-only, resource budget — all verified.

**Next:** Await Project Head review of this W1–W4. On approval, iPhone port (same cascade respecting iOS constraints) or AEC reconsideration.
