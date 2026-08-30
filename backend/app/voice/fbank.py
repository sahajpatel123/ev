"""Kaldi-compatible 80-dim fbank for CAM++ speaker embeddings.

The community CAM++ ONNX exports (3D-Speaker / WeSpeaker) do not include a
waveform frontend. They expect 80-bin log-mel filterbanks at 16 kHz with
per-utterance mean normalization, matching ``torchaudio.compliance.kaldi.fbank``
/ ``kaldi_native_fbank.OnlineFbank``.
"""

from __future__ import annotations

from collections.abc import Sequence


def kaldi_fbank(
    waveform: Sequence[float],
    *,
    sample_rate: int = 16000,
    num_mel_bins: int = 80,
    dither: float = 0.0,
) -> list[list[float]]:
    """Return ``[T, 80]`` CMN log-fbank frames from a 16 kHz mono waveform."""

    if sample_rate != 16000:
        raise ValueError(f"CAM++ fbank requires 16 kHz audio, got {sample_rate}")
    if not waveform:
        raise ValueError("audio contains no samples")
    try:
        import kaldi_native_fbank as knf
        import numpy as np
    except ImportError as exc:
        raise RuntimeError(
            "kaldi-native-fbank and numpy are required for CAM++ fbank features; "
            "install the ml extra"
        ) from exc

    audio = np.asarray(list(waveform), dtype=np.float32).reshape(-1)
    opts = knf.FbankOptions()
    opts.frame_opts.samp_freq = float(sample_rate)
    opts.frame_opts.dither = float(dither)
    opts.mel_opts.num_bins = int(num_mel_bins)
    extractor = knf.OnlineFbank(opts)
    # 3D-Speaker scales float32 [-1, 1] into the int16 range before fbank.
    extractor.accept_waveform(sample_rate, (audio * 32768.0).tolist())
    extractor.input_finished()
    if extractor.num_frames_ready <= 0:
        raise ValueError("audio is too short for CAM++ fbank (need ~25 ms)")
    frames = np.stack(
        [extractor.get_frame(index) for index in range(extractor.num_frames_ready)]
    ).astype(np.float32)
    frames -= frames.mean(axis=0, keepdims=True)
    return frames.tolist()
