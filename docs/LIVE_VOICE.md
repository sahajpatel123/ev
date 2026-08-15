# EV LIVE — full-duplex conversational runtime

The HTTP voice path (`POST /v1/voice/utterance`, SSE `/utterance/stream`) is a
**turn-based door**: one clip in, one reply out. That is the older generation
of voice assistants.

EV LIVE is the continuous conversational operating system. It does not replace
wake, owner verification, ASR, TTS, memory, or DeepSeek. It sits in front of
them and decides *when* to listen, wait, acknowledge, interrupt, speak, or
delegate deep work.

```text
                 EV LIVE (real-time nervous system)
                         │
        ┌────────────────┼────────────────┐
        ↓                ↓                ↓
    Perception       Conversation       Audio
                      State             Rendering
        │                │                 │
     VAD / speech    turn-taking        prosody
     wake (ears)     interruption       timing
     noise           waiting            streaming TTS
        │                │                 │
        └────────────────┼────────────────┘
                         ↓
                  EV ORCHESTRATOR
                         │
         ┌───────────────┼───────────────┐
         ↓               ↓               ↓
      DeepSeek         Memory           Tools
      reasoning       retrieval         APIs
```

The most important split:

```text
EV LIVE  = ears + timing + conversational behavior + voice
DeepSeek = deep reasoning
Memory   = long-term identity and life context
Tools    = actions and external information
```

Always-on ears (VAD, wake word) stay a **low-power local runtime**. EV LIVE
activates after a verified wake. A large language model is never left chewing
raw microphone audio 24/7.

## 1. What this is not

```text
Speech-to-Text → LLM → Text-to-Speech
```

That pipeline still exists (`app.voice.pipeline`) and LIVE **reuses it** when
a user turn is actually finished. LIVE's job is everything around that:

- silence is information (thinking pauses vs turn-end)
- backchannels ("Mhm.", "Yeah.") while the owner holds the floor
- barge-in the instant the owner speaks over EVIE
- time-to-first-audio (stream TTS chunks, speak a filler while tools run)
- behavior envelope (how to sound) separate from semantic content

The existing HTTP/SSE utterance path stays. Clients migrate to the WebSocket
when they can stream continuously.

## 2. Session door vs conversation loop

When **EV.app is open**, opening the app *is* the door. There is no wake
word. `POST /v1/voice/live/open` creates an owner-authenticated session and
the menu-bar client streams the microphone on `WS /v1/voice/live`.

The wake-word path remains for `ev.ears` when the app is *not* holding the
mic (closed / background). `ev.ears` is stopped while EV.app is open so the
two processes do not share one input device.

```text
EV.app open ──POST /live/open──▶ AWAKE ── WS /v1/voice/live
                                           (continuous listen + speak)

IDLE ──wake (ears, app closed)──▶ VERIFY ──▶ AWAKE
                                               │
                                               └── WS /v1/voice/live
```

`WS /v1/voice/live?session_id=<uuid>` accepts an awake / follow_up /
processing / responding session. App-open and Talk sessions that already
ended are revived in place. Auth is `Authorization: Bearer …` or `?token=`.

## 3. Protocol

Client → server (JSON text frames, or raw PCM16 binary at 16 kHz mono):

| `type` | Purpose |
| --- | --- |
| `audio` | `{pcm16_b64}` — one capture block; server VAD + turn-taking |
| `speech` | `{active: true\|false}` — explicit VAD (tests / client VAD) |
| `partial` | `{text, sequence}` — incremental ASR hypothesis |
| `text` / `transcript` | `{text, commit?}` — inject a finished utterance (PTT / tests). Default `commit: true` |
| `playback` | `{active}` — client is playing assistant audio |
| `commit` | force end-of-turn (PTT release) |
| `control` | `{action: end\|quiet\|attentive\|passive\|barge_in\|commit}` |

Server → client (`LiveEvent.as_dict()`):

| `type` | Purpose |
| --- | --- |
| `ready` | channel open; start streaming audio |
| `state` | conversation snapshot (phase, who is speaking, interruption) |
| `partial` | incremental transcript |
| `final_transcript` | committed user turn |
| `backchannel` | "Mhm." / "Yeah." — play immediately, do not treat as a full reply |
| `tts_chunk` | playable spoken unit (start now) |
| `latency` | `{metric: ttfa, ms}` — time from turn authorization to first audio |
| `reply` | full reply metadata; chunks already streamed |
| `barge_in` | **stop playback immediately** |
| `error` | `{code, message, fatal}` — `fatal: true` closes the channel |

The engine ticks ~20 Hz (`EV_VOICE_LIVE_TICK_MS`, default 50).

## 4. Turn-taking (silence is information)

Naive rule, rejected here: `if silence > 800ms: user_finished()`.

LIVE classifies the last ASR partial:

| Pause class | Example | Wait |
| --- | --- | --- |
| `complete` | "That's interesting." / "what's the weather" | `EV_VOICE_LIVE_END_PAUSE_MS` (280) |
| `wake` | "Evie" | `EV_VOICE_LIVE_WAKE_HOLD_MS` (650) |
| `thinking` | "I don't know" | `EV_VOICE_LIVE_THINKING_GRACE_MS` (700) |
| `trailing` | "I was thinking maybe we could" | `EV_VOICE_LIVE_TRAILING_GRACE_MS` (1100) |

A leading Evie is stripped before the pause class and before chat, so
"Evie what's the weather" is a weather turn, not a wake-only Yes?. Bare
"Evie" still gets a spoken "Yes?" that **does not hold the floor** — the
owner can keep talking without that ack cancelling their command.

Thinking sounds (`hmm`, `uh`, `yeah`) are not turns. Empty silence is not a
turn. A response already in flight cannot start a second one. User speech
while EVIE is speaking is barge-in, not a new HTTP utterance.

`control.action=quiet` extends the pause (stay quiet and listen). `passive`
never self-responds (wake-level only).

## 5. Backchannels

While the owner holds the floor in attentive mode, LIVE may speak a short
cue (`Mhm.`, `Yeah.`, `Right.`, `Got it.`, `Okay.`) after ~1.8 s, capped at
three per turn. It stays quiet for sad / frustrated / urgent affect.

These are overlapping listening cues, not a held floor. The client plays
them without cancelling the user's turn.

## 6. Behavior envelope vs TTS

LIVE does **not** stuff `[sad][slow]` tags into reply text.

```json
{
  "semantic_content": "I understand why that was frustrating.",
  "interaction_mode": "empathetic",
  "energy": "low",
  "pace": "slow",
  "interruptibility": "high",
  "pause_before_response_ms": 240
}
```

`to_speech_style()` maps that onto the existing `SpeechStyle`
(urgency / warmth / brevity) so Kokoro, Edge, and the meta double keep
working. Owner emotion still flows through `app.ev.interaction`.

## 7. Foreground conversation + background intelligence

Quick turns run the shared chat+TTS pipeline immediately (same
`stream_chat_tts_pipeline` as SSE).

Turns that need search, life-write tools, or long "why / explain / what
happened" reasoning are **delegated** on the pipeline path: LIVE speaks a
filler and runs the chat provider / tools in a background task.

Live audio does **not** go through DeepSeek (or Grok 4.6) on every spoken
turn when `EV_XAI_API_KEY` is set. It goes to **Grok Voice Think Fast 2.0**
(`wss://api.x.ai/v1/realtime`, model `grok-voice-think-fast-2.0`). That
model hears, thinks-while-speaking, and returns audio. EV still executes
life tools when it asks. Typed chat, the HUD, and HTTP utterance stay on
`EV_CHAT_PROVIDER` — typically `deepseek-v4-flash`. The voice model is not
a drop-in for chat completions.

Until the xAI key is set, live keeps the local ASR + DeepSeek + TTS loop.
`EV_VOICE_LIVE_BRAIN=pipeline` forces that loop even with a key.

## 8. Configuration

| Key | Default | Meaning |
| --- | --- | --- |
| `EV_VOICE_LIVE_ENABLED` | `true` | Serve `WS /v1/voice/live` |
| `EV_VOICE_LIVE_TICK_MS` | `50` | Decision cadence |
| `EV_VOICE_LIVE_END_PAUSE_MS` | `280` | Complete-sentence / finished-question pause |
| `EV_VOICE_LIVE_THINKING_GRACE_MS` | `700` | Mid-thought pause |
| `EV_VOICE_LIVE_TRAILING_GRACE_MS` | `1100` | "and… / maybe… / could…" pause |
| `EV_VOICE_LIVE_WAKE_HOLD_MS` | `650` | Bare "Evie" — wait for a command before Yes? |
| `EV_VOICE_LIVE_MIN_SPEECH_MS` | `160` | Ignore clicks / coughs |
| `EV_VOICE_LIVE_QUIET_END_PAUSE_MS` | `1300` | Extra wait in quiet mode |
| `EV_VOICE_LIVE_RESPONSE_COOLDOWN_MS` | `450` | Echo / re-trigger guard |
| `EV_VOICE_LIVE_MAX_PAUSE_MS` | `2500` | Hard cap on any wait |
| `EV_VOICE_LIVE_BACKCHANNEL` | `true` | Enable listening cues |
| `EV_VOICE_LIVE_VAD_THRESHOLD` | `0.35` | Energy/Silero gate on PCM frames |
| `EV_VOICE_LIVE_ASR_PARTIAL_MS` | `160` | Incremental ASR cadence |
| `EV_VOICE_LIVE_BRAIN` | `auto` | `auto` = Grok Voice whenever `EV_XAI_API_KEY` is set; `pipeline` = local ASR+chat+TTS |
| `EV_XAI_VOICE_MODEL` | `grok-voice-think-fast-2.0` | Speech-to-speech model id |
| `EV_XAI_VOICE_VOICE` | `eve` | Grok Voice roster voice |

ASR, TTS, wake, follow-up, and sleep phrases stay in `docs/VOICE.md`.

## 9. Code map

| Module | Role |
| --- | --- |
| `app.voice.live.state` | Conversation operating-system snapshot |
| `app.voice.live.turn_taking` | Silence-aware turn decisions |
| `app.voice.live.backchannel` | When to say "Mhm." |
| `app.voice.live.behavior` | Envelope → `SpeechStyle` |
| `app.voice.live.delegate` | Foreground vs DeepSeek/tools |
| `app.voice.live.engine` | Signal in, decisions out (no I/O) |
| `app.voice.live.session` | Engine + ASR/TTS/chat callbacks |
| `app.voice.live.grok_voice` | Grok Voice Think Fast 2.0 realtime bridge |
| `app.voice.live.transport` | WebSocket mapping |
| `app.voice.pipeline` | Shared STT → chat → TTS (reused, not replaced) |
| `clients/ears` | Low-power mic / VAD / wake (still the 24/7 ear) |

## 10. Tests

```bash
cd backend
uv run pytest tests/test_voice_live.py tests/test_voice_lifecycle.py -q
```

`test_voice_live.py` is offline-deterministic (no weights, echo/meta
doubles). It covers thinking vs complete pauses, barge-in cancel, sleep
phrases, behavior envelopes, and deep-work routing.

## 11. Client integration status

- EV.app opens `POST /v1/voice/live/open` on launch and streams the
  microphone on `WS /v1/voice/live` for as long as the app is open. The
  owner just talks — no Evie wake, no push-to-talk door.
- `LiveVoiceConnection` is the WebSocket client and accepts raw PCM16 frames.
- `LiveVoiceMicrophone` converts device input to 16 kHz mono PCM16 and
  enables voice-processing AEC so listen-while-speak and barge-in work on
  the same Mac.
- The always-on ears client (`clients/ears`) still opens `WS /v1/voice/live`
  after a wake handshake when the menu-bar app is not holding the mic. EV.app
  stops `ev.ears` while it is running.
- Clients must stop local playback when they receive `barge_in` and begin
  playback for each `tts_chunk`. The HTTP/SSE path remains available as a
  fallback for clients that cannot hold a WebSocket.
- Native audio models (no transcript in the middle) are a future engine
  swap behind the same live state machine. LIVE does not require them to
  improve turn-taking, barge-in, backchannels, or perceived latency.
