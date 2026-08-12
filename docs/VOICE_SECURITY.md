# VOICE SECURITY — Sentry (Agent 5)

**Owner:** Agent 5 (SENTRY) — `backend/app/voice/{speaker,anti_spoof,security,sensitive}.py`

This document covers the threat model for EVIE's owner-only voice gate, the
speaker-verification engine choice, threshold calibration, passive liveness,
replay protection, and what an attacker can and cannot do with a recording of
the owner's voice.

## 1. Threat model

The voice gate protects a privileged owner session (`voice.wake →
voice.verify → awake → utterance`). The attacks it must resist, in order of
likelihood:

| Attack | Vector | Countermeasure |
| --- | --- | --- |
| Recording replay | Playback of the owner's own enrollment or challenge audio | Single-use nonce; server-side audio SHA-256 replay window; passive liveness model; transcript-bound challenge |
| Loudspeaker replay of challenge phrase | A stranger plays the owner's recording of the issued challenge phrase | Same as above; the fingerprint of the exact bytes is rejected on the second use, and the liveness model scores the acoustic path |
| Text-to-speech / voice conversion | Synthesized owner voice | ASVspoof-style audio-liveness model (2 MB `liveness-audio` slot) |
| Hash-double confusion | Operator believes the dev/test hash "embedding" is a security control | Production refuses to start the voice path when `EV_VOICEPRINT_PROVIDER` resolves to the test double |
| Client-side hash forging | Client sends a fresh `audio_sha256` to evade replay detection | Fingerprints are computed server-side; client hashes are advisory and ignored in production |
| Echoed challenge phrase | Client echoes the expected phrase without speaking it | Challenge phrase is checked against the ASR transcript of the submitted audio, not an echoed string |
| Arbitrary file read | Client passes an `audio_ref` pointing at `/etc/passwd` or a symlink | `audio_ref` reads are disabled unless an explicit allowlist (`EV_VOICE_AUDIO_ALLOWED_DIRS`) is set; every path is resolved and containment-checked |
| Database theft | Attacker steals encrypted voiceprint rows | Fernet + scrypt-derived key (`security.py`); no plaintext biometric material at rest; fails closed on bad key |

What an attacker with a recording of the owner's voice **can** do: nothing
that opens a session, provided the production path is used (CAM++/SpeechBrain
/HTTP encoder + liveness model + server-side fingerprints + transcript
binding). The voiceprint itself may still match a recording, which is exactly
why the other layers exist — replay of the same audio is rejected by
fingerprint, and re-recording through a loudspeaker is scored by the liveness
model.

What an attacker **cannot** do: start a privileged session from a recording
replay, mint a fresh client hash to evade the replay window, echo the
challenge phrase without audio, read arbitrary files through `audio_ref`, or
select the hash test double in a production config.

## 2. Speaker engine: CAM++ (recommended)

CAM++ (7.2M params) was chosen over ECAPA-TDNN and ERes2Net-base for the
28 MB always-resident speaker slot:

| Model | Params | VoxCeleb1-O EER | License |
| --- | ---: | ---: | --- |
| **CAM++** (selected) | 7.2 M | **0.65%** | Apache-2.0 |
| ERes2Net-base | 6.61 M | 0.84% | Apache-2.0 |
| ECAPA-TDNN (SpeechBrain `spkrec-ecapa-voxceleb`) | 20.8 M | ~0.86–1.45% depending on training/recipe | Apache-2.0 |

Sources: ModelScope/3D-Speaker leaderboard (CAM++ 0.65%, ERes2Net-base
0.84%) and the ECAPA-TDNN VoxCeleb1-O reports (0.86% unofficial reimpl,
~0.96–1.45% SpeechBrain-recipe results). CAM++ is both smaller and more
accurate, and the 192-dim embedding matches the existing voiceprint schema, so
**only the encoder changes** — voiceprint storage, dimension, and Fernet
encryption are untouched.

The ONNX export (16 kHz input, 192-dim output) is loaded through the
ModelArbiter under the locked 28 MB `speaker-ecapa`/`speaker-campp` entry —
never outside it. `onnxruntime` comes from the `ml` extra (dependency request
to Agent 2; the `ml` extra is now installed in the backend venv). When the
weights file is absent the engine fails closed in production; under pytest it
degrades to the deterministic test double and sets `degraded=true`.

**Privacy rationale — keep CAM++ local.** The fleet's reasoning/LLM layer is
moving to a hosted API because the M2/8 GB Mac cannot run local LLM inference,
but the voiceprint domain must **not** follow. CAM++ is 28 MB and runs fully
on-device: enrollment audio and embeddings never leave the machine, there is
no third-party dependency in the verify path, and latency stays local.
`HttpSpeakerVerifier` remains available only as an option for a
**self-hosted** encoder service behind the regional remote-processing gate —
it is not the recommendation and never the default.

## 3. Threshold calibration (EER + FAR=0)

The old hardcoded `0.82` in the test double is **deleted**. The shipped
threshold comes from
`calibrate_operating_point(owner_scores, impostor_scores)` in
`backend/app/voice/speaker.py`, which returns the EER, the EER threshold, the
**highest TAR operating point with FAR = 0**, and the ROC curve.

Runbook (requires the human approvals in §8 and Agent 2's VoxCeleb subset):

```bash
# One command: capture (or point at) owner WAVs, score >=50 impostor clips,
# compute EER + the FAR=0 operating point, emit the ROC, write the artifact,
# and print the threshold to ship:
cd backend
EV_VAULT_KEY=... uv run python -m app.voice.speaker capture --out-dir owner_wavs/
EV_VAULT_KEY=... uv run ev-eval speaker \
  --owner-dir owner_wavs/ --impostor-dir voxceleb_subset/ --roc-out roc.csv
#   -> writes backend/eval/ml/speaker_security.json and prints SHIPPED_THRESHOLD
# 2. Ship the printed threshold:
export EV_VOICEPRINT_THRESHOLD=<SHIPPED_THRESHOLD>
# 3. Or calibrate from precomputed score CSVs:
EV_VAULT_KEY=... uv run python -m app.voice.speaker owner_scores.csv impostor_scores.csv
```

The direct module command works identically and can write the artifact itself:

```bash
uv run python -m app.voice.speaker eval \
  --owner-dir owner_wavs/ --impostor-dir voxceleb_subset/ \
  --roc-out roc.csv --report canonical
```

The artifact is `ev.speaker.eval.v1` and is consumed by `ev-eval speaker`
(Agent 2) and Agent 20's `eval_gates` (`backend/app/scripts/eval_gates.py`),
which enforces **EER ≤ 3%** and **false_accepts_at_threshold == 0**. A
degraded (test-double) artifact is recorded honestly with `degraded: true`
and the gates SKIP it instead of treating it as measured.

`eval --test-double` runs the same harness against the deterministic byte
fingerprint for a dry run; its warning is explicit that the resulting
threshold is **not** a production threshold.

## 3.1 Guided enrollment capture (shared with Agent 3)

The owner never reads documentation. One command records and validates the
enrollment set:

```bash
uv run python -m app.voice.speaker capture --out-dir owner_wavs/
```

It reuses Agent 3's ears capture layer (`app.audio.capture.MicrophoneStream`,
16 kHz mono int16) — the same capture contract as VAD/wake — and walks the
owner through **varied phrasing and distance**:

| # | Phrase | Distance/volume |
| --- | --- | --- |
| 1 | the sun rises in the east | normal voice, arm's length |
| 2 | my favorite color is blue | from across the room |
| 3 | I am speaking to EVIE | quiet voice, close to the mic |
| 4 | tomorrow is another day | normal voice, arm's length |
| 5 | coffee before everything | from across the room |
| 6 | the password is safe with me | louder voice, far from the mic |

Each sample is validated before it is kept: 16 kHz, mono, 16-bit PCM, ≥2 s,
and audible RMS; a rejected sample prompts a retry. Already-recorded WAVs can
be checked the same way without a microphone:

```bash
uv run python -m app.voice.speaker capture --from-dir /path/to/recordings
```

This is the single capture UX: Agent 3's ears process records the same
16 kHz mono PCM16 contract, and any enrollment pipeline consumes the same
WAV files.

**Honest status:** the calibration has not been measured yet on this machine —
it requires the human-approved enrollment recordings, the loudspeaker replay
test, and the VoxCeleb eval download, none of which exist in the offline
worktree today. The current `EV_VOICEPRINT_THRESHOLD` default is `0.72`
(config), and the acceptance target is: **EER ≤ 3%**, **zero false accepts at
the shipped threshold**, and **0 accepts on 20 loudspeaker replays** of the
owner's own enrollment audio. Those numbers will be published here as soon as
the approvals land.

## 4. Anti-spoofing (production posture)

`backend/app/voice/anti_spoof.py`:

1. **Server-side fingerprints.** `compute_audio_sha256()` hashes the submitted
   audio bytes on the server. `ReplayGuard.fingerprint_replayed()` accepts only
   `AudioFingerprint` objects (`server_computed=True`); plain client strings
   are ignored in production. The dev/test path keeps string behavior only
   while pytest is running.
2. **Passive liveness.** `AudioLivenessModel` loads the 2 MB `liveness-audio`
   ONNX entry through the arbiter and returns a live probability. No model →
   the gate **fails closed** (`degraded`, reject). `liveness_proof` and
   `live_score` are advisory only — they can never pass a strict check.
3. **Transcript-bound challenge.** The issued phrase is compared against the
   ASR transcript of the submitted audio (`transcript_matches_expected`), or
   an injected `asr` transcriber is called on the audio. An echoed phrase
   without audio/transcript fails.
4. **Single-use nonces** (`ReplayGuard.issue/consume`) with purpose, session
   binding, and expiry — unchanged and still real.

> MiniFASNet is a **face** anti-spoofing model (~98% on face datasets); it does
> not apply to voice. The voice countermeasure is the ASVspoof-style audio
> model in the 2 MB `liveness-audio` slot.

## 5. File access security

`sample_audio_bytes()` never follows a client-controlled absolute path. An
`audio_ref` is read only when:

* `EV_VOICE_AUDIO_ALLOWED_DIRS` is set to a colon-separated allowlist of
  directories, **and**
* the resolved path (after `..` and symlink expansion) is inside one of those
  roots, **and**
* it is a regular readable file.

Remote `http(s)://` refs are refused for speaker verification.

## 6. HTTP provider: no more silent fallback

`EV_VOICEPRINT_PROVIDER=http` now constructs a real `HttpSpeakerVerifier`
client (`POST {EV_VOICEPRINT_BASE_URL}/v1/embed`). It is refused unless
`EV_ALLOW_REMOTE_VOICEPRINT_PROCESSING=true` (regional policy), so the
transparency disclosure ("remote destination") matches reality. Unset /
`hash` providers are refused outside pytest with a clear error.

## 7. Test mode vs production

`is_test_runtime()` returns true only while pytest is executing
(`PYTEST_CURRENT_TEST`). In that mode the deterministic test double is
selectable and `LivenessGate`/`ReplayGuard` keep their legacy semantics so the
offline suite is green. Outside pytest:

* hash provider → `RuntimeError` (refuses to start the voice path);
* liveness without audio/model/transcript → reject;
* client `audio_sha256` → ignored for replay detection.

## 8. Human approvals requested (blocking the measured gates)

1. **5+ owner enrollment samples** (16 kHz mono WAV) to calibrate and to test
   loudspeaker replay.
2. **Permission to play 20 loudspeaker replay attacks** of the owner's own
   enrollment audio against the live system.
3. **Consent for the impostor eval subset download** (VoxCeleb1-O cleaned
   trials, eval-only, research license — registered by Agent 2).

## 8.1 Physical replay test (the attack that matters)

Once the owner's samples exist and the liveness model is resident, run the
acceptance test exactly as the attack would happen:

```bash
uv run python -m app.voice.speaker replay-test \
  --api-url http://127.0.0.1:8000 \
  --api-key <device token> \
  --enrollment-wav owner-01.wav \
  --rounds 20
```

The runner wakes a fresh session, plays the owner's own enrollment audio
through the **loudspeaker**, and attempts verification 20 times. Requirement:
**0 accepts** (`REPLAY_ACCEPTS=0`, exit 0). Each attempt must be rejected by
the replay fingerprint, the passive-liveness model, or the transcript/
challenge binding — and no session may reach `awake`. `--no-playback` is an
API-only rehearsal that skips the speaker.

The result (20/20 rejected) is reported with the EER/ROC numbers. Until the
human grants replay permission, this test is not run; synthetic tests
deliberately cannot prove it.

## 9. Dependency notes (fleet)

* **Agent 2:** pin and register the CAM++ ONNX export (or repoint
  `speaker-ecapa` to it) with a sha256; pin the 2 MB `liveness-audio`
  ASVspoof-style model. (`ev-eval speaker` integration is done: it calls
  `python -m app.voice.speaker eval` and writes `eval/ml/speaker_security.json`.)
* **Agent 3:** the guided enrollment recorder reuses `app.audio.capture`
  (`MicrophoneStream` / `pcm_to_wav_bytes`) so there is one 16 kHz mono PCM16
  capture contract. No second capture implementation exists.
* **Agent 4 (`lifecycle.py`) / Agent 14 (`runtime.py`):** pass the submitted
  audio (`audio_b64`/`audio_ref`) and the ASR transcript (or the runtime's
  `transcriber`) into `LivenessGate.check()` / `check_with_evidence()`, and
  log `evidence.audio_sha256` instead of the client field. Until then the
  strict gate rejects verification in production (fail-closed by design).
* **Agent 19:** assert the identity matrix — an unverified voice cannot mint
  re-verification proofs or reach owner-level operations.

## 10. Tests

* `tests/test_speaker_verification.py` — production refusal, CAM++/HTTP fake
  engines, degraded test-double path, SpeechBrain real-decode skips,
  allowlist, calibration, one-command artifact writing, guided capture.
* `tests/test_anti_spoof.py` — server fingerprints, strict liveness, transcript
  binding, replay trust boundary, audio-liveness model wiring.

Real-weight tests skip cleanly when the dependency/model is absent (offline CI
law), and at least one test exercises each real factory entry point with an
injected engine. With `onnxruntime` installed, the fail-closed paths (missing
model file → refused; liveness model unavailable → reject) run against the
real runtime library.
