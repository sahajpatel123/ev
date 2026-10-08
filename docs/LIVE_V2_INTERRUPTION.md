# LIVE V2 interruption — owner cut-in, Eve cut-in, floor, gestures

Companion to [`LIVE_VOICE.md`](LIVE_VOICE.md). V1 (`interrupt_v1.py`, the
Mac `ExplicitInterruptMonitor`) stays dead, legacy, and unwired. Everything
below is the new V2 architecture: client-measured evidence, server fusion,
exactly-once cancel, symmetric floor ownership, and a shared gesture
envelope for Mac and iPhone PWA (`pcm_ws` — WebRTC media is retired).

## The one law

Local silence happens first, then evidence, then the server fuses. The
server never interrupts on ambiguous evidence: not-confidently-self does
not mean owner.

## Owner cuts Eve (Interrupt V2)

### Client → server: `owner_evidence`

Sent over the live WS when the client confirms owner speech **during Eve's
playback** (Mac 100 ms onset poll, PWA 100 ms RMS windows, both with
~300 ms persistence):

```jsonc
{
  "type": "owner_evidence",
  "speech_ms": 300,          // contiguous near-end speech, required
  "confidence": 0.72,        // detector score, optional
  "client_confirmed": true,  // persistence window completed, optional
  "aec_active": true,        // echo cancellation truthfully reported
  "playback_active": true,
  "audio_played_ms": 1200,   // heard position, optional (omit if unknown)
  "preroll_ms": 800,         // onset audio preserved, optional
  "echo_score": 0.1,         // self-similarity vs played audio, optional
  "mic_rms": 0.05,           // sanity feature only, optional
  "response_id": "resp-1"    // latch key, optional
}
```

Honesty rules: never send a zero you did not measure (omit instead), never
fabricate confidence, report `aec_active` truthfully (Mac: `false`, phone:
`true`).

### Fusion (`app/voice/live/interrupt_v2.py`)

| Verdict | Meaning |
| --- | --- |
| `owner_confirmed` | Interrupt. Exactly once per response. |
| `ambiguous` | Never interrupts. Silent. |
| `self` | Echo. Never interrupts. Silent. |
| `ignored` | Eve is not speaking. Normal turn-taking owns it. |

Order: Eve silent → `ignored`; `echo_score >= 0.5` → `self`;
`speech_ms < 160` → `ambiguous`; absurd `mic_rms` → `ambiguous`;
present-but-low confidence → `ambiguous` (bar 0.5 with AEC, 0.6 without);
absent confidence requires `client_confirmed` + AEC or → `ambiguous`;
playback without AEC and without client confirmation → `ambiguous`;
else `owner_confirmed`.

### Cancel (exactly once)

- Local player stops first (client), then the frame is sent.
- Server: reset playback boundary, cancel the respond task (pipeline) or
  `GeminiLiveBridge.interrupt_for_user` (live path — latch, one provider
  cancel, heard-position truncate, stale-PCM discard), abort ASR feed.
- A `barge_in` event (`reason: owner_cut_in_v2`) re-broadcasts so every
  surface agrees. Durable jobs (computer futures, delegate graph, code
  jobs) keep running; approval holds persist.
- Pipeline path emits an interrupted `ReplyEvent` claiming only the heard
  prefix (`generated_text` carries the full text for debugging).

## Eve cuts the owner (policy-gated floor take)

Triggers (`app/voice/live/eve_cutin.py`): `safety`, `timer_due`,
`urgent_alert`, `invited` (one-shot, armed by "stop me if…"),
`known_answer` (caller must prove grounding).

Gates: owner must hold the floor; `passive` denies non-safety;
`sad`/`frustrated` denies non-safety; 30 s cooldown (safety bypasses);
quiet hours deny non-emergencies. Unknown triggers deny.

Speech flows through the normal lane (bridge `speak_ack` / pipeline cue),
marked `eve_cut_in: true` + `cut_in_trigger` on `tts_chunk` (pipeline) and
`reply`, ledgered as `kind="cut_in"`, then the floor yields back to the
owner. Time-critical parked mail (`Callout` rows) attempts a cut-in when
the owner turn is the only blocker; ordinary lines stay parked.

## Floor (`floor` event)

`{type: "floor", floor, previous, reason}`. Values: `idle`,
`owner_holds`, `eve_holds`, `contested`, `yielding`. Emitted on change from
the engine tick drain on both brains.

## Gestures (`gesture` event)

`{type: "gesture", gesture, intensity, floor}`. Vocabulary:
`idle`, `listening`, `thinking`, `speaking`, `yielding`, `yield_back`,
`contested`, `urgent`; intensity `low`/`medium`/`high`. Emitted on change.
Mac modulates orb energy; PWA pulses living glass + haptics (`eveCutIn` on
urgent). Unknown values must be ignored by clients.

## Client checklist

- [ ] Stop local playback BEFORE sending `owner_evidence`.
- [ ] Read `audio_played_ms` BEFORE stopping (stops reset position).
- [ ] Resend/forward onset audio (preroll) so Eve hears the whole cut-in.
- [ ] Ignore unknown `gesture`/`floor` values (forward-compat).
- [ ] Reduced-motion: no gesture pulse, no contested animation.
- [ ] `owner_evidence` is dropped while paused/muted (server-side too).
