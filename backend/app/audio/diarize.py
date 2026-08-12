"""Optional, on-demand speaker diarization for meeting recordings only.

pyannote.audio 3.1 is deliberately never resident: the package and model are
loaded lazily, run for a single meeting file, and released. Diarization is
gated behind explicit consent (``EV_EARS_DIARIZE_CONSENT=true``) and is out of
scope for live ambient audio — it exists for recordings the human explicitly
selects.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


class DiarizationConsentError(RuntimeError):
    """Explicit consent is required before running diarization."""


class DiarizationUnavailableError(RuntimeError):
    """pyannote.audio or its model is not installed/configured."""


@dataclass
class SpeakerTurn:
    start_s: float
    end_s: float
    speaker: str


def _check_consent(consent: bool) -> None:
    if not consent:
        raise DiarizationConsentError(
            "Speaker diarization requires explicit consent "
            "(set EV_EARS_DIARIZE_CONSENT=true for a specific meeting recording). "
            "It never runs on live ambient audio."
        )


def diarize_meeting(
    audio_path: str | Path,
    *,
    consent: bool = False,
    hf_token: str | None = None,
    max_duration_s: float = 3600.0,
    pipeline_factory=None,
) -> list[SpeakerTurn]:
    """Run pyannote 3.1 on one meeting recording (blocking, on-demand).

    ``pipeline_factory`` is injectable for tests; the real path imports
    ``pyannote.audio.Pipeline`` and requires the model's Hugging Face token.
    """

    _check_consent(consent)
    path = Path(audio_path)
    if not path.is_file():
        raise DiarizationUnavailableError(f"meeting audio not found: {path}")
    if pipeline_factory is not None:
        pipeline = pipeline_factory()
    else:
        try:
            from pyannote.audio import Pipeline
        except ImportError as exc:
            raise DiarizationUnavailableError(
                "pyannote.audio is not installed (Agent 2 dependency request). "
                "Diarization stays disabled until then."
            ) from exc
        if not hf_token:
            raise DiarizationUnavailableError(
                "pyannote speaker-diarization-3.1 requires a Hugging Face token; "
                "pass EV_EARS_DIARIZE_HF_TOKEN for the selected recording."
            )
        pipeline = Pipeline.from_pretrained(
            "pyannote/speaker-diarization-3.1", use_auth_token=hf_token
        )
    diarization = pipeline(str(path))
    turns: list[SpeakerTurn] = []
    for turn, _, speaker in diarization.itertracks(yield_label=True):
        start = float(turn.start)
        end = float(turn.end)
        if end - start > max_duration_s:
            end = start + max_duration_s
        turns.append(SpeakerTurn(start_s=round(start, 3), end_s=round(end, 3), speaker=str(speaker)))
    return turns


def diarize_meeting_configured(
    audio_path: str | Path,
    *,
    hf_token: str | None = None,
    max_duration_s: float = 3600.0,
    pipeline_factory=None,
) -> list[SpeakerTurn]:
    """Run diarization using configured consent/token (see docs/AUDIO.md)."""

    from app.config import settings

    return diarize_meeting(
        audio_path,
        consent=settings.ears_diarize_consent,
        hf_token=hf_token or settings.ears_diarize_hf_token,
        max_duration_s=max_duration_s,
        pipeline_factory=pipeline_factory,
    )
