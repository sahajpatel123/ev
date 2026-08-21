"""Typed protocol events for the EV LIVE runtime.

The engine emits these; transports (WebSocket, and later others) serialize
them. Keeping them as dataclasses here means the runtime never depends on a
specific transport and the WebSocket protocol stays a thin mapping layer.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class LiveEvent:
    """Base class: every event carries its type name and a monotonic stamp."""

    type: str
    at_ms: int

    def as_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {"type": self.type, "at_ms": self.at_ms}
        for key, value in self.__dict__.items():
            if key in {"type", "at_ms"}:
                continue
            out[key] = value
        return out


@dataclass
class ReadyEvent(LiveEvent):
    """The live channel is open; the client may start streaming audio."""

    session_id: str | None = None
    conversation_id: str | None = None
    config: dict = field(default_factory=dict)

    def __init__(
        self,
        *,
        at_ms: int,
        session_id: str | None = None,
        conversation_id: str | None = None,
        config: dict | None = None,
    ) -> None:
        super().__init__("ready", at_ms)
        self.session_id = session_id
        self.conversation_id = conversation_id
        self.config = config or {}


@dataclass
class StateEvent(LiveEvent):
    """A snapshot of the continuous conversation state."""

    state: dict = field(default_factory=dict)

    def __init__(self, *, at_ms: int, state: dict) -> None:
        super().__init__("state", at_ms)
        self.state = state


@dataclass
class CameraRequestEvent(LiveEvent):
    """A target-bound request for a camera provider to resolve explicitly."""

    action: str
    device_id: str | None = None
    request_id: str | None = None
    duration_ms: int | None = None
    interval_ms: int | None = None
    max_frames: int | None = None
    detail: str | None = None

    def __init__(
        self,
        *,
        at_ms: int,
        action: str,
        device_id: str | None = None,
        request_id: str | None = None,
        duration_ms: int | None = None,
        interval_ms: int | None = None,
        max_frames: int | None = None,
        detail: str | None = None,
    ) -> None:
        super().__init__("camera_request", at_ms)
        self.action = action
        self.device_id = device_id
        self.request_id = request_id
        self.duration_ms = duration_ms
        self.interval_ms = interval_ms
        self.max_frames = max_frames
        self.detail = detail


@dataclass
class ComputerRequestEvent(LiveEvent):
    """Ask the connected Mac client to perform one structured computer action."""

    command: str
    request_id: str | None = None
    arguments: dict = field(default_factory=dict)
    device_id: str | None = None

    def __init__(
        self,
        *,
        at_ms: int,
        command: str,
        request_id: str | None = None,
        arguments: dict | None = None,
        device_id: str | None = None,
    ) -> None:
        super().__init__("computer_request", at_ms)
        self.command = command
        self.action = command
        self.request_id = request_id
        self.arguments = arguments or {}
        self.device_id = device_id


@dataclass
class ComputerStateEvent(LiveEvent):
    """Client-reported Mac control permission and foreground state."""

    computer_state: dict = field(default_factory=dict)

    def __init__(self, *, at_ms: int, computer_state: dict) -> None:
        super().__init__("computer_state", at_ms)
        self.computer_state = computer_state


@dataclass
class CameraStateEvent(LiveEvent):
    """Provider-reported camera state; clients may present this as fact."""

    camera_state: dict = field(default_factory=dict)

    def __init__(self, *, at_ms: int, camera_state: dict) -> None:
        super().__init__("camera_state", at_ms)
        self.camera_state = camera_state


@dataclass
class RealtimeDiagnosticsEvent(LiveEvent):
    """Safe provider/session diagnostics for developer-facing clients."""

    diagnostics: dict = field(default_factory=dict)

    def __init__(self, *, at_ms: int, diagnostics: dict) -> None:
        super().__init__("realtime_diagnostics", at_ms)
        self.diagnostics = diagnostics


@dataclass
class PartialTranscriptEvent(LiveEvent):
    """Incremental ASR hypothesis while the human is still speaking."""

    text: str
    sequence: int
    stable: bool = False
    confidence: float = 0.0

    def __init__(
        self,
        *,
        at_ms: int,
        text: str,
        sequence: int,
        stable: bool = False,
        confidence: float = 0.0,
    ) -> None:
        super().__init__("partial", at_ms)
        self.text = text
        self.sequence = sequence
        self.stable = stable
        self.confidence = confidence


@dataclass
class FinalTranscriptEvent(LiveEvent):
    """The user's completed turn as recognized by ASR."""

    text: str
    confidence: float = 1.0
    provider: str = "dev"
    transcript_source: str = "provider"

    def __init__(
        self,
        *,
        at_ms: int,
        text: str,
        confidence: float = 1.0,
        provider: str = "dev",
        transcript_source: str = "provider",
    ) -> None:
        super().__init__("final_transcript", at_ms)
        self.text = text
        self.confidence = confidence
        self.provider = provider
        self.transcript_source = transcript_source


@dataclass
class BackchannelEvent(LiveEvent):
    """The assistant is speaking a short listening cue ("Mhm.")."""

    text: str

    def __init__(self, *, at_ms: int, text: str) -> None:
        super().__init__("backchannel", at_ms)
        self.text = text


@dataclass
class BargeInEvent(LiveEvent):
    """The user started talking while the assistant was speaking — stop output."""

    reason: str = "user_speech"

    def __init__(self, *, at_ms: int, reason: str = "user_speech") -> None:
        super().__init__("barge_in", at_ms)
        self.reason = reason


@dataclass
class TtsChunkEvent(LiveEvent):
    """One playable spoken unit: start playing it now."""

    index: int
    text: str
    audio_b64: str | None = None
    audio_ref: str | None = None
    content_type: str | None = None
    duration_ms: int | None = None
    sample_rate: int | None = None
    provider: str = "dev"

    def __init__(
        self,
        *,
        at_ms: int,
        index: int,
        text: str,
        audio_b64: str | None = None,
        audio_ref: str | None = None,
        content_type: str | None = None,
        duration_ms: int | None = None,
        sample_rate: int | None = None,
        provider: str = "dev",
    ) -> None:
        super().__init__("tts_chunk", at_ms)
        self.index = index
        self.text = text
        self.audio_b64 = audio_b64
        self.audio_ref = audio_ref
        self.content_type = content_type
        self.duration_ms = duration_ms
        self.sample_rate = sample_rate
        self.provider = provider


@dataclass
class ReplyEvent(LiveEvent):
    """The full reply has landed (metadata; chunks already streamed)."""

    text: str
    conversation_id: str | None = None
    model: str | None = None
    context_tokens: int = 0
    style: dict = field(default_factory=dict)
    device_id: str | None = None
    tts_device_id: str | None = None

    def __init__(
        self,
        *,
        at_ms: int,
        text: str,
        conversation_id: str | None = None,
        model: str | None = None,
        context_tokens: int = 0,
        style: dict | None = None,
        device_id: str | None = None,
        tts_device_id: str | None = None,
    ) -> None:
        super().__init__("reply", at_ms)
        self.text = text
        self.conversation_id = conversation_id
        self.model = model
        self.context_tokens = context_tokens
        self.style = style or {}
        self.device_id = device_id
        self.tts_device_id = tts_device_id


@dataclass
class LatencyEvent(LiveEvent):
    """A measured latency boundary (TTFA / TTFW / TTCR)."""

    metric: str
    ms: int
    authorized_at_ms: int = 0

    def __init__(
        self,
        *,
        at_ms: int,
        metric: str,
        ms: int,
        authorized_at_ms: int = 0,
    ) -> None:
        super().__init__("latency", at_ms)
        self.metric = metric
        self.ms = ms
        self.authorized_at_ms = authorized_at_ms


@dataclass
class ConversationMovedEvent(LiveEvent):
    """This instance no longer owns audible assistant speech."""

    to_device_id: str | None = None
    reason: str = "lease"

    def __init__(
        self,
        *,
        at_ms: int,
        to_device_id: str | None = None,
        reason: str = "lease",
    ) -> None:
        super().__init__("conversation_moved", at_ms)
        self.to_device_id = to_device_id
        self.reason = reason
        self.code = "audio_owner_lost"


@dataclass
class ErrorEvent(LiveEvent):
    """A non-fatal error; the live channel stays open unless ``fatal``."""

    code: str
    message: str
    fatal: bool = False

    def __init__(
        self, *, at_ms: int, code: str, message: str, fatal: bool = False
    ) -> None:
        super().__init__("error", at_ms)
        self.code = code
        self.message = message
        self.fatal = fatal

    def as_dict(self) -> dict[str, Any]:
        out = super().as_dict()
        out["text"] = self.message
        return out


@dataclass
class HudEvent(LiveEvent):
    """A HUD card the client should render now (progress, evidence, hold)."""

    card: dict = field(default_factory=dict)
    kind: str = "card"

    def __init__(
        self,
        *,
        at_ms: int,
        card: dict | None = None,
        kind: str = "card",
    ) -> None:
        super().__init__("hud", at_ms)
        self.card = card or {}
        self.kind = kind

    def as_dict(self) -> dict[str, Any]:
        out = super().as_dict()
        out["hud"] = self.card
        return out
