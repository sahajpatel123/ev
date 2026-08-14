"""Declarative model registry: license-first, checksum-pinned downloads."""

from __future__ import annotations

import threading
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class RegistryError(ValueError):
    """Invalid registry state or model entry."""


class LicenseError(RegistryError):
    """A model without a license was refused registration."""


class ChecksumError(RegistryError):
    """A downloaded artifact failed its pinned sha256 check."""


class DiskGuardError(RegistryError):
    """A download was refused because free disk is below the minimum."""


class ModelNotFoundError(RegistryError, KeyError):
    """Requested model is not registered."""


class ModelTier(StrEnum):
    ALWAYS = "always"
    SYSTEM = "system"
    ON_DEMAND = "on_demand"
    EXCLUSIVE = "exclusive"


TIER_ORDER = (ModelTier.ALWAYS, ModelTier.SYSTEM, ModelTier.ON_DEMAND, ModelTier.EXCLUSIVE)


class ModelSpec(BaseModel):
    """A declarative model entry.

    ``license`` is mandatory: a model without a license cannot be registered.
    ``source_url`` may be present with ``sha256=None`` as an *unverified seed
    entry*; ``pull`` refuses to download until the checksum is pinned.
    """

    model_config = ConfigDict(validate_assignment=True)

    name: str = Field(min_length=1, pattern=r"^[a-zA-Z0-9][a-zA-Z0-9._-]*$")
    task: str = Field(min_length=1)
    source_url: str | None = None
    sha256: str | None = None
    disk_mb: int = Field(ge=0)
    resident_mb: int = Field(ge=0)
    peak_mb: int = Field(ge=0)
    tier: ModelTier
    license: str = Field(min_length=1)
    license_url: str | None = None
    version: str | None = None
    verified: bool = False
    # Optional models are registered and loadable, but are NOT pinned at boot
    # and are never reported as missing/required by `ml doctor`.
    optional: bool = False
    # Overrides the artifact filename inside the cache (default: name + URL
    # suffix). Used when an engine resolves a specific cache filename.
    target_name: str | None = None

    @field_validator("sha256")
    @classmethod
    def _normalize_sha256(cls, value: str | None) -> str | None:
        if value is None:
            return None
        digest = value.lower()
        if len(digest) != 64 or any(char not in "0123456789abcdef" for char in digest):
            raise ValueError("sha256 must be 64 lowercase hex characters")
        return digest

    @model_validator(mode="after")
    def _validate_entry(self) -> ModelSpec:
        if not self.license.strip():
            raise ValueError("license is required")
        if self.peak_mb < self.resident_mb:
            raise ValueError("peak_mb must be >= resident_mb")
        return self


class ModelRegistry:
    """Process-local registry with license and exclusivity enforcement."""

    def __init__(self, *, exclusive_limit_mb: int | None = None) -> None:
        self._specs: dict[str, ModelSpec] = {}
        self._lock = threading.RLock()
        self.exclusive_limit_mb = exclusive_limit_mb

    def register(self, spec: ModelSpec) -> ModelSpec:
        with self._lock:
            if not spec.license.strip():
                raise LicenseError(f"model {spec.name!r} has no license and cannot be registered")
            if spec.name in self._specs:
                raise RegistryError(f"model {spec.name!r} is already registered")
            if (
                spec.tier is ModelTier.EXCLUSIVE
                and self.exclusive_limit_mb is not None
                and spec.resident_mb > self.exclusive_limit_mb
            ):
                raise RegistryError(
                    f"exclusive model {spec.name!r} resident_mb={spec.resident_mb} "
                    f"exceeds exclusive limit {self.exclusive_limit_mb}MB"
                )
            self._specs[spec.name] = spec
            return spec

    def get(self, name: str) -> ModelSpec:
        with self._lock:
            try:
                return self._specs[name]
            except KeyError as exc:
                raise ModelNotFoundError(f"model {name!r} is not registered") from exc

    def names(self) -> list[str]:
        with self._lock:
            return sorted(self._specs)

    def all(self) -> list[ModelSpec]:
        with self._lock:
            return sorted(
                self._specs.values(),
                key=lambda spec: (TIER_ORDER.index(spec.tier), spec.name),
            )

    def by_tier(self, tier: ModelTier) -> list[ModelSpec]:
        with self._lock:
            return sorted(
                (spec for spec in self._specs.values() if spec.tier is tier),
                key=lambda spec: spec.name,
            )


def builtin_models() -> list[ModelSpec]:
    """Locked roster backing docs/MODEL_BUDGET.md.

    Entries with ``source_url`` but no ``sha256`` are seed entries: their
    checksums must be pinned (and ``verified=True``) before ``pull`` will
    download them.
    """

    return [
        ModelSpec(
            name="embed-all-minilm-l6-v2",
            task="embedding",
            source_url="https://huggingface.co/sentence-transformers/all-MiniLM-L6-v2/resolve/main/onnx/model.onnx",
            disk_mb=95,
            resident_mb=100,
            peak_mb=100,
            tier=ModelTier.ALWAYS,
            license="Apache-2.0",
            license_url="https://huggingface.co/sentence-transformers/all-MiniLM-L6-v2",
            version="v2",
            optional=True,
        ),
        ModelSpec(
            name="embed-granite-r2",
            task="embedding",
            source_url=(
                "https://huggingface.co/ibm-granite/granite-embedding-97m-multilingual-r2/"
                "resolve/main/onnx/model_quint8_avx2.onnx"
            ),
            sha256="a6022dd8220ea6f6595562a1328ee216f4a94faa55362f2f4747c80f1e78772e",
            disk_mb=100,
            resident_mb=460,
            peak_mb=520,
            tier=ModelTier.ON_DEMAND,
            license="Apache-2.0",
            license_url="https://huggingface.co/ibm-granite/granite-embedding-97m-multilingual-r2",
            version="r2",
            verified=True,
        ),
        ModelSpec(
            name="wake-evie-porcupine",
            task="wake_word",
            source_url=None,
            disk_mb=12,
            resident_mb=16,
            peak_mb=16,
            tier=ModelTier.ALWAYS,
            license="Apache-2.0 (Picovoice Porcupine; custom .ppn requires an access key)",
            license_url="https://github.com/Picovoice/porcupine",
            version="3.0",
            optional=True,
        ),
        ModelSpec(
            name="wake-openwakeword",
            task="wake_word",
            source_url=None,
            disk_mb=15,
            resident_mb=15,
            peak_mb=20,
            tier=ModelTier.ALWAYS,
            license="Apache-2.0 (openWakeWord custom EVIE head)",
            license_url="https://github.com/dscripka/openWakeWord",
            version="custom-evie-head",
        ),
        ModelSpec(
            name="vad-silero",
            task="voice_activity_detection",
            source_url="https://github.com/snakers4/silero-vad/raw/master/files/silero_vad.onnx",
            disk_mb=2,
            resident_mb=2,
            peak_mb=2,
            tier=ModelTier.ALWAYS,
            license="MIT",
            license_url="https://github.com/snakers4/silero-vad",
            version="v5",
        ),
        ModelSpec(
            name="speaker-ecapa",
            task="speaker_embedding",
            source_url=None,
            disk_mb=60,
            resident_mb=28,
            peak_mb=28,
            tier=ModelTier.ALWAYS,
            license="Apache-2.0 (SpeechBrain spkrec-ecapa-voxceleb)",
            license_url="https://huggingface.co/speechbrain/spkrec-ecapa-voxceleb",
            version="spkrec-ecapa-voxceleb",
            optional=True,
        ),
        ModelSpec(
            name="speaker-campp",
            task="speaker_embedding",
            source_url=(
                "https://huggingface.co/welcomyou/campplus-3dspeaker-200k-onnx/resolve/main/"
                "campplus_cn_en_common_200k.onnx"
            ),
            sha256="dd1740aa1e1ffa3895f96aef2166b8af2bb2ad09c00769dd275ee36aef6a2a7f",
            disk_mb=27,
            resident_mb=28,
            peak_mb=40,
            tier=ModelTier.ALWAYS,
            license="Apache-2.0 (CAM++ 7.2M, 3D-Speaker bilingual 200k)",
            license_url="https://github.com/modelscope/3D-Speaker",
            version="campp-7.2m-zh-en-200k",
            verified=True,
            target_name="speaker-campp",
        ),
        ModelSpec(
            name="liveness-audio",
            task="liveness",
            source_url=None,
            disk_mb=2,
            resident_mb=2,
            peak_mb=2,
            tier=ModelTier.ALWAYS,
            license="MIT",
            license_url=None,
            version="1",
        ),
        ModelSpec(
            name="scene-yamnet",
            task="audio_scene",
            source_url=None,
            disk_mb=20,
            resident_mb=17,
            peak_mb=17,
            tier=ModelTier.ALWAYS,
            license="Apache-2.0 (YAMNet weights)",
            license_url="https://github.com/tensorflow/models/tree/master/research/audioset/yamnet",
            version="1",
        ),
        ModelSpec(
            name="asr-faster-whisper-tiny",
            task="asr",
            source_url=None,
            disk_mb=80,
            resident_mb=75,
            peak_mb=75,
            tier=ModelTier.ON_DEMAND,
            license="MIT (faster-whisper)",
            license_url="https://github.com/SYSTRAN/faster-whisper",
            version="tiny",
        ),
        ModelSpec(
            name="asr-faster-whisper-base",
            task="asr",
            source_url=None,
            disk_mb=150,
            resident_mb=145,
            peak_mb=145,
            tier=ModelTier.ON_DEMAND,
            license="MIT (faster-whisper)",
            license_url="https://github.com/SYSTRAN/faster-whisper",
            version="base",
        ),
        ModelSpec(
            name="tts-piper-en-lessac-medium",
            task="tts",
            source_url="https://huggingface.co/rhasspy/piper-voices/resolve/v1.0.0/en/en_US/lessac/medium/en_US-lessac-medium.onnx",
            disk_mb=65,
            resident_mb=60,
            peak_mb=60,
            tier=ModelTier.ON_DEMAND,
            license="MIT (Piper voice)",
            license_url="https://github.com/rhasspy/piper",
            version="en_US-lessac-medium",
        ),
        ModelSpec(
            name="tts-kokoro-82m-int8",
            task="tts",
            source_url=(
                "https://github.com/thewh1teagle/kokoro-onnx/releases/download/"
                "model-files-v1.0/kokoro-v1.0.int8.onnx"
            ),
            sha256="6e742170d309016e5891a994e1ce1559c702a2ccd0075e67ef7157974f6406cb",
            disk_mb=92,
            resident_mb=100,
            peak_mb=140,
            tier=ModelTier.ON_DEMAND,
            license="Apache-2.0 (Kokoro-82M)",
            license_url="https://github.com/hexgrad/kokoro",
            version="kokoro-v1.0-int8",
            verified=True,
        ),
        ModelSpec(
            name="tts-kokoro-82m-fp16",
            task="tts",
            source_url=(
                "https://github.com/thewh1teagle/kokoro-onnx/releases/download/"
                "model-files-v1.0/kokoro-v1.0.fp16.onnx"
            ),
            sha256="c1610a859f3bdea01107e73e50100685af38fff88f5cd8e5c56df109ec880204",
            disk_mb=177,
            resident_mb=190,
            peak_mb=240,
            tier=ModelTier.ON_DEMAND,
            license="Apache-2.0 (Kokoro-82M)",
            license_url="https://github.com/hexgrad/kokoro",
            version="kokoro-v1.0-fp16",
            verified=True,
        ),
        ModelSpec(
            name="tts-kokoro-voices-v1.0",
            task="tts_voices",
            source_url=(
                "https://github.com/thewh1teagle/kokoro-onnx/releases/download/"
                "model-files-v1.0/voices-v1.0.bin"
            ),
            sha256="bca610b8308e8d99f32e6fe4197e7ec01679264efed0cac9140fe9c29f1fbf7d",
            disk_mb=28,
            resident_mb=28,
            peak_mb=28,
            tier=ModelTier.ON_DEMAND,
            license="Apache-2.0 (Kokoro-82M)",
            license_url="https://github.com/hexgrad/kokoro",
            version="voices-v1.0",
            verified=True,
            # Engine-neutral: shared by every Kokoro precision (int8/fp16/fp32).
            target_name="tts-kokoro.voices",
        ),
        ModelSpec(
            name="llm-mlx-3b",
            task="local_llm",
            source_url=None,
            disk_mb=2200,
            resident_mb=2000,
            peak_mb=2400,
            tier=ModelTier.EXCLUSIVE,
            license="Llama 3.2 Community License (or per-model license)",
            license_url="https://ai.meta.com/llama/license/",
            version="3b-4bit",
            optional=True,
        ),
        # CORTEX (Agent 10): offline reasoning/classification brain.
        # Qwen3-1.7B Q4 via Ollama (~1.1 GB). Exclusive tier: evicts on-demand
        # models and takes the arbiter global lock when acquired. sha256 is
        # pinned on first verified `ollama pull qwen3:1.7b` (Ollama registry).
        ModelSpec(
            name="qwen3-1.7b",
            task="local_llm",
            source_url=None,
            disk_mb=1100,
            resident_mb=1000,
            peak_mb=1200,
            tier=ModelTier.EXCLUSIVE,
            license="Apache-2.0 (Qwen3)",
            license_url="https://huggingface.co/Qwen/Qwen3-1.7B",
            version="1.7b-q4",
            optional=True,
        ),
        ModelSpec(
            name="trainer-mlx-lora",
            task="trainer",
            source_url=None,
            disk_mb=2400,
            resident_mb=2000,
            peak_mb=3500,
            tier=ModelTier.EXCLUSIVE,
            license="Llama 3.2 Community License (or per-model license)",
            license_url="https://ai.meta.com/llama/license/",
            version="mlx-tune",
            optional=True,
        ),
        # AGENT 7 ROSTER — OpenCV Zoo SFace ONNX (Apache-2.0, LFW 0.9940).
        # sha256 pinned from Hugging Face LFS oid (2021dec, 38,696,353 bytes).
        ModelSpec(
            name="face-sface",
            task="face_embedding",
            source_url="https://huggingface.co/opencv/face_recognition_sface/resolve/main/face_recognition_sface_2021dec.onnx",
            sha256="0ba9fbfa01b5270c96627c4ef784da859931e02f04419c829e83484087c34e79",
            disk_mb=37,
            resident_mb=37,
            peak_mb=90,
            tier=ModelTier.ON_DEMAND,
            license="Apache-2.0",
            license_url="https://huggingface.co/opencv/face_recognition_sface",
            version="opencv_zoo face_recognition_sface_2021dec",
            verified=True,
        ),
    ]
