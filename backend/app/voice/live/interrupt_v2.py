"""Owner voice cut-in fusion (Interrupt V2) for the EV LIVE runtime.

Interrupt V1 (``interrupt_v1.py``) is dead, legacy, and unwired: it needed a
local mel-template spotter, an analysis side channel, and calibration data
that never shipped. V2 takes a different, shippable path:

- the **client** owns near-end evidence. It watches its own microphone tap
  while Eve's audio plays, with echo cancellation active, and reports a
  small evidence frame (speech duration, VAD confidence, AEC state, and an
  optional self-similarity score against the audio it is playing);
- the **server** fuses that evidence with what it knows (is Eve actually
  speaking? is this response already cancelled?) and returns one verdict.

Only ``OWNER_CONFIRMED`` may interrupt. ``AMBIGUOUS`` never interrupts —
not-confidently-self does not mean owner. ``SELF`` (echo) and ``IGNORED``
(Eve is not speaking, so there is nothing to cut) are ordinary outcomes.

Laws carried forward from the V1 closure:

- provider mic forwarding stays blocked during playback; the evidence frame
  is metadata, never the provider input path;
- no fixed-RMS primary gate: microphone energy is a sanity feature only
  (absurd values downgrade to ambiguous, sane values never confirm alone);
- exactly-once execution is delegated to
  ``GeminiLiveBridge.interrupt_for_user`` (latch, one provider cancel,
  heard-position truncate, stale-PCM discard); the :class:`InterruptLatch`
  here is the pipeline-path twin so both brains share one contract;
- local speaker silence happens first (the client stops its player before
  sending evidence; the server re-broadcasts ``barge_in`` so every surface
  agrees).

This module is pure: no I/O, no numpy, no threads. It imports safely on a
bare install so offline CI and the deterministic doubles keep working.
"""

from __future__ import annotations

from dataclasses import dataclass, field

#: Fusion verdicts.
OWNER_CONFIRMED = "owner_confirmed"
AMBIGUOUS = "ambiguous"
SELF = "self"
IGNORED = "ignored"

#: Minimum contiguous near-end speech before evidence can confirm (mirrors
#: ``EV_VOICE_LIVE_MIN_SPEECH_MS`` default; the client enforces its own
#: cadence and the server re-checks here so old clients cannot lower it).
MIN_SPEECH_MS = 160

#: Minimum client VAD confidence before evidence can confirm.
MIN_CONFIDENCE = 0.5

#: Client self-similarity at or above this means "I am hearing Eve's own
#: audio back" — verdict SELF, never an interruption.
SELF_ECHO_SCORE = 0.5

#: Microphone RMS sanity window (see module docstring: sanity only).
SANITY_MIC_RMS_RANGE = (0.004, 0.6)


@dataclass(frozen=True)
class OwnerEvidence:
    """One client-reported near-end speech evidence frame."""

    speech_ms: int = 0
    confidence: float = 0.0
    aec_active: bool = False
    playback_active: bool = False
    audio_played_ms: int | None = None
    preroll_ms: int | None = None
    echo_score: float | None = None
    mic_rms: float | None = None
    response_id: str | None = None


@dataclass(frozen=True)
class OwnerVerdict:
    """The fusion outcome for one evidence frame."""

    verdict: str
    reason: str

    @property
    def confirmed(self) -> bool:
        return self.verdict == OWNER_CONFIRMED


def _optional_int(value: object) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return max(0, int(value))
    return None


def _optional_float(value: object) -> float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    return None


def _optional_str(value: object) -> str | None:
    if isinstance(value, str) and value.strip():
        return value.strip()
    return None


def parse_owner_evidence(message: dict | None) -> OwnerEvidence:
    """Parse an untrusted client ``owner_evidence`` frame defensively."""

    payload = message if isinstance(message, dict) else {}
    return OwnerEvidence(
        speech_ms=_optional_int(payload.get("speech_ms")) or 0,
        confidence=_optional_float(payload.get("confidence")) or 0.0,
        aec_active=bool(payload.get("aec_active", False)),
        playback_active=bool(payload.get("playback_active", False)),
        audio_played_ms=_optional_int(payload.get("audio_played_ms")),
        preroll_ms=_optional_int(payload.get("preroll_ms")),
        echo_score=_optional_float(payload.get("echo_score")),
        mic_rms=_optional_float(payload.get("mic_rms")),
        response_id=_optional_str(
            payload.get("response_id") or payload.get("provider_response_id")
        ),
    )


def fuse_owner_evidence(
    evidence: OwnerEvidence, *, eve_speaking: bool
) -> OwnerVerdict:
    """Fuse one evidence frame into an interruption verdict.

    ``eve_speaking`` is server truth (a response is open / playback is
    active). The client can only report what its microphone hears; the
    server decides whether that constitutes cutting Eve off.
    """

    if not eve_speaking:
        return OwnerVerdict(IGNORED, "eve_not_speaking")
    if evidence.echo_score is not None and evidence.echo_score >= SELF_ECHO_SCORE:
        return OwnerVerdict(SELF, "echo_self_similarity")
    if evidence.playback_active and not evidence.aec_active:
        # Echo-unsafe: without AEC the mic cannot distinguish Eve's speaker
        # from the owner's voice during playback.
        return OwnerVerdict(AMBIGUOUS, "playback_without_aec")
    if evidence.speech_ms < MIN_SPEECH_MS:
        return OwnerVerdict(AMBIGUOUS, "speech_too_short")
    if evidence.confidence < MIN_CONFIDENCE:
        return OwnerVerdict(AMBIGUOUS, "confidence_too_low")
    if evidence.mic_rms is not None:
        low, high = SANITY_MIC_RMS_RANGE
        if not (low <= evidence.mic_rms <= high):
            return OwnerVerdict(AMBIGUOUS, "mic_rms_out_of_range")
    return OwnerVerdict(OWNER_CONFIRMED, "owner_cut_in")


@dataclass
class InterruptLatch:
    """Exactly-once guard so one response is cancelled at most once.

    The Gemini bridge owns its own latch (``_interrupt_in_flight`` plus the
    cancelled-response set); this is the pipeline-path twin with the same
    contract: ``claim()`` returns True exactly once per response id.
    """

    _claimed: set[str] = field(default_factory=set)
    _limit: int = 32

    def claim(self, response_id: str | None) -> bool:
        key = (response_id or "").strip() or "<open-response>"
        if key in self._claimed:
            return False
        self._claimed.add(key)
        while len(self._claimed) > self._limit:
            self._claimed.pop()
        return True

    def reset(self) -> None:
        self._claimed.clear()
