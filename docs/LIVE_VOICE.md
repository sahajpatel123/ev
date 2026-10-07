# EV LIVE — full-duplex conversational runtime

The HTTP voice path (`POST /v1/voice/utterance`, SSE `/utterance/stream`) is a
**turn-based door**: one clip in, one reply out. That is the older generation
of voice assistants.

EV LIVE is the continuous conversational operating system. It does not replace
wake, owner verification, memory, or EV Core policy. Gemini Live 3.8 is the
hearing/speaking lane; MiMo-V2.6-Flash is the reasoning/tool-planning lane
(reached via `delegate_task`); local TTS is the pipeline speaking lane.
EV LIVE coordinates them and decides *when* to listen, wait, acknowledge,
interrupt, speak, or delegate deep work.

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
      MiMo             Memory           Tools
      (delegate_task)  retrieval         APIs
```

The most important split:

```text
EV LIVE       = timing + conversational behavior + voice transport
Gemini Live   = speech perception + direct replies (transcript/endpointing/audio)
MiMo          = reasoning, planning, tool calls, and replies (via delegate_task)
EV Core       = authorization, execution, verification, and audit
Memory/Tools  = long-term identity, life context, and external actions
```

Always-on ears (VAD, wake word) stay a **low-power local runtime**. EV LIVE
activates after a verified wake. A large language model is never left chewing
raw microphone audio 24/7.

## 1. What this is not

```text
Gemini Live 3.8 → delegate_task → MiMo-V2.6-Flash → Text-to-Speech
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
| `control` | `{action: end\|quiet\|attentive\|passive\|barge_in\|commit\|pause\|resume\|unmute\|mute\|cancel}` |

Server → client (`LiveEvent.as_dict()`):

| `type` | Purpose |
| --- | --- |
| `ready` | channel open; start streaming audio. `config` includes `brain`, `device_id`, `tts_device_id`, `paused`, `muted`, `approval_hold` |
| `state` | conversation snapshot (phase, who is speaking, interruption, pause/mute/hold) |
| `partial` | incremental transcript |
| `final_transcript` | committed user turn |
| `backchannel` | "Mhm." / "Yeah." — play immediately, do not treat as a full reply |
| `tts_chunk` | playable spoken unit (start now) |
| `latency` | `{metric: ttfa, ms}` — time from turn authorization to first audio |
| `reply` | full reply metadata; chunks already streamed. May include `device_id` / `tts_device_id` |
| `hud` | `{card, kind}` — progress, evidence, or approval-hold `ev.hud.card.v1` card |
| `barge_in` | **stop playback immediately** (speech only; durable jobs keep running) |
| `error` | `{code, message, fatal}` — `fatal: true` closes the channel. `realtime_disconnect` / `realtime_connect` are **non-fatal**; the EV socket stays open and the bridge reconnects. Native ASR failures (`asr_connection_closed`, `asr_timeout`, `asr_unusable`) request one client-side live-channel recovery per connection generation; auth/quota/format failures reset the incomplete turn and remain visible for controls/text. `asr_empty_result` / `asr_no_speech` are ordinary no-speech outcomes. |

The engine ticks ~20 Hz (`EV_VOICE_LIVE_TICK_MS`, default 50).

Voice stays an interface. Pause/resume/mute drop PCM but keep the socket;
`resume` re-arms realtime input and reconnects if the upstream died during a
long mute. `barge_in` / `cancel` stop in-flight speech only — durable jobs
keep running. R3/R4 tools emit an approval-hold HUD card (`confirmation_channel:
hud_or_biometric`); wake verification is not an independent factor, and the
audio loop never waits for the tap. Quiet hours suppress proactive live
speech; emergencies still speak. Devices share one live `conversation_id`
and keep per-device `VoiceSession` rows. Gemini Live uses a
function-tool projection computed from the current runtime capability
manifest. Only available, live-eligible tools are advertised; every call is
validated and sent through `evaluate_policy`/`dispatch` before execution.
The ready/state capability manifest retains the same runtime manifest,
live-tool projection, approved/executable tool names, current device/provider,
setup gaps, and confirmation requirements used for that session's HUD.
R3/R4 confirmation holds keep the same session alive, then the approved result
is returned to Gemini and the continuation resumes spoken output.
The transcript regex resolver (`resolve_live_action`) is pipeline-only and is
not used by Gemini. iOS opens the same live door via
`LiveVoiceCoordinator` (shared `LiveVoiceConnection` + microphone). Mac
EV.app still uses `LiveConversation`. Live injects that miss an in-process
socket are parked on the Callout table (`voice.live.inject`) and drained by
the owning websocket. `POST /v1/voice/live/open` resolves a registry UUID
or `Device.name`. Device HUD approve needs Face ID/Touch ID then
`POST /v1/identity/reverification` (`X-EV-Reverify`), or a WebAuthn
assertion on the approve body. Wake verification is not an independent
factor.

### Acceptance provider modes

The acceptance harness has two explicit modes:

- `local_deterministic`: a provider-free realtime WebSocket double. It records
  only event types, tool names, argument keys, call IDs, evidence presence, and
  continuation requests. It is the deterministic CI gate for the complete
  voice → function → policy → adapter → evidence → spoken-result chain.
- `real_provider`: provide the MiMo (OpenRouter) and Gemini Live (Google)
  credentials. The adapters record safe metadata for provider selection,
  session readiness, tool names, function-call argument keys, function output
  status, ASR endpointing, and continuation requests. They never log API keys,
  raw audio, or raw private tool payloads.

The local mode proves architecture and is safe to run without external side
effects. Real-provider mode requires outbound WSS and valid credentials; it is
not considered exercised by CI unless a live provider session is explicitly
run. Before Mac manual testing, verify the ready event reports matching
`tool_names` and `upstream_tool_names`, `upstream_session_ready=true`, and no
`provider_mismatch` or `capability_error`.

### 10-minute Mac dogfood gate

Use the local deterministic provider first. Keep external side effects
disabled for this pass: do not test calls, mail sending, locks, printers,
purchases, destructive commands, cameras, or drones.

| Minute | Check | Pass evidence |
| --- | --- | --- |
| 0:00–1:00 | Start the backend and open EV.app's live session. | The socket opens and emits one `ready` event. |
| 1:00–2:00 | Inspect the first `ready.config.realtime` before speaking. | `tool_names == upstream_tool_names`, `upstream_session_ready=true`, and `capability_error` is empty/null. If not, stop; a later diagnostics frame is not a substitute for this gate. |
| 2:00–3:00 | Say “What’s the weather in Surat?” | `get_weather` is advertised, selected, policy-allowed, adapter-backed, evidenced, and spoken. |
| 3:00–4:00 | Say “Set a timer for one minute.” | `start_timer` returns the timer ID in evidence and in the spoken reply. |
| 4:00–5:00 | Say “What do I prefer for voice tests?” | `search_memory` returns saved results with provenance/evidence; stop on any success claim without evidence. |
| 5:00–6:00 | Say “Show me the local acceptance card.” | `present` opens only the safe local fixture surface and emits evidence. |
| 6:00–7:00 | If the connected fixture is installed, ask for calendar and messages. | `calendar_read`/`list_messages` appear only with their matching integration and scopes; otherwise EV explains the missing connection. |
| 7:00–8:00 | Review the live HUD and audit record. | Each call shows tool name, policy effect, adapter result, evidence, and call ID; no raw audio, secrets, or private payloads. |
| 8:00–9:00 | Ask “What can you do?” | The spoken answer contains no `manifest` or `runtime execution` jargon and no generic fallback. |
| 9:00–10:00 | Stop the session and record GO/NO-GO. | Realtime disconnect/reconnect and provider failures remain explicit; do not continue to real side effects. |

A failed row is a NO-GO for owner-facing dogfood. Capture the exact ready
diagnostics, advertised/called tool names, policy effect, adapter status,
evidence fields, spoken result, and audit request ID, with secrets and raw
private payloads omitted.

Acceptance handoff: the current transport emits the initial `ready` frame
before the upstream `setupComplete` acknowledgement. In that frame,
`upstream_tool_names` is empty and `upstream_session_ready=false`; the later
`realtime_diagnostics` frame is the first frame that proves the handshake.
This is a NO-GO for the requested “ready before speaking” contract until the
realtime bridge/client wiring makes the owner-facing ready state wait for, or
explicitly gate on, the matching acknowledgement.

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

Live audio does **not** go through the chat brain (MiMo) on every spoken
turn when a live key is set. Default live brain is **Gemini Live
`gemini-3.8-live-extended-thinking`**
(`wss://generativelanguage.googleapis.com/ws/google.ai.generativelanguage.v1beta.GenerativeService.BidiGenerateContent`)
when `EV_GOOGLE_API_KEY` is set (paid tier only). The model hears,
thinks-while-speaking, and returns audio. The bridge uses automatic VAD
with native PCM audio and the configured `Aoede` voice by default, plus
sliding-window context compression and session resumption so long
conversations survive reconnects. EV still executes life tools when it
asks. Typed chat, the HUD, and HTTP utterance stay on `EV_CHAT_PROVIDER` —
`xiaomi/mimo-v2.6-flash`. The voice model is not a drop-in for chat
completions.

Until a live key is set, live keeps the local ASR + MiMo + TTS loop.
`EV_VOICE_LIVE_BRAIN=pipeline` forces that loop even with a key.
`EV_VOICE_LIVE_BRAIN=gemini` forces the live provider.

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
| `EV_VOICE_LIVE_BRAIN` | `auto` | `auto` = Gemini Live if `EV_GOOGLE_API_KEY` is set, else local pipeline; `gemini` forces; `pipeline` = local ASR+chat+TTS (legacy `openai` / `xai` values map to `gemini`) |
| `EV_GOOGLE_API_KEY` | — | Google AI key (`aistudio.google.com`). Paid tier only: free-tier traffic may be used to improve Google's products |
| `EV_GEMINI_LIVE_MODEL` | `gemini-3.8-live-extended-thinking` | Gemini Live speech-to-speech model id. Live function tools come from the current runtime projection and are rechecked by policy before dispatch |
| `EV_GEMINI_LIVE_VOICE` | `Aoede` | Gemini Live prebuilt voice |
| `EV_GEMINI_LIVE_REASONING_EFFORT` | `low` | Thinking depth for the Extended Thinking model; the base live model ignores it |
| `EV_GEMINI_LIVE_URL` | `wss://…BidiGenerateContent` | Gemini Live WebSocket |

ASR, TTS, wake, follow-up, and sleep phrases stay in `docs/VOICE.md`.

## 9. Code map

| Module | Role |
| --- | --- |
| `app.voice.live.state` | Conversation operating-system snapshot |
| `app.voice.live.turn_taking` | Silence-aware turn decisions |
| `app.voice.live.backchannel` | When to say "Mhm." |
| `app.voice.live.behavior` | Envelope → `SpeechStyle` |
| `app.voice.live.delegate` | Foreground vs MiMo/tools |
| `app.voice.live.engine` | Signal in, decisions out (no I/O) |
| `app.voice.live.session` | Engine + ASR/TTS/chat callbacks |
| `app.voice.live.gemini_live` | Gemini Live speech-to-speech bridge |
| `app.voice.live.transport` | WebSocket mapping |
| `app.voice.pipeline` | Shared STT → chat → TTS (reused, not replaced) |
| `clients/ears` | Low-power mic / VAD / wake (still the 24/7 ear) |

## 10. Tests

```bash
cd backend
uv run pytest tests/test_voice_live.py tests/test_voice_interaction.py tests/test_voice_lifecycle.py -q
```

`test_voice_live.py` is offline-deterministic (no weights, echo/meta
doubles). It covers thinking vs complete pauses, barge-in cancel, sleep
phrases, behavior envelopes, and deep-work routing.
`test_voice_interaction.py` covers mute reconnect, provider disconnect,
approval hold, barge-in vs durable jobs, quiet hours, cross-device
conversation identity, provider function continuation, the live mailbox, and registry
device resolution.

## 11. Client integration status

- EV.app opens `POST /v1/voice/live/open` on launch and streams the
  microphone on `WS /v1/voice/live` for as long as the app is open. The
  owner just talks — no Evie wake, no push-to-talk door. The Mac client
  sends the registry device UUID (`EV_REGISTRY_DEVICE_ID`) when present.
- iOS EV.app starts the same live door through `LiveVoiceCoordinator`
  after device bootstrap. This tree does not compile the iOS app (no
  Xcode here); the coordinator is authored and compiled via EVClient on
  macOS.
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
- Gemini Live failures never leave a client latched in a thinking state: the
  client returns to listening for deterministic ASR errors, and closes the
  current live socket once when a native ASR stream is unusable so the normal
  lifecycle creates a fresh session. Existing playback is allowed to finish
  before recovery. The public websocket event order and Mac/iPhone controls
  do not change.

## 12. Delegate graph (MiMo planner → supervisors → workers)

When `EV_COGNITIVE_MODE=realtime_delegate` and `EV_DELEGATE_GRAPH=on`, a
`delegate_task` call runs as a small DAG instead of a single kernel turn:

```text
delegate_task(task)
  → MiMo planner: one structured call → validated DAG (≤8 nodes, tiered R/W/D)
  → fast path: single read-only node → one static tool call + evidence check
  → deep path: topological waves, ≤3 nodes in parallel, one supervisor each
       supervisor: run worker → collect receipt → decider verdict
       retry: the supervisor's reasons become the retry's brief; the worker
              must fix the named failure, not repeat it
  → manager report: per step DONE / NOT_DONE / NEEDS_OWNER + state/score/why
  → if work remains and no owner question: next shift re-plans ONLY the
       remainder with that report as context (≤ EV_GRAPH_MAX_SHIFTS)
  → aggregator joins verdicts into answered / waiting / failed
```

A job is therefore a sequence of shifts (plan → work → judge), capped by
`EV_GRAPH_MAX_SHIFTS` (default 2). The final `GraphOutcome` carries `shifts`,
`remaining` (human labels), `status_report`, and a spoken status the live
mouth delivers: what finished, what did not, and what needs the owner. Tier-D
stops and decider `ask_owner` verdicts end the loop immediately — a re-plan
never routes around the owner.

Contracts live in `app/cognitive/graph.py` (`TaskNode`, `WorkerReceipt`,
`SupervisorVerdict`, `StatusEvent`, `GraphOutcome`, `shift_report`). Workers
(`app/cognitive/worker.py`) run deterministic semantic tools first and a
bounded, deadline-aware MiMo executor loop second; each node runs under
session snapshot/restore isolation with its own DB session. Supervisors
(`app/cognitive/supervisor.py`) call the decider (`app/gateway/decider.py`,
same OpenRouter key as MiMo, Decisions API) for one typed verdict — a `noul`
(is it done?), a `choice` (which outcome?), and a `score` (how good?) against
the same node state — and enforce: no evidence refs means not done, tier-D
means ask the owner, undecodable verdicts degrade to the deterministic local
check.

Progress persists on the job row (`budget.status_events`, last 20) and the
throttled milestone becomes the receipt `spoken`, so `operation=status`
reports live progress. Planner outages pre-execution fall back to the legacy
single-turn path and are recorded on the job (`budget.graph_fallback` +
receipt `graph_fallback`), never silent; anything failing mid-run is an
honest job failure, never a silent rerun. Gemini's session instructions gain
a manifest-derived LIVE REACH card
(`app/ev/protocols.py::delegate_capability_card`) so speech-time awareness
tracks ready vs setup-gated families. Default `off` keeps legacy behavior
byte-identical. Tests: `tests/test_delegate_graph.py` (offline).
