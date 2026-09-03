# Voice Tool Lag – Root Cause & Remediation Plan
**Date:** 2026-09-02  
**Scope:** Eve voice glitching/lagging *only* when any tool is invoked (recall, memory, computer, etc.); normal conversation clean.

## 1. Signal Flow
```
Mac/iOS mic (16k PCM) --WS--> LiveSession --append_pcm--> GrokVoiceBridge --WS--> OpenAI/XAI Realtime
                                      ^                                          |
                                      |  TtsChunkEvent (audio_b64 + provider_response_id)
                                      +---- LiveSession outbound --WS--> Client TTSPlayer/LivePCMPlayer --> AVAudioEngine --> speaker
```
Tool path: `provider: response.function_call_arguments.done` → `GrokVoiceBridge._run_tool` → `LiveSession.run_live_tool` (DB + Mac computer_result) → `conversation.item.create: function_call_output` + `response.create: continuation` → new provider audio deltas.

## 2. Root Causes (all triggered only on tool path)

### RC1 – Client Playback Lane Drop (Mac, iOS)
`LiveConversation.swift: handle(tts_chunk)` uses `LivePlaybackLane.decide`.  
*Tool preamble still queued (queuedFrames>0) + fast tool (100-400ms) → lane = .drop → first 1-2 continuation chunks **dropped**, mid-sentence start after underrun → perceived stutter/missing words. Slow tool (>1.5s) starves queue → lane = .roll → `invalidatePlayback` (`playerNode.stop+reset`, `generation+=1`) **hard-chops** tail with click, discards outstanding completions.*

**Why tool-only:** non-tool responses are one continuous `provider_response_id`; no lane switch. Tool continuations are *new* `provider_response_id` → lane logic exercised.

### RC2 – Gap Underrun + Mic Gate Re-open
Gap 0.5-3s during tool execution drains `scheduledLeadMs` to 0.  
`TTSPlayer.maybeStartPlayback` needs 120 ms prime after long gap; gap >1.5s also expires `captureMuteUntil` (TTSPlayer) and `_SELF_ECHO_QUARANTINE_S` (GrokVoiceBridge, 1.0s). Mic unmutes, forwards ambient/room tone to provider → provider VAD creates spurious `speech_started` → duplicate `response.create` collides with tool continuation → two overlapping audio streams → glitch.

**Why tool-only:** only tool path has multi-second audio silence. Normal chat streams continuously.

### RC3 – Backend Audio Loop Blocking
`GrokVoiceBridge._upstream_event_loop` processes audio deltas *and* previously handled tool inline. Even with `_tool_worker`, `_handle_upstream` still `await`s `_emit_pcm` and blocks next audio delta if tool-queue backs up. `LiveSession.outbound` size 64 can be filled with progress/evidence/hud/state events during tool, delaying `TtsChunkEvent` behind head-of-line telemetry.

**Why tool-only:** extra HUD (`progress_hud`/`evidence_hud`) + synchronous dispatch only on tool.

### RC4 – Converter Discontinuity After Gap
`TTSPlayer.playbackBuffer` reuses one `AVAudioConverter` but resets `provided` per chunk (`convert(to:) closure`). Each chunk converted independently → phase discontinuity at every chunk boundary. After a gap, converter state is stale → first post-gap chunk has audible click. Previous fix `ee69414` fixed server resampler but left this client-side per-chunk gap artifact.

### RC5 – iOS Parity
`LiveVoiceCoordinator` + `LivePCMPlayer` lack tool-boundary lane handling; `player.enqueue` uses same per-buffer converter discontinuity and `shouldMuteCapture` tail 0.16s (too short) → same gap issues on iPhone.

## 3. Remediation Plan

### 3.1 Backend (app/voice/live/grok_voice.py)
- **Isolate tool pipeline:** Ensure *all* function-call handling offloads to `_tool_worker`; remove dead `await _run_tool` inside `_handle_upstream` (already spawned upstream). Make `_tool_loop` catch exceptions so one failed tool never kills audio loop.
- **Protect audio priority:** In `LiveSession.emit`, guarantee `TtsChunkEvent` never stuck behind full queue: when `outbound.full()` and event is `TtsChunkEvent`, discard coalesced telemetry *first* (`partial/state/latency/realtime_diagnostics`) and `await outbound.put` (not drop). Add `DRAIN_TIMEOUT` comment.
- **Gap-aware mic gate:** Extend `_POST_PLAYBACK_TAIL_S` logic to cover tool gaps: when `_tool_boundary_pending` or `_pending_tools>0`, keep mic gated even if playback inactive and lastAudio >1s ago. Introduce `_tool_gap_mic_gate_until` timestamp set on `function_call_arguments.done` and cleared on `response.created`. Check in `_playback_blocks_mic`.
- **Converter continuity not needed server side** (now native rate) – no change.

### 3.2 LiveSession (app/voice/live/session.py)
- **Mute during tool:** On `push_progress` / `tool_boundary_pending`, set `engine.state.assistant_is_speaking = True` (or dedicated `tool_state`) so tick loop and `_playback_blocks_mic` remain closed across gap. Clear only after `ReplyEvent` or `TtsChunkEvent` resumes.
- **Outbound queue:** `_prepare_outbound` already discards `TtsChunkEvent` correctly; adjust to prioritize: if `event.type == tts_chunk` and `outbound.full()`, discard `queued.type in _LIVE_COALESCED_EVENT_TYPES` *aggressively* before attempt.

### 3.3 macOS Client (TTSPlayer.swift + LiveConversation.swift)
**TTSPlayer.swift**
- Smooth gap resume: Change `maybeStartPlayback` to treat tool gaps specially: if `underrunEvents>0 && !responseFinished` and `scheduledLeadMs >= 80` (one chunk) *immediately* resume, not requiring 120ms prime after long gap where underrun already happened. Keep `resumeHole` window extend to 2.0s when `responseFinished==false` and `underrunEvents>0` (tool gap), so continuation resumes instantly after gap.
- Converter continuity: Make `playbackBuffer` handle streaming correctly: keep converter alive across chunks without resetting `provided` incorrectly; feed chunks through single converter instance with proper `inputStatus` handling (already does) but *don't* recreate converter per call if rate same; preserve fractional position. Add comment and ensure `converter.reset()` not called per chunk.
- Tail hold: Extend tool-gap tail: add `toolGapMuteUntil: Date` set from `LiveConversation` when tool starts, consulted in `shouldMuteCapture`/`mirroredSpeaking` so mic stays muted across gap regardless of `pendingFrames`.

**LiveConversation.swift**
- **Eliminate drop:** Change `LivePlaybackLane.decide` last case from `.drop` to `.adoptProviderId`. Adopt keeps single AVAudio stream, appends continuation PCM to same `activeResponseID` without `invalidatePlayback` → no chop, no loss. Only `rollToNewResponse` when truly starved (queued==0). Update comment.
- **Mic gate across tool:** When `lane == .adoptProviderId` and previous lane was tool continuation, set `playbackPlayer.toolGapMuteUntil = now+3.0` (cover typical tool latency).
- **Smooth roll:** When rolling, avoid `playerNode.stop()+reset` if `pendingBuffers==0` already starved → just `beginResponse` without extra stop (TTSPlayer `invalidatePlayback` already safe when not playing). Ensure `finishResponse` completion settled before `beginResponse`.

**LivePlaybackLane.swift**
- Change logic:
```swift
if accepted.isEmpty { return .adoptProviderId }
if accepted == incoming { return .enqueue }
if queuedFrames <= 0 { return .rollToNewResponse }
return .adoptProviderId // was .drop
```
- Update doc comment.

### 3.4 iOS Client (EVClient/LiveVoice.swift + LiveVoiceCoordinator.swift)
- Mirror mac fix: `LivePCMPlayer` `shouldMuteCapture` tail extend to 1.5s during tool gap; add `toolGapMuteUntil` similar. Change converter to reuse correctly (or document iOS uses AVAudioConverter per buffer – already similar, extend buffer to avoid per-chunk click by using larger aggregate).
- `LiveVoiceCoordinator.handle(tts_chunk)` should not drop: simply `player.enqueue` irrespective of providerResponseId (iOS doesn't track lane) – already does; ensure it doesn't reset on gap. Add same adopt logic if tracking.

### 3.5 Testing
- Unit: add `test_tool_continuation_lane` for `LivePlaybackLane` (queued>0 adopt, queued==0 roll).
- Unit: `test_grok_mic_gate_during_tool_gap` – simulate `function_call` then ensure `append_pcm` withheld for 2s even after playback inactive.
- Unit: `test_live_outbound_prioritizes_tts_during_tool` – fill outbound with telemetry, emit TtsChunk, verify not dropped.
- Integration: existing `test_voice_live` barge-in + new `test_tool_audio_continuity` (synthetic tool latency 300ms, verify zero dropped chunks, no underrun >1).
- P0 transport containment tests (already) – ensure still pass.

## 4. Implementation Order
1. Backend grok_voice mic gate + tool worker isolation
2. LiveSession outbound priority + assistant speaking hold
3. LivePlaybackLane → adopt fix
4. TTSPlayer gap resume + toolGapMute
5. iOS parity
6. Tests + `make test` / `make eval`

## 5. Risks & Mitigations
- Adopt could merge stale duplicate responses → mitigated because `.adopt` only when `incoming != accepted` but `queued>0`; provider guarantee no duplicate tool continuation with same ID; duplicates already deduped via `_handled_tool_calls`.
- Extending mic mute 3s could hide legitimate barge-in during tool gap → owner can still `barge_in` via Escape/Stop button (deterministic path) which bypasses mic gate.
- Outbound aggressive discard may lose `partial` transcript → partials coalesced (latest kept), not lost.

## 6. Verification
- `uv run pytest backend/tests/test_voice_*` – all pass, new tool continuity tests pass
- `make test` – full suite green
- Manual soak: `make compose-up && curl /v1/health` + Mac EV.app talk test with memory recall + computer tool -> `~/Library/Logs/EV/tts-metrics.jsonl` shows `underruns ≤1` and `gap` handling, no `overflow` or `gaps` >0
- `~/Library/Logs/EV/startup-trace.jsonl` shows no dead-link reconnections during tool turns
