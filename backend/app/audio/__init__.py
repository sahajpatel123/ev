"""Local audio understanding for the always-on ears process.

Capture (mic → PCM16 ring), VAD (Silero ONNX or energy double), wake (voice
layer), scene (YAMNet or VAD features), and optional on-demand diarization.
Raw audio is never persisted by default.
"""

from app.audio.capture import (
    MicrophoneDeniedError,
    MicrophoneStream,
    MicrophoneUnavailableError,
    list_input_devices,
    pcm_to_wav_bytes,
)
from app.audio.ring import PCM16RingBuffer
from app.audio.scene import (
    YamNetSceneClassifier,
    classify_wav,
    classify_wav_vad_features,
    default_scene_classifier,
    set_scene_classifier,
)
from app.audio.vad import (
    EnergyVad,
    SileroVadOnnx,
    VadEngine,
    VadSegment,
    default_vad_engine,
    segment_utterances,
)

__all__ = [
    "EnergyVad",
    "MicrophoneDeniedError",
    "MicrophoneStream",
    "MicrophoneUnavailableError",
    "PCM16RingBuffer",
    "SileroVadOnnx",
    "VadEngine",
    "VadSegment",
    "YamNetSceneClassifier",
    "classify_wav",
    "classify_wav_vad_features",
    "default_scene_classifier",
    "default_vad_engine",
    "list_input_devices",
    "pcm_to_wav_bytes",
    "segment_utterances",
    "set_scene_classifier",
]
