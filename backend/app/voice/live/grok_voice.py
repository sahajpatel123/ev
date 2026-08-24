"""Realtime speech-to-speech brain for EV LIVE.

Live talk is not a chat-completions model. EV.app still talks
``WS /v1/voice/live`` (16 kHz PCM); this module is the upstream socket:

- OpenAI Realtime (``gpt-realtime-2.1-mini`` at 24 kHz, resampled here)
- xAI Grok Voice Think Fast 2.0 (16 kHz native)

Typed chat stays on ``EV_CHAT_PROVIDER``.
"""

from __future__ import annotations

import array
import asyncio
import base64
import contextlib
import hashlib
import json
import logging
import time
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any
from urllib.parse import urlencode
from uuid import uuid4

from app.config import settings
from app.ev.camera_runtime import (
    build_realtime_image_item,
    log_camera,
    pop_observations,
)
from app.ev.computer_strategy import (
    REQUIRED_COMPUTER_TOOLS,
    evaluate_provider_computer_schema,
    looks_like_computer_task,
)
from app.ev.tool_select import LIVE_VOICE_TOOLS
from app.voice.live.barge_in import (
    delivered_assistant_text,
    generated_duration_ms,
)
from app.voice.live.events import (
    ErrorEvent,
    FinalTranscriptEvent,
    LatencyEvent,
    LiveEvent,
    PartialTranscriptEvent,
    RealtimeDiagnosticsEvent,
    ReplyEvent,
    TtsChunkEvent,
)
from app.voice.live.layer import (
    is_quota_close,
    spoken_missing_key,
    spoken_provider_connect_failed,
    spoken_provider_disconnect,
    spoken_provider_quota_block,
    ws_close_fields,
)
from app.voice.live.voice_memory import (
    DRAIN_TIMEOUT_S,
    PREFIX_BYTES,
    UserAudioTurn,
    note_latency_ms,
    note_pending,
    note_status,
    note_transcription_config,
    transcribe_utterance_pcm,
)

logger = logging.getLogger("ev.voice.live.grok")

# A process-local fingerprint makes a stale launchd worker visible.  Do not
# derive this at health-check time: the point is to report the code that was
# loaded into this process, not whatever happens to be on disk now.
_COMPUTER_EXECUTION_INSTRUCTIONS = (
    "The computer goal is not verified. Call a listed computer function now. "
    "If open_app returned control.preferred=semantic_adapter, call app_action "
    "with the supported operation — do not inspect dozens of Accessibility "
    "nodes first. Preserve ordinals. Do not speak successful completion."
)
_COMPUTER_SPEECH_INSTRUCTIONS = (
    "Speak only the truthful outcome from the latest function output. "
    "Do not claim success unless verified is true. Do not mention budgets, "
    "tools, or schemas."
)
REALTIME_BRIDGE_VERSION = "ev-realtime-barge-in-v1"
REALTIME_BRIDGE_SOURCE_FINGERPRINT = hashlib.sha256(
    Path(__file__).read_bytes()
).hexdigest()[:16]

OnLiveEvent = Callable[[LiveEvent], Awaitable[None]]
OnToolCall = Callable[[str, dict, str], Awaitable[str]]

# After speakers stop, drop this much mic so room echo cannot cancel her.
_ECHO_TAIL_S = 0.18
# SELF-ECHO QUARANTINE: mic frames arriving this soon after our own last
# emitted speech chunk are Evie hearing herself (speaker tail, room reverb,
# playback lagging response.done). Forwarding them lets provider VAD commit
# a false user turn and auto-create a continuation response — the 2026-08-23
# "right, let's get back to it" mid-answer breaks. Owner onset survives via
# VAD prefix padding; the gate reopens after the window.
_SELF_ECHO_QUARANTINE_S = 1.0
# AUTHORITATIVE PLAYBACK TAIL: after the client reports physical playback
# complete, room reverberation can persist briefly. The client owns physical
# truth (samples rendered); this bounds the acoustic tail that follows.
_POST_PLAYBACK_TAIL_S = 0.5
# TEST-ONLY long-form diagnostic instructions (EV_LONG_FORM_DIAGNOSTIC=1).
# Exists solely to recreate the 45-90s failure envelope in ONE response_id
# for internal self-echo verification. Never active in normal conversations.
_LONG_FORM_DIAGNOSTIC_INSTRUCTIONS = (
    "Give one continuous spoken explanation for at least sixty seconds. "
    "Do not end early. Do not ask a question. Do not say 'let's continue', "
    "'let's get back to it', or narrate recovery. Remain in one response "
    "and keep speaking until the full explanation is delivered."
)
# Assistant transcript deltas are UI metadata, not audio. Keep them from
# competing with PCM delivery on the client's main actor.
_OUTPUT_TRANSCRIPT_MIN_INTERVAL_S = 0.08
# Keep provider reads independent from client/audio playout.  A short bounded
# handoff is enough to absorb a burst without turning it into seconds of stale
# audio in the websocket library's internal receive buffer.
_UPSTREAM_EVENT_QUEUE_MAX = 8

_AUDIO_DELTA_TYPES = frozenset(
    {
        "response.output_audio.delta",
        "response.audio.delta",
    }
)
_TRANSCRIPT_DELTA_TYPES = frozenset(
    {
        "response.output_audio_transcript.delta",
        "response.audio_transcript.delta",
        "response.output_text.delta",
    }
)
_INPUT_TRANSCRIPT_TYPES = frozenset(
    {
        "conversation.item.input_audio_transcription.completed",
        "conversation.item.input_audio_transcription.delta",
        "input_audio_transcription.completed",
        "conversation.item.input_audio_transcription.updated",
    }
)
_SPEECH_STARTED_TYPES = frozenset(
    {
        "input_audio_buffer.speech_started",
        "input_audio_buffer.speech_started.delta",
    }
)
_SPEECH_STOPPED_TYPES = frozenset(
    {
        "input_audio_buffer.speech_stopped",
    }
)
_AUDIO_COMMITTED_TYPES = frozenset(
    {
        "input_audio_buffer.committed",
    }
)
_VOICE_MEMORY_TRACE_TYPES = frozenset(
    {
        "input_audio_buffer.speech_started",
        "input_audio_buffer.speech_stopped",
        "input_audio_buffer.committed",
        "conversation.item.created",
        "conversation.item.done",
        "conversation.item.input_audio_transcription.completed",
        "input_audio_transcription.completed",
        "response.created",
        "response.done",
    }
)

_REALTIME_PROVIDERS = frozenset({"openai", "xai"})
_REALTIME_PROVIDER_ALIASES = {
    "openai-realtime": "openai",
    "grok": "xai",
    "grok-voice": "xai",
    "xai-realtime": "xai",
}
_MAX_FUNCTION_ARGUMENT_BYTES = 32_000
_MAX_FUNCTION_OUTPUT_BYTES = 8_000
_FUNCTION_ERROR_TYPES = frozenset(
    {
        "response.function_call_arguments.failed",
        "response.function_call.failed",
        "response.tool_call.failed",
    }
)
_SESSION_EXPIRY_TYPES = frozenset(
    {
        "session.expired",
        "session_expired",
        "realtime.session.expired",
    }
)
_TRANSCRIPT_DONE_TYPES = frozenset(
    {
        "response.output_audio_transcript.done",
        "response.audio_transcript.done",
        "response.output_text.done",
        "response.text.done",
    }
)

# Life tools the realtime model may call. The rest of the registry stays
# on the typed-chat / pipeline path so the session stays snappy.
GROK_VOICE_TOOL_NAMES = tuple(sorted(LIVE_VOICE_TOOLS))


def _normalize_realtime_provider(value: str | None, *, default: str = "xai") -> str:
    provider = str(value or default).strip().lower()
    provider = _REALTIME_PROVIDER_ALIASES.get(provider, provider)
    if provider not in _REALTIME_PROVIDERS:
        raise ValueError(f"Unsupported realtime provider: {provider or '<empty>'}")
    return provider


def _provider_from_model(model: Any) -> str | None:
    value = str(model or "").strip().lower()
    if value.startswith(("gpt-", "openai-")):
        return "openai"
    if value.startswith(("grok-", "xai-")):
        return "xai"
    return None


def _event_provider_hint(event: dict) -> str | None:
    """Read only explicit/provider-model metadata; never infer from content."""

    for key in ("provider", "provider_name", "source_provider", "upstream_provider"):
        value = event.get(key)
        if value:
            try:
                return _normalize_realtime_provider(str(value))
            except ValueError:
                return str(value).strip().lower() or "unknown"
    session = event.get("session")
    if isinstance(session, dict):
        for key in ("provider", "provider_name", "source_provider"):
            value = session.get(key)
            if value:
                try:
                    return _normalize_realtime_provider(str(value))
                except ValueError:
                    return str(value).strip().lower() or "unknown"
        return _provider_from_model(session.get("model"))
    return _provider_from_model(event.get("model"))


def _safe_id_fingerprint(value: Any) -> str | None:
    raw = str(value or "").strip()
    if not raw:
        return None
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:12]


def _tool_schema_metadata(tool: dict) -> dict:
    """Return schema shape metadata without logging descriptions or values."""

    name = str(tool.get("name") or "").strip()
    parameters = tool.get("parameters")
    if not isinstance(parameters, dict):
        return {"name": name, "schema": "invalid"}
    try:
        canonical = json.dumps(
            parameters,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            default=str,
        )
    except (TypeError, ValueError):
        return {"name": name, "schema": "unserializable"}
    properties = parameters.get("properties")
    required = parameters.get("required")
    return {
        "name": name,
        "schema": hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:16],
        "type": str(parameters.get("type") or "") or None,
        "property_names": sorted(
            str(key) for key in (properties.keys() if isinstance(properties, dict) else ())
        ),
        "required": sorted(str(key) for key in required)
        if isinstance(required, list)
        else [],
    }


def _tool_schema_metadata_list(tools: list[dict] | tuple[dict, ...]) -> list[dict]:
    return [
        _tool_schema_metadata(tool)
        for tool in tools
        if isinstance(tool, dict) and str(tool.get("name") or "").strip()
    ]


def _function_tools_from_payload(raw_tools: Any) -> tuple[list[dict], bool]:
    """Extract function tools and report malformed tool metadata separately."""

    if not isinstance(raw_tools, list):
        return [], True
    functions: list[dict] = []
    malformed = False
    for raw in raw_tools:
        if not isinstance(raw, dict):
            malformed = True
            continue
        if raw.get("type") != "function":
            continue
        name = raw.get("name")
        parameters = raw.get("parameters")
        if not isinstance(name, str) or not name.strip() or not isinstance(parameters, dict):
            malformed = True
            continue
        functions.append(raw)
    return functions, malformed


def grok_voice_enabled() -> bool:
    """True when live conversation should speak through a realtime S2S model.

    Typed chat stays on ``EV_CHAT_PROVIDER`` (DeepSeek). Spoken live turns use
    OpenAI Realtime (``gpt-realtime-2.1-mini``) when that key is set, otherwise
    Grok Voice, unless the owner forced ``EV_VOICE_LIVE_BRAIN=pipeline``.
    """

    return live_realtime_provider() is not None


def live_realtime_provider() -> str | None:
    """``openai``, ``xai``, or ``None`` (local ASR + chat + TTS)."""

    brain = (settings.voice_live_brain or "auto").strip().lower()
    if brain == "pipeline":
        return None
    openai_key = bool((settings.openai_api_key or "").strip())
    xai_key = bool((settings.xai_api_key or "").strip())
    if brain == "openai":
        return "openai" if openai_key else None
    if brain == "xai":
        return "xai" if xai_key else None
    if brain == "auto":
        if openai_key:
            return "openai"
        if xai_key:
            return "xai"
    return None


def grok_voice_url(*, model: str | None = None, realtime_url: str | None = None) -> str:
    base = (realtime_url or settings.xai_voice_realtime_url).rstrip("/")
    pinned = (model or settings.xai_voice_model).strip() or "grok-voice-think-fast-2.0"
    return f"{base}?{urlencode({'model': pinned})}"


def openai_realtime_url(*, model: str | None = None, realtime_url: str | None = None) -> str:
    base = (realtime_url or settings.openai_realtime_url).rstrip("/")
    pinned = (model or settings.openai_realtime_model).strip() or "gpt-realtime-2.1-mini"
    return f"{base}?{urlencode({'model': pinned})}"


def _sandbox_instruction_suffix(capability_manifest: dict | None) -> str:
    if not isinstance(capability_manifest, dict):
        return ""
    if capability_manifest.get("memory_scope") != "sandbox":
        return ""
    from app.device_gateway.sandbox_tools import SANDBOX_LIVE_INSTRUCTIONS

    return "\n" + SANDBOX_LIVE_INSTRUCTIONS


def grok_voice_instructions(
    *,
    name: str | None = None,
    description: str | None = None,
    capability_manifest: dict | None = None,
) -> str:
    from app.ev.personality import identity_block
    from app.ev.protocols import spoken_ready_capability_line

    ready_line = (
        spoken_ready_capability_line(capability_manifest)
        if isinstance(capability_manifest, dict)
        else None
    )
    block = identity_block(
        name or settings.persona_name,
        description or settings.persona_description,
        compact=True,
        live_sheet=ready_line,
    )
    return (
        f"{block}\n"
        "You are in a live spoken conversation. Hear the owner and answer "
        "in your voice immediately, calmly and clearly. One question at a time. "
        "Answer ordinary chat directly. Use a listed EV function when the owner "
        "asks you to act or needs current information (text, call, remind, look "
        "something up, show, timer, open, close, look at the camera). "
        "When they ask what you see, to look at something in view, what they "
        "are holding, or to read a label, call look if it is listed. "
        "When they ask for a timer that should ring, call the listed timer "
        "function first with minutes (1 means one minute) and do not only say "
        "you will set it. "
        "When they ask to open or close a named app or an https link, call the "
        "listed open or close function first. "
        "Call only listed functions with their declared parameters; never invent "
        "a function name or argument. If EV asks for confirmation, say the hold "
        "line and wait; never claim completion before verified evidence. "
        "If they only said your name, say Yes? and wait. Do not wait for a "
        "wake word — the app is already open. Prefer action over essay."
        " When asked what you can do, answer in partner language from the live "
        "operator sheet, not with function IDs. Mention the refused list only "
        "when the owner asks what you will not do."
        + _sandbox_instruction_suffix(capability_manifest)
    )


def openai_realtime_instructions(
    *,
    name: str | None = None,
    description: str | None = None,
    capability_manifest: dict | None = None,
) -> str:
    """Instructions for the OpenAI Realtime function-calling session."""

    from app.ev.personality import spoken_identity
    from app.ev.protocols import spoken_ready_capability_line

    who = spoken_identity(name or settings.persona_name)
    instructions = (
        f"You are {who}. Pronounce your name as the two letter names E V, never E-y or Evie. Never present as ChatGPT, OpenAI, Grok, xAI, or DeepSeek.\n"
        "This is a spoken conversation. Hear the person and answer out loud "
        "calmly and clearly. Use an available EV function for every owner request to "
        "perform an action or retrieve "
        "current or personal information, you MUST call the matching listed EV "
        "function before answering. This includes setting or starting a timer: "
        "call the matching listed timer function with the requested minutes and "
        "text when applicable. "
        "For any turn that may involve projects, goals, commitments, status, "
        "what-changed, or canonical life state, call evie_turn with the canonical "
        "owner transcript (owner speech only, final). Luna will interpret, Evie "
        "Core owns truth. Only claim Done/Created/Saved when evie_turn returns "
        "ok true; never contradict its canonical_data. "
        "This includes opening, closing, inspecting, or operating apps on this "
        "Mac: call the matching listed computer functions before speaking, and "
        "keep calling them until the owner's goal is verified or blocked. "
        "Speech is never execution evidence. Do not say a track is playing, a "
        "playlist was found, or a click happened unless the function output "
        "has verified true. "
        "When they ask what you see, what they are holding, to read something "
        "in view, what color something is, or whether something looks right, "
        "and camera look is listed as ready, call look. Do not guess. Do not "
        "claim you cannot see. The owner does not need to say camera. That is "
        "one current frame, not a stream. For change over a few seconds, call "
        "observe_camera. Never invent visual contents if no image is present. "
        "For an owner action, the function call must be the first output item: emit "
        "no spoken audio, acknowledgement, promise, or assistant message before it. "
        "Never answer with a promise, plan, or conversational acknowledgement such "
        "as 'I'll set that' or 'let me do that' without first making the function "
        "call. For timers and other single-shot tools, call each matching function "
        "at most once for one owner request. For computer-control goals you may "
        "call inspect_ui, ui_action, screen_look, app_action, open_app, activate_app, "
        "list_apps, and close_app multiple times: observe, act, verify, continue. "
        "Prefer app_action for Music playlists, tracks, and playback. Preserve "
        "ordinals: first stays first, second stays second. Do not stop "
        "after merely opening an app if the owner asked you to do something inside "
        "it. After a non-computer function output arrives, treat that request as "
        "handled: do not repeat the same function call, and give the short spoken "
        "answer from the returned result, including a truthful failure if it failed. "
        "Treat function output as authoritative. For computer tools, executed "
        "and verified are different: opening an app is not completion of a "
        "play/find/type goal. Only claim the owner's requested outcome when "
        "verified is true. If must_continue is true or completion_claim_allowed "
        "is false, keep calling computer functions or report the actual "
        "failure; never say it is playing, sent, or done without a verification "
        "receipt. "
        "Call only listed functions with their declared parameters; never invent "
        "a function name or argument. If no exact high-level function matches, "
        "compose the listed computer-control functions before declaring inability. "
        "Do not describe manual steps when you can perform them. If a listed "
        "function is missing because it is unavailable, say the setup or policy "
        "reason from the live manifest; do not fall back to generic chat or claim "
        "the action ran. "
        "If a function result requires confirmation, say the hold line and wait "
        "for the owner; do not claim completion. "
        "Do not wait for a wake word — the app is open. Prefer action over essay. "
        "When asked what you can do, use the live operator sheet in partner "
        "language, never raw function IDs. Mention refusals only when asked. "
        "SPEECH STYLE: you have one voice and one conversational floor. Speak "
        "in a calm, warm, conversational tone with relaxed, steady pacing and "
        "smooth sentence transitions. Deliver each response as one coherent "
        "thought: when an explanation requires detail, keep speaking smoothly "
        "until the explanation is complete, then finish cleanly and stop. Use "
        "natural sentence boundaries with brief punctuation pauses; never "
        "leave long unexplained silence inside an answer. Do not narrate that "
        "you are thinking, resuming, continuing, or recovering. Never use "
        "listener backchannels or filler acknowledgements (mhm, uh-huh, "
        "right, okay) unless the answer itself genuinely requires the words."
        + _sandbox_instruction_suffix(capability_manifest)
    )
    if isinstance(capability_manifest, dict):
        instructions += (
            "\nLIVE IDENTITY CAPABILITY SHEET (ready only):\n"
            + spoken_ready_capability_line(capability_manifest)
        )
    return instructions


def capability_instructions(manifest: dict | None) -> str:
    """Keep provider-side speech grounded without sending a JSON registry dump."""

    if not isinstance(manifest, dict):
        return ""
    if manifest.get("memory_scope") == "sandbox":
        from app.device_gateway.sandbox_tools import SANDBOX_LIVE_INSTRUCTIONS

        return "\n" + SANDBOX_LIVE_INSTRUCTIONS
    from app.ev.camera_runtime import camera_model_instructions
    from app.ev.computer_runtime import computer_model_instructions
    from app.ev.protocols import spoken_operator_sheet
    from app.memory.relationship import live_memory_instructions

    sheet = spoken_operator_sheet(manifest)
    error = str(manifest.get("capability_error") or "").strip()
    failure = (
        f" Live capability projection error: {error}. Do not claim any EV action "
        "is available or complete until the projection recovers."
        if error
        else ""
    )
    camera = camera_model_instructions(manifest.get("camera"))
    computer = computer_model_instructions(manifest.get("computer_control"))
    memory = live_memory_instructions(manifest)
    return (
        "\nCURRENT LIVE OPERATOR SHEET (truthful and authoritative):\n"
        f"{sheet}\n{camera}\n{computer}\n{memory}\nOnly claim capabilities from the 'I can do now' line as ready. "
        "Use partner labels rather than function IDs. Never claim an action "
        "completed without a successful result and evidence."
        + failure
    )


def grok_voice_tools(specs: list[dict] | None = None) -> list[dict]:
    """Build flat Realtime function payloads from an approved spec projection.

    ``None`` means that the capability projection was not supplied.  It is
    deliberately treated as empty: the realtime bridge must never widen the
    live surface by reading the static registry.
    """

    wanted = set(GROK_VOICE_TOOL_NAMES)
    blocked = {"execute_command", "drone", "print_start", "camera_replay", "ticket_buy"}
    payload: list[dict] = []
    source_specs = specs or []
    for spec in source_specs:
        if not isinstance(spec, dict):
            continue
        name = spec.get("name")
        if not isinstance(name, str):
            continue
        name = name.strip()
        if name not in wanted or name in blocked:
            continue
        parameters = spec.get("parameters")
        if parameters is not None and not isinstance(parameters, dict):
            continue
        payload.append(
            {
                "type": "function",
                "name": name,
                "description": spec.get("description") or "",
                "parameters": parameters or {"type": "object", "properties": {}},
            }
        )
    return payload


def approved_live_tool_specs(manifest: dict | None) -> list[dict]:
    """Project the existing policy manifest onto the live function surface.

    The policy manifest is the dynamic source of availability/provider state;
    ``LIVE_VOICE_TOOLS`` remains a local defense-in-depth boundary for the
    realtime audio loop. Per-call arguments are still validated and authorized
    again before dispatch, so exposing a schema never grants execution rights.
    """

    if not isinstance(manifest, dict):
        return []
    # Prefer the already-filtered runtime projection.  ``capabilities`` is
    # retained as a compatibility input for callers that have the complete
    # manifest, but it must not widen the live surface when a projection is
    # present.
    raw_entries = manifest.get("live_tool_projection")
    if not isinstance(raw_entries, list):
        raw_entries = manifest.get("capabilities")
    if not isinstance(raw_entries, list):
        raw_entries = manifest.get("tools")
    if not isinstance(raw_entries, list):
        return []
    approved: list[dict] = []
    for raw in raw_entries:
        if not isinstance(raw, dict):
            continue
        name = str(raw.get("name") or "").strip()
        if name not in LIVE_VOICE_TOOLS:
            continue
        # ``protocols.capability_reply`` exposes the already-filtered runtime
        # projection as flat ``type=function`` payloads. Full capability
        # entries carry ``availability`` instead; accept either shape, but
        # never infer approval from a bare registry name.
        projected_function = raw.get("type") == "function"
        if "availability" in raw and raw.get("availability") != "available":
            continue
        if not projected_function and (
            raw.get("model_exposed") is False
            or raw.get("realtime_eligible") is False
            or raw.get("risk_class") in {"R4", "forbidden"}
        ):
            continue
        if (
            not projected_function
            and raw.get("approved") is not True
            and raw.get("availability") != "available"
        ):
            continue
        approved.append(raw)
    return approved


def _manifest_allows_search(manifest: dict | None) -> bool:
    """Whether an explicit runtime projection permits provider web search."""

    if not isinstance(manifest, dict):
        return False
    if manifest.get("capability_error"):
        return False
    containers = [manifest]
    runtime = manifest.get("runtime_manifest")
    if isinstance(runtime, dict):
        containers.append(runtime)
    if any(
        isinstance(container.get("live_tool_projection"), list)
        and not container["live_tool_projection"]
        for container in containers
    ):
        return False
    for container in containers:
        if container.get("capability_error"):
            continue
        entries = container.get("capabilities")
        if not isinstance(entries, list):
            continue
        for raw in entries:
            if not isinstance(raw, dict) or raw.get("name") not in {"search_web", "web_search"}:
                continue
            if raw.get("availability") != "available":
                continue
            if raw.get("model_exposed") is False or raw.get("realtime_eligible") is False:
                continue
            if raw.get("risk_class") in {"R4", "forbidden"}:
                continue
            return True
    return False


def grok_session_update(
    *,
    provider: str | None = None,
    capability_manifest: dict | None = None,
    approved_tools: list[dict] | None = None,
    function_tools: list[dict] | None = None,
    turn_authority_v2: bool = False,
) -> dict:
    """OpenAI Realtime uses a GA ``session`` with 24 kHz PCM and server VAD.

    Semantic-VAD defaults were leaving the second user turn un-answered:
    the first reply would finish and the provider would wait indefinitely
    to "be sure" the owner was done. Server VAD with ``create_response``
    starts the next spoken turn as soon as they pause.
    """

    kind = _normalize_realtime_provider(provider or live_realtime_provider() or "xai")
    selected_tools = function_tools if function_tools is not None else approved_tools
    if selected_tools is None and isinstance(capability_manifest, dict):
        selected_tools = approved_live_tool_specs(capability_manifest)
    # Neither provider is allowed to fall back to the static registry. The
    # transport normally passes an explicit list (including an empty
    # fail-closed list), but direct callers must get the same behavior.
    if selected_tools is None:
        selected_tools = []
    realtime_tools = grok_voice_tools(selected_tools)
    if kind == "openai":
        voice = (settings.openai_realtime_voice or "marin").strip() or "marin"
        turn_detection = {
            "type": "server_vad",
            "threshold": 0.5,
            "prefix_padding_ms": 200,
            "silence_duration_ms": 400,
            "interrupt_response": False,
            # V2 CANARY: VAD stays a sensor. The application owns
            # response.create; the provider never answers on its own.
            "create_response": not turn_authority_v2,
        }
        return {
            "type": "session.update",
            "session": {
                "type": "realtime",
                "model": (settings.openai_realtime_model or "gpt-realtime-2.1-mini").strip(),
                "instructions": openai_realtime_instructions(
                    capability_manifest=capability_manifest
                )
                + capability_instructions(capability_manifest),
                "output_modalities": ["audio"],
                "audio": {
                    "input": {
                        "format": {"type": "audio/pcm", "rate": 24000},
                        "transcription": {"model": "gpt-4o-mini-transcribe"},
                        "turn_detection": turn_detection,
                    },
                    "output": {
                        "format": {"type": "audio/pcm", "rate": 24000},
                        "voice": voice,
                    },
                },
                "tools": realtime_tools,
                "tool_choice": "auto" if realtime_tools else "none",
            },
        }
    vad = {
        "type": "server_vad",
        "threshold": float(settings.xai_voice_vad_threshold),
        "silence_duration_ms": int(settings.xai_voice_silence_ms),
        "prefix_padding_ms": 330,
    }
    xai_tools = realtime_tools
    # xAI's built-in web_search is provider-side execution, so only expose it
    # when the current EV search capability is explicitly available. It is not
    # a substitute for an empty EV function projection.
    if _manifest_allows_search(capability_manifest):
        xai_tools = [{"type": "web_search"}, *realtime_tools]
    return {
        "type": "session.update",
        "session": {
            "instructions": grok_voice_instructions(
                capability_manifest=capability_manifest
            )
            + capability_instructions(capability_manifest),
            "voice": settings.xai_voice_voice or "eve",
            "reasoning": {"effort": "none"},
            "turn_detection": vad,
            "audio": {
                "input": {
                    "format": {"type": "audio/pcm", "rate": 16000},
                    "transcription": {"language_hint": "en"},
                },
                "output": {"format": {"type": "audio/pcm", "rate": 16000}},
            },
            "tools": xai_tools,
            "tool_choice": "auto" if xai_tools else "none",
        },
    }


def resample_pcm16(pcm: bytes, *, src_rate: int, dst_rate: int) -> bytes:
    """Linear resample of mono PCM16. No-op when rates match."""

    return _StreamResampler(src_rate, dst_rate).feed(pcm, flush=True)


class _StreamResampler:
    """Continuous linear SRC so chunk boundaries do not click."""

    def __init__(self, src_rate: int, dst_rate: int) -> None:
        self.src_rate = int(src_rate)
        self.dst_rate = int(dst_rate)
        self._buf = array.array("h")
        self._pos = 0.0

    def reset(self) -> None:
        self._buf = array.array("h")
        self._pos = 0.0

    def feed(self, pcm: bytes, *, flush: bool = False) -> bytes:
        if not pcm and not flush:
            return b""
        if self.src_rate == self.dst_rate or self.src_rate <= 0 or self.dst_rate <= 0:
            return pcm
        n = len(pcm) - (len(pcm) % 2)
        if n >= 2:
            chunk = array.array("h")
            chunk.frombytes(pcm[:n])
            self._buf.extend(chunk)
        if not self._buf:
            return b""
        step = self.src_rate / self.dst_rate
        out = array.array("h")
        pos = self._pos
        last = len(self._buf) - 1
        limit = last if flush else last
        while pos < limit:
            left = int(pos)
            right = min(left + 1, last)
            frac = pos - left
            sample = self._buf[left] + (self._buf[right] - self._buf[left]) * frac
            out.append(int(sample))
            pos += step
        consumed = min(int(pos), len(self._buf) - (0 if flush else 1))
        if consumed > 0:
            del self._buf[:consumed]
            pos -= consumed
        if flush:
            self._buf = array.array("h")
            pos = 0.0
        self._pos = max(0.0, pos)
        return out.tobytes()


class GrokVoiceBridge:
    """One upstream Grok Voice realtime socket bound to an EV LIVE session."""

    # LiveSession uses this marker to distinguish a real function-call bridge
    # from a legacy OpenAI sidecar object that cannot own tool calls.
    supports_function_calls = True
    bridge_version = REALTIME_BRIDGE_VERSION

    def __init__(
        self,
        *,
        on_event: OnLiveEvent,
        on_tool: OnToolCall | None = None,
        connect: Callable[..., Awaitable[Any]] | None = None,
        now_ms: Callable[[], int] | None = None,
        api_key: str | None = None,
        model: str | None = None,
        provider: str | None = None,
        reconnect_delay_s: float = 0.4,
        capability_manifest: dict | None = None,
        capability_manifest_loader=None,
        approved_tool_specs: list[dict] | None = None,
        tool_specs: list[dict] | None = None,
        tool_specs_loader=None,
        fallback_transcriber=None,
        turn_authority_v2: bool = False,
        long_form_diagnostic: bool = False,
        turn_commit_grace_s: float = 0.6,
    ) -> None:
        self._on_event = on_event
        self._on_tool = on_tool
        self._connect = connect or _default_connect
        self._long_form_diagnostic = bool(long_form_diagnostic)
        self._now = now_ms or (lambda: 0)
        self._provider = _normalize_realtime_provider(
            provider or live_realtime_provider() or "xai"
        )
        self._api_key: str | None
        if api_key is not None:
            self._api_key = api_key
        elif self._provider == "openai":
            self._api_key = settings.openai_api_key
        else:
            self._api_key = settings.xai_api_key
        self._model: str | None
        if model is not None:
            self._model = model
        elif self._provider == "openai":
            self._model = settings.openai_realtime_model
        else:
            self._model = settings.xai_voice_model
        self._upstream_rate = 24000 if self._provider == "openai" else 16000
        self._in_resampler = _StreamResampler(16000, self._upstream_rate)
        self._out_resampler = _StreamResampler(self._upstream_rate, 16000)
        self._ws: Any = None
        self._send_lock = asyncio.Lock()
        self._pump: asyncio.Task | None = None
        self._upstream_event_task: asyncio.Task | None = None
        self._upstream_events: asyncio.Queue[dict] | None = None
        self._input_audio_task: asyncio.Task | None = None
        self._input_audio_pending: dict | None = None
        self._input_audio_wakeup = asyncio.Event()
        self._closed = False
        self._out_pcm = bytearray()
        self._chunk_index = 0
        self._reply_text = ""
        self._last_output_transcript_emit_at = 0.0
        self._first_audio = True
        self._pending_tools = 0
        self._failed = False
        self._playback_active = False
        self._playback_since = 0.0
        self._echo_until = 0.0
        self._playback_silent_after = 0.0
        self._last_audio_emit_at = 0.0
        self._mic_gate_logged = False
        self._assistant_open = False
        self._response_active = False
        self._audio_accepting = True
        self._user_input_open = False
        self._interrupt_in_flight = False
        self._cancelled_response_ids: set[str] = set()
        self._assistant_item_id: str | None = None
        self._reconnect_delay = max(0.01, float(reconnect_delay_s))
        self._reconnect_base = self._reconnect_delay
        # Quota/spend-limit floors the backoff so a billing block cannot turn
        # into an 8s retry storm when refusals arrive without quota markers.
        self._reconnect_floor = self._reconnect_base
        self._quota_announced = False
        # TURN AUTHORITY V2 (canary): VAD is a SENSOR, not conversation
        # authority. With the flag on, provider auto-response creation is
        # disabled (create_response=false) and THIS bridge explicitly creates
        # a response only after an owner turn truly yields: speech_stopped +
        # bounded grace with no continuation. Idempotent per logical turn.
        self._turn_authority_v2 = bool(turn_authority_v2)
        self._turn_commit_grace_s = max(0.05, float(turn_commit_grace_s))
        self._v2_pending_commit: asyncio.Task | None = None
        self._v2_response_created_for_turn: str | None = None
        self._reconnect_task: asyncio.Task | None = None
        self._reconnecting = False
        self._disconnect_announced = False
        self._failed_permanent = False
        self._starting = False
        self._upstream_tool_names: tuple[str, ...] = ()
        self._upstream_session_ready = False
        self._provider_mismatch = False
        self._session_update_metadata: dict[str, Any] = {}
        self._session_ack_metadata: dict[str, Any] = {}
        self._tool_choice: str | None = None
        self._response_tool_choice_supported = True
        self._computer_schema_eval: dict[str, Any] = {}
        self._schema_refresh_attempted = False
        self._function_call_error = False
        self._capability_error: str | None = None
        self._capability_manifest = (
            dict(capability_manifest) if isinstance(capability_manifest, dict) else None
        )
        self._capability_manifest_loader = capability_manifest_loader
        selected_tool_specs = tool_specs if tool_specs is not None else approved_tool_specs
        # ``None`` is a missing capability projection, not permission to read
        # the static registry. Keep both providers fail-closed.
        self._tool_specs = list(selected_tool_specs or [])
        self._load_tools_from_manifest = selected_tool_specs is None
        self._tool_specs_loader = tool_specs_loader
        self._handled_tool_calls: set[str] = set()
        self._tool_response_ids: set[str] = set()
        self._pending_confirmation_calls: dict[str, str] = {}
        self._turn_audio_bytes = 0
        self._turn_audio_chunks = 0
        self._response_id: str | None = None
        self._tool_boundary_pending = False
        self._continuation_sent = False
        self._last_input_transcript = ""
        self._last_input_transcript_at = 0.0
        self._owner_turns: dict[str, UserAudioTurn] = {}
        self._open_turn_id: str | None = None
        self._pcm_prefix = bytearray()
        self._durability_draining = False
        self._provider_session_id: str | None = None
        self._input_transcription_requested = False
        self._input_transcription_confirmed = False
        self._input_transcription_model: str | None = None
        self._fallback_transcriber = fallback_transcriber

    @property
    def function_tools_enabled(self) -> bool:
        """Whether this session advertised at least one EV function."""

        return bool(self.advertised_function_tools)

    @property
    def advertised_function_tools(self) -> list[dict]:
        """Exact flat function payloads sent in the current session.update."""

        return grok_voice_tools(self._tool_specs)

    @property
    def advertised_tool_names(self) -> tuple[str, ...]:
        advertised = self.advertised_function_tools
        return tuple(
            str(spec.get("name"))
            for spec in advertised
            if isinstance(spec, dict) and spec.get("name")
        )

    @property
    def advertised_tool_metadata(self) -> list[dict]:
        """Safe name/schema metadata for diagnostics; no descriptions or values."""

        return _tool_schema_metadata_list(self.advertised_function_tools)

    @property
    def tool_choice(self) -> str | None:
        return self._tool_choice

    @property
    def session_update_metadata(self) -> dict:
        return dict(self._session_update_metadata)

    @property
    def session_ack_metadata(self) -> dict:
        return dict(self._session_ack_metadata)

    @property
    def realtime_diagnostics(self) -> dict:
        """Metadata-only bridge state suitable for health/state surfaces."""

        return {
            "provider": self._provider,
            "model": self._model,
            "tool_choice": self._tool_choice,
            "tool_names": list(self.advertised_tool_names),
            "tool_schemas": self.advertised_tool_metadata,
            "upstream_tool_names": list(self._upstream_tool_names),
            "upstream_session_ready": self._upstream_session_ready,
            "provider_mismatch": self._provider_mismatch,
            "function_call_error": self._function_call_error,
            "capability_error": bool(self._capability_error),
            "provider_session_id": self._provider_session_id,
            "input_transcription_requested": self._input_transcription_requested,
            "input_transcription_confirmed": self._input_transcription_confirmed,
            "input_transcription_model": self._input_transcription_model,
            "pending_voice_turns": self.pending_voice_turn_count(),
            "computer_tool_schema_hash": (self._computer_schema_eval or {}).get(
                "computer_tool_schema_hash"
            ),
            "tool_schema_match": (self._computer_schema_eval or {}).get("tool_schema_match"),
            "provider_tools_confirmed": (self._computer_schema_eval or {}).get(
                "provider_tools_confirmed"
            ),
            "computer_control_ready": (self._computer_schema_eval or {}).get(
                "computer_control_ready"
            ),
            "tool_schema_generation": (self._computer_schema_eval or {}).get(
                "computer_tool_schema_hash"
            ),
        }

    @property
    def upstream_tool_names(self) -> tuple[str, ...]:
        """Function names acknowledged by the active provider session."""

        return self._upstream_tool_names

    @property
    def upstream_session_ready(self) -> bool:
        return self._upstream_session_ready

    def diagnostics_snapshot(self) -> dict[str, Any]:
        """Return provider facts safe to expose in the Mac developer HUD."""

        return {
            "provider": self._provider,
            "model": self._model,
            "bridge_version": self.bridge_version,
            "advertised_tool_names": list(self.advertised_tool_names),
            "acknowledged_tool_names": list(self.upstream_tool_names),
            "upstream_session_ready": self.upstream_session_ready,
            "tool_choice": self._tool_choice,
            "tool_schemas": self.advertised_tool_metadata,
            "provider_mismatch": self._provider_mismatch,
            "function_call_error": self._function_call_error,
            "capability_error": self._capability_error,
            "provider_session_id": self._provider_session_id,
            "input_transcription_requested": self._input_transcription_requested,
            "input_transcription_confirmed": self._input_transcription_confirmed,
            "input_transcription_model": self._input_transcription_model,
            "pending_voice_turns": self.pending_voice_turn_count(),
            "computer_tool_schema_hash": (self._computer_schema_eval or {}).get(
                "computer_tool_schema_hash"
            ),
            "tool_schema_match": (self._computer_schema_eval or {}).get("tool_schema_match"),
            "provider_tools_confirmed": (self._computer_schema_eval or {}).get(
                "provider_tools_confirmed"
            ),
            "computer_control_ready": (self._computer_schema_eval or {}).get(
                "computer_control_ready"
            ),
            "tool_schema_generation": (self._computer_schema_eval or {}).get(
                "computer_tool_schema_hash"
            ),
        }

    def _response_create_for_user_text(self, text: str) -> dict[str, Any]:
        if looks_like_computer_task(text):
            return self._response_create_after_tool(must_continue=True, terminal_speech=False)
        return {"type": "response.create"}

    def _response_create_after_tool(
        self, *, must_continue: bool, terminal_speech: bool
    ) -> dict[str, Any]:
        create: dict[str, Any] = {"type": "response.create"}
        if must_continue:
            response: dict[str, Any] = {"instructions": _COMPUTER_EXECUTION_INSTRUCTIONS}
            if self._response_tool_choice_supported:
                response["tool_choice"] = "required"
            create["response"] = response
            return create
        if terminal_speech:
            response = {"instructions": _COMPUTER_SPEECH_INSTRUCTIONS}
            if self._response_tool_choice_supported:
                response["tool_choice"] = "none"
            create["response"] = response
        return create

    async def start(self) -> bool:
        if self._ws is not None:
            return True
        if self._closed or self._failed_permanent:
            return False
        if self._starting:
            return self._ws is not None
        self._starting = True
        try:
            return await self._start_unlocked()
        finally:
            self._starting = False

    async def _start_unlocked(self) -> bool:
        if self._ws is not None:
            return True
        if self._closed or self._failed_permanent:
            return False
        leftover = self._pump
        self._pump = None
        if leftover is not None and not leftover.done():
            leftover.cancel()
        self._cancel_upstream_event_pump()
        self._cancel_input_audio_pump()
        self._cancel_v2_pending_commit()
        if not (self._api_key or "").strip():
            self._failed = True
            self._failed_permanent = True
            await self._on_event(
                ErrorEvent(
                    at_ms=self._now(),
                    code="realtime_missing_key",
                    message=spoken_missing_key(self._provider),
                    fatal=True,
                )
            )
            return False
        if self._provider == "openai":
            url = openai_realtime_url(model=self._model)
            headers = {"Authorization": f"Bearer {self._api_key}"}
        else:
            url = grok_voice_url(model=self._model)
            headers = {"Authorization": f"Bearer {self._api_key}"}
        logger.warning(
            "realtime_trace event=provider.selected provider=%s model=%s",
            self._provider,
            self._model,
        )
        try:
            self._ws = await self._connect(url, additional_headers=headers)
        except Exception as exc:  # noqa: BLE001 - keep EV LIVE alive and retry
            self._failed = False
            logger.error(
                "realtime_trace event=connect.failed provider=%s error_type=%s",
                self._provider,
                type(exc).__name__,
            )
            if not self._disconnect_announced:
                self._disconnect_announced = True
                await self._on_event(
                    ErrorEvent(
                        at_ms=self._now(),
                        code="realtime_connect",
                        message=spoken_provider_connect_failed(self._provider)[:240],
                        fatal=False,
                    )
                )
            self._schedule_reconnect()
            return False
        self._failed = False
        self._disconnect_announced = False
        self._quota_announced = False
        self._reconnect_delay = self._reconnect_base
        self._reconnect_floor = self._reconnect_base
        await self._refresh_capability_manifest()
        await self._refresh_tool_specs()
        session_update = grok_session_update(
            provider=self._provider,
            capability_manifest=self._capability_manifest,
            function_tools=self._tool_specs,
            turn_authority_v2=self._turn_authority_v2,
        )
        session_payload = session_update.get("session")
        session_payload = session_payload if isinstance(session_payload, dict) else {}
        session_tools = session_payload.get("tools")
        session_tools = session_tools if isinstance(session_tools, list) else []
        self._tool_choice = str(session_payload.get("tool_choice") or "none")
        self._session_update_metadata = {
            "event": "session.update",
            "provider": self._provider,
            "model": self._model,
            "tool_choice": self._tool_choice,
            "tool_names": [
                str(item.get("name"))
                for item in session_tools
                if isinstance(item, dict) and item.get("name")
            ],
            "tool_schemas": _tool_schema_metadata_list(
                [item for item in session_tools if isinstance(item, dict)]
            ),
        }
        audio = session_payload.get("audio") if isinstance(session_payload.get("audio"), dict) else {}
        audio_in = audio.get("input") if isinstance(audio.get("input"), dict) else {}
        requested_tx = (
            audio_in.get("transcription")
            if isinstance(audio_in.get("transcription"), dict)
            else {}
        )
        self._input_transcription_requested = bool(requested_tx)
        self._input_transcription_model = (
            requested_tx.get("model") if isinstance(requested_tx, dict) else None
        )
        self._session_update_metadata["input_transcription_requested"] = (
            self._input_transcription_requested
        )
        self._session_update_metadata["input_transcription_model"] = (
            self._input_transcription_model
        )
        note_transcription_config(
            requested=self._input_transcription_requested,
            provider_confirmed=self._input_transcription_confirmed,
            model=self._input_transcription_model,
            provider_session_id=self._provider_session_id,
        )
        self._upstream_tool_names = ()
        self._upstream_session_ready = False
        self._provider_mismatch = False
        self._session_ack_metadata = {}
        self._tool_boundary_pending = False
        self._continuation_sent = False
        self._response_id = None
        self._turn_audio_bytes = 0
        self._turn_audio_chunks = 0
        if not await self._send(session_update):
            return False
        if self._ws is None:
            return False
        self._upstream_events = asyncio.Queue(maxsize=_UPSTREAM_EVENT_QUEUE_MAX)
        self._upstream_event_task = asyncio.create_task(
            self._upstream_event_loop(), name="ev-realtime-voice-events"
        )
        self._input_audio_task = asyncio.create_task(
            self._input_audio_loop(), name="ev-realtime-voice-input"
        )
        logger.warning(
            "realtime_trace event=session.update.sent provider=%s model=%s tool_choice=%s tool_names=%s tool_schemas=%s",
            self._provider,
            self._model,
            self._tool_choice,
            self._session_update_metadata["tool_names"],
            self._session_update_metadata["tool_schemas"],
        )
        await self._on_event(
            RealtimeDiagnosticsEvent(
                at_ms=self._now(),
                diagnostics={
                    **self.diagnostics_snapshot(),
                    "phase": "session.update.sent",
                },
            )
        )
        if not session_tools:
            message = (
                "No approved realtime tools were exposed; function calls are disabled."
            )
            logger.warning(
                "realtime_trace event=tool_projection.empty provider=%s capability_error=%s",
                self._provider,
                bool(self._capability_error),
            )
            await self._on_event(
                ErrorEvent(
                    at_ms=self._now(),
                    code="realtime_no_tools",
                    message=message,
                    fatal=False,
                )
            )
        self._pump = asyncio.create_task(self._recv_loop(), name="ev-realtime-voice-recv")
        logger.warning(
            "Live realtime connected provider=%s model=%s advertised_tools=%s",
            self._provider,
            self._model,
            list(self.advertised_tool_names),
        )
        return True

    def set_playback(self, active: bool) -> None:
        """Tell the bridge whether local playback is currently audible."""

        was = self._playback_active
        self._playback_active = bool(active)
        if self._playback_active:
            self._user_input_open = False
            if not was:
                self._playback_since = time.monotonic()
            self._playback_silent_after = 0.0
        else:
            if was:
                # The client rendered the final speaker sample: authoritative
                # physical completion. Protect only the acoustic tail now.
                self._playback_silent_after = time.monotonic() + _POST_PLAYBACK_TAIL_S
                self._echo_until = time.monotonic() + _ECHO_TAIL_S
            self._playback_since = 0.0

    def _playback_blocks_mic(self) -> bool:
        now = time.monotonic()
        # 1. AUTHORITATIVE CLIENT PLAYBACK: the client owns physical reality —
        #    while it reports rendering, the speaker is audible no matter what
        #    response.done says or how long ago our last chunk was sent.
        #    response.done and backend send completion can NEVER open the mic.
        if self._playback_active:
            return True
        # 2. Post-playback acoustic tail after authoritative completion.
        if now < self._playback_silent_after:
            return True
        # 3. Bounded fail-safe for clients that never report playback state:
        #    our own recent emissions still mean the speaker may be audible.
        #    This is a fallback ceiling, not the primary definition.
        last_emit = self._last_audio_emit_at
        if last_emit and (now - last_emit) < _SELF_ECHO_QUARANTINE_S:
            return True
        if now < self._echo_until:
            return True
        return False

    async def append_pcm(self, pcm: bytes) -> None:
        if not pcm or self._closed:
            return
        if self._playback_blocks_mic() and not self._user_input_open:
            if not self._mic_gate_logged:
                logger.warning(
                    "realtime_trace event=mic_gate.drop provider=%s response_active=%s assistant_open=%s playback_active=%s",
                    self._provider,
                    self._response_active,
                    self._assistant_open,
                    self._playback_active,
                )
                self._mic_gate_logged = True
            return
        if self._mic_gate_logged:
            logger.warning(
                "realtime_trace event=mic_gate.open provider=%s",
                self._provider,
            )
            self._mic_gate_logged = False
        if self._playback_active:
            self._playback_active = False
            self._playback_since = 0.0
        self._capture_owner_pcm(pcm)
        if self._durability_draining:
            return
        if self._ws is None:
            await self.start()
        if self._ws is None:
            return
        if self._upstream_rate != 16000:
            pcm = self._in_resampler.feed(pcm)
            if not pcm:
                return
        # A slow provider write must never make the server stop reading the
        # client's microphone/control socket. Keep only the newest unsent
        # frame; stale mic audio is worse than a bounded drop under pressure.
        self._input_audio_pending = {
            "type": "input_audio_buffer.append",
            "audio": base64.b64encode(pcm).decode("ascii"),
        }
        self._input_audio_wakeup.set()

    async def send_text(self, text: str) -> None:
        raw = (text or "").strip()
        if not raw or self._closed:
            return
        self._last_input_transcript = raw
        self._last_input_transcript_at = time.monotonic()
        self._discard_queued_audio_events()
        if self._ws is None:
            await self.start()
        if self._ws is None:
            return
        self._audio_accepting = True
        if not await self._send(
            {
                "type": "conversation.item.create",
                "item": {
                    "type": "message",
                    "role": "user",
                    "content": [{"type": "input_text", "text": raw}],
                },
            }
        ):
            return
        if self._long_form_diagnostic:
            logger.warning(
                "realtime_trace event=long_form_diagnostic.response_create "
                "note=TEST-ONLY instructions override active"
            )
            await self._send(
                {
                    "type": "response.create",
                    "response": {
                        "instructions": _LONG_FORM_DIAGNOSTIC_INSTRUCTIONS
                    },
                }
            )
            return
        await self._send(self._response_create_for_user_text(raw))

    async def cancel(self) -> None:
        self._note_cancelled_response()
        self._out_pcm.clear()
        self._first_audio = True
        self._audio_accepting = False
        self._out_resampler.reset()
        self._discard_queued_audio_events()
        await self._cancel_active_response()

    async def interrupt_for_user(
        self,
        *,
        reason: str = "user_barge_in",
        audio_played_ms: int | None = None,
        confidence: float | None = None,
        preroll_ms: int | None = None,
    ) -> dict:
        """Client-confirmed near-end barge-in. Local playback already stopped."""

        started = time.monotonic()
        if self._interrupt_in_flight:
            return {"latched": False, "duplicate": True}
        self._interrupt_in_flight = True
        generated_text = self._reply_text
        generated_ms = generated_duration_ms(
            audio_bytes=self._turn_audio_bytes, text=generated_text
        )
        cancelled_id = self._response_id
        self._note_cancelled_response()
        self._audio_accepting = False
        self._out_pcm.clear()
        self._first_audio = True
        self._out_resampler.reset()
        self._discard_queued_audio_events()
        self._playback_active = False
        self._playback_since = 0.0
        self._echo_until = 0.0
        self._user_input_open = True
        delivered = delivered_assistant_text(
            generated_text,
            audio_played_ms=audio_played_ms,
            generated_duration_ms=generated_ms,
        )
        if generated_text.strip() or self._turn_audio_bytes or cancelled_id:
            await self._on_event(
                ReplyEvent(
                    at_ms=self._now(),
                    text=delivered,
                    model=self._model,
                    interrupted=True,
                    interruption_reason=reason,
                    provider_response_id=cancelled_id,
                    audio_played_ms=audio_played_ms,
                    generated_duration_ms=generated_ms,
                    generated_text=generated_text.strip() or None,
                )
            )
        await self._truncate_assistant_item(audio_played_ms)
        await self._cancel_active_response()
        self._reply_text = ""
        self._last_output_transcript_emit_at = 0.0
        self._chunk_index = 0
        self._turn_audio_bytes = 0
        self._turn_audio_chunks = 0
        self._assistant_item_id = None
        cancel_ms = int((time.monotonic() - started) * 1000)
        await self._on_event(
            LatencyEvent(
                at_ms=self._now(),
                metric="barge_in_provider_cancel",
                ms=cancel_ms,
            )
        )
        logger.warning(
            "barge.backend.received event=barge_in.confirmed reason=%s played_ms=%s generated_ms=%s cancel_ms=%s confidence=%s preroll_ms=%s response_id=%s",
            reason,
            audio_played_ms,
            generated_ms,
            cancel_ms,
            confidence,
            preroll_ms,
            cancelled_id,
        )
        logger.warning(
            "realtime_trace event=barge_in.confirmed reason=%s played_ms=%s generated_ms=%s cancel_ms=%s confidence=%s preroll_ms=%s",
            reason,
            audio_played_ms,
            generated_ms,
            cancel_ms,
            confidence,
            preroll_ms,
        )
        self._interrupt_in_flight = False
        return {
            "latched": True,
            "provider_response_id": cancelled_id,
            "audio_played_ms": audio_played_ms,
            "generated_duration_ms": generated_ms,
            "cancel_ms": cancel_ms,
        }

    def _note_cancelled_response(self) -> None:
        rid = self._response_id
        if rid:
            self._cancelled_response_ids.add(rid)
            if len(self._cancelled_response_ids) > 32:
                extras = list(self._cancelled_response_ids)[16:]
                self._cancelled_response_ids = set(extras)

    def _output_is_stale(self, event: dict) -> bool:
        rid = _event_response_id(event)
        if rid and rid in self._cancelled_response_ids:
            return True
        return False

    async def _truncate_assistant_item(self, audio_played_ms: int | None) -> None:
        if self._ws is None or not self._assistant_item_id:
            return
        played = max(0, int(audio_played_ms or 0))
        if played == 0 and not self._turn_audio_bytes:
            # Nothing was generated or delivered on this item: truncating a
            # zero-audio item is a provider protocol error, not a no-op.
            return
        await self._send(
            {
                "type": "conversation.item.truncate",
                "item_id": self._assistant_item_id,
                "content_index": 0,
                "audio_end_ms": played,
            }
        )

    async def mute_input(self) -> None:
        """Owner muted — drop leftover mic so unmute does not fire a stale turn."""

        self._out_pcm.clear()
        self._first_audio = True
        self._assistant_open = False
        self._audio_accepting = False
        self._discard_queued_audio_events()
        if self._ws is None:
            return
        await self._send({"type": "input_audio_buffer.clear"})
        await self._cancel_active_response()

    async def resume_input(self) -> None:
        """Re-arm realtime input after the client resumes from mute."""

        self._out_pcm.clear()
        self._first_audio = True
        self._assistant_open = False
        self._audio_accepting = False
        self._echo_until = 0.0
        self._playback_active = False
        self._playback_since = 0.0
        self._last_audio_emit_at = 0.0
        self._mic_gate_logged = False
        self._user_input_open = False
        self._interrupt_in_flight = False
        self._in_resampler.reset()
        self._discard_queued_audio_events()
        if self._ws is None:
            await self.start()
        if self._ws is None:
            return
        # Drop any frame that raced the mute command. This does not end the
        # response session or change VAD; it simply starts a clean new turn.
        await self._send({"type": "input_audio_buffer.clear"})

    async def _cancel_active_response(self) -> None:
        if not self._response_active or self._ws is None:
            return
        self._response_active = False
        self._assistant_open = False
        self._audio_accepting = False
        await self._send({"type": "response.cancel"})

    def close(self) -> None:
        pending = self._turns_awaiting_transcript()
        if pending and not self._durability_draining:
            note_status(
                "voice_memory.persist_failed",
                reason="closed_with_pending_transcripts",
                count=len(pending),
            )
            try:
                loop = asyncio.get_running_loop()
                loop.create_task(
                    self._fallback_pending_turns("closed_with_pending"),
                    name="ev-voice-memory-close-fallback",
                )
            except RuntimeError:
                pass
        self._closed = True
        self._cancel_input_audio_pump()
        task = self._reconnect_task
        if task is not None and not task.done():
            task.cancel()
        self._reconnect_task = None
        task = self._pump
        if task is not None and not task.done():
            task.cancel()
        self._pump = None
        self._cancel_upstream_event_pump()
        ws = self._ws
        self._ws = None
        if ws is not None:
            with contextlib.suppress(Exception):
                closer = getattr(ws, "close", None)
                if closer is not None:
                    result = closer()
                    if asyncio.iscoroutine(result):
                        asyncio.create_task(result)

    def pending_voice_turn_count(self) -> int:
        return len(self._turns_awaiting_transcript())

    def _turns_awaiting_transcript(self) -> list[UserAudioTurn]:
        return [turn for turn in self._owner_turns.values() if turn.awaiting_transcript()]

    def _capture_owner_pcm(self, pcm: bytes) -> None:
        if not pcm:
            return
        turn = self._owner_turns.get(self._open_turn_id or "")
        if turn is not None and not turn.transcription_received:
            turn.append_pcm(pcm)
            return
        self._pcm_prefix.extend(pcm)
        overflow = len(self._pcm_prefix) - PREFIX_BYTES
        if overflow > 0:
            del self._pcm_prefix[:overflow]

    def _ensure_open_turn(self) -> UserAudioTurn:
        existing = self._owner_turns.get(self._open_turn_id or "")
        if existing is not None and not existing.transcription_received:
            return existing
        turn = UserAudioTurn(
            local_turn_id=uuid4().hex[:16],
            provider_session_id=self._provider_session_id,
        )
        if self._pcm_prefix:
            turn.append_pcm(bytes(self._pcm_prefix))
            self._pcm_prefix.clear()
        self._open_turn_id = turn.local_turn_id
        self._owner_turns[turn.local_turn_id] = turn
        note_pending(self.pending_voice_turn_count())
        return turn

    def _commit_open_turn(self, *, item_id: str | None = None) -> UserAudioTurn:
        turn = self._ensure_open_turn()
        if item_id:
            turn.provider_item_id = item_id
        turn.audio_committed = True
        turn.transcription_expected = True
        if not turn.committed_at:
            turn.committed_at = time.monotonic()
        turn.status = "transcription_pending"
        note_status(
            "voice_memory.turn_committed",
            turn=turn.local_turn_id,
            item_id=turn.provider_item_id,
        )
        note_status("voice_memory.transcription_pending", turn=turn.local_turn_id)
        note_pending(self.pending_voice_turn_count())
        return turn

    def _bind_item_id(self, item_id: str | None) -> UserAudioTurn | None:
        if not item_id:
            return self._owner_turns.get(self._open_turn_id or "")
        for turn in self._owner_turns.values():
            if turn.provider_item_id == item_id:
                return turn
        turn = self._owner_turns.get(self._open_turn_id or "")
        if turn is None or turn.transcription_received:
            turn = self._commit_open_turn(item_id=item_id)
        else:
            turn.provider_item_id = item_id
        return turn

    def _turn_for_item(self, item_id: str | None) -> UserAudioTurn | None:
        if item_id:
            for turn in self._owner_turns.values():
                if turn.provider_item_id == item_id:
                    return turn
        pending = self._turns_awaiting_transcript()
        if pending:
            return pending[-1]
        return self._owner_turns.get(self._open_turn_id or "")

    async def drain_voice_memory(self, *, timeout_s: float | None = None) -> dict[str, Any]:
        """Keep the provider socket open until owner turns are transcribed or fallen back."""

        self._durability_draining = True
        open_turn = self._owner_turns.get(self._open_turn_id or "")
        if (
            open_turn is not None
            and not open_turn.audio_committed
            and len(open_turn.pcm) >= 320
        ):
            self._commit_open_turn()
        bound = DRAIN_TIMEOUT_S if timeout_s is None else max(0.05, float(timeout_s))
        logger.info(
            "realtime_trace event=voice_memory.drain_start pending=%s timeout_s=%s",
            self.pending_voice_turn_count(),
            bound,
        )
        deadline = time.monotonic() + bound
        settle_until = time.monotonic() + min(0.4, bound)
        while time.monotonic() < settle_until and not self._turns_awaiting_transcript():
            await asyncio.sleep(0.05)
        while self._turns_awaiting_transcript() and time.monotonic() < deadline:
            await asyncio.sleep(0.05)
        leftover = self._turns_awaiting_transcript()
        if leftover:
            await self._fallback_pending_turns("transcription_timeout")
        drained = [turn for turn in self._owner_turns.values() if turn.transcription_received]
        note_pending(self.pending_voice_turn_count())
        logger.info(
            "realtime_trace event=voice_memory.drain_done transcribed=%s leftover=%s",
            len(drained),
            self.pending_voice_turn_count(),
        )
        return {
            "pending": self.pending_voice_turn_count(),
            "transcribed": len(drained),
            "timeout_s": bound,
        }

    async def _fallback_pending_turns(self, reason: str) -> None:
        for turn in list(self._turns_awaiting_transcript()):
            await self._fallback_turn(turn, reason=reason)

    async def _fallback_turn(self, turn: UserAudioTurn, *, reason: str) -> None:
        if turn.transcription_received:
            return
        note_status(
            "voice_memory.transcription_timeout",
            turn=turn.local_turn_id,
            reason=reason,
            pcm_bytes=len(turn.pcm),
        )
        if not turn.pcm:
            turn.persist_failed = True
            turn.status = "persist_failed"
            note_status("voice_memory.persist_failed", turn=turn.local_turn_id, reason="no_pcm")
            note_pending(self.pending_voice_turn_count())
            return
        note_status("voice_memory.fallback_asr_started", turn=turn.local_turn_id, reason=reason)
        spoken = await transcribe_utterance_pcm(
            bytes(turn.pcm),
            transcriber=self._fallback_transcriber,
        )
        if not spoken:
            turn.persist_failed = True
            turn.status = "persist_failed"
            turn.release_pcm()
            note_status(
                "voice_memory.persist_failed",
                turn=turn.local_turn_id,
                reason="fallback_empty",
            )
            note_pending(self.pending_voice_turn_count())
            return
        await self._emit_user_transcript(
            spoken,
            final=True,
            item_id=turn.provider_item_id,
            source="fallback_asr",
        )

    async def _send(self, payload: dict, *, timeout_s: float = 2.0) -> bool:
        ws = self._ws
        if ws is None:
            return False
        raw = json.dumps(payload)
        try:
            async with self._send_lock:
                if self._ws is not ws:
                    return False
                await asyncio.wait_for(ws.send(raw), timeout=max(0.1, timeout_s))
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - recover a dropped LIVE socket
            logger.debug("Grok Voice send failed", exc_info=True)
            await self._note_disconnect(exc, ws=ws)
            return False
        return True

    async def _input_audio_loop(self) -> None:
        """Send at most one newest microphone frame at a time."""

        while not self._closed:
            await self._input_audio_wakeup.wait()
            while not self._closed:
                self._input_audio_wakeup.clear()
                payload = self._input_audio_pending
                self._input_audio_pending = None
                if payload is None:
                    break
                if not await self._send(payload, timeout_s=0.4) and self._ws is None:
                    return

    def _cancel_input_audio_pump(self) -> None:
        task = self._input_audio_task
        self._input_audio_task = None
        self._input_audio_pending = None
        self._input_audio_wakeup.set()
        if task is not None and task is not asyncio.current_task() and not task.done():
            task.cancel()

    async def _upstream_event_loop(self) -> None:
        """Handle provider events off the websocket receive coroutine."""

        queue = self._upstream_events
        if queue is None:
            return
        try:
            while not self._closed and self._upstream_events is queue:
                event = await queue.get()
                try:
                    await self._handle_upstream(event)
                finally:
                    queue.task_done()
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - reconnect a failed event pump
            logger.error(
                "realtime_trace event=event_handler.failed provider=%s error_type=%s",
                self._provider,
                type(exc).__name__,
            )
            await self._note_disconnect(exc)

    def _cancel_upstream_event_pump(self) -> None:
        task = self._upstream_event_task
        self._upstream_event_task = None
        queue = self._upstream_events
        self._upstream_events = None
        if queue is not None:
            while True:
                try:
                    queue.get_nowait()
                except asyncio.QueueEmpty:
                    break
                else:
                    queue.task_done()
        if task is not None and task is not asyncio.current_task() and not task.done():
            task.cancel()

    def _discard_queued_audio_events(self) -> None:
        """Remove provider audio still waiting behind a cancelled response."""

        queue = self._upstream_events
        if queue is None:
            return
        retained: list[dict] = []
        while True:
            try:
                event = queue.get_nowait()
            except asyncio.QueueEmpty:
                break
            kind = str(event.get("type") or "")
            queue.task_done()
            if kind in _AUDIO_DELTA_TYPES or kind in _TRANSCRIPT_DELTA_TYPES:
                continue
            retained.append(event)
        for event in retained:
            queue.put_nowait(event)

    async def _recv_loop(self) -> None:
        ws = self._ws
        if ws is None:
            return
        try:
            async for message in ws:
                if self._closed:
                    return
                event = _parse_event(message)
                if event:
                    queue = self._upstream_events
                    if queue is None:
                        return
                    await queue.put(event)
                if self._ws is not ws:
                    return
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - keep EV LIVE alive
            close_code, close_reason = ws_close_fields(exc)
            logger.error(
                "realtime_trace event=receive.failed provider=%s error_type=%s close_code=%s close_reason=%s",
                self._provider,
                type(exc).__name__,
                close_code,
                close_reason,
            )
            await self._note_disconnect(exc, ws=ws)
        else:
            if not self._closed:
                await self._note_disconnect(
                    ConnectionError("realtime stream closed"), ws=ws
                )

    async def _close_ws_quietly(self, target: Any | None) -> None:
        if target is None:
            return
        try:
            closer = getattr(target, "close", None)
            if closer is not None:
                await asyncio.wait_for(closer(), timeout=2.0)
        except Exception:  # noqa: BLE001 - a dead socket must never break recovery
            logger.debug("realtime upstream socket close failed", exc_info=True)

    async def _abandon_upstream_ws(self, ws: Any | None = None) -> None:
        """Close and forget an upstream socket so a dead session cannot leak.

        Each reconnect opens a brand-new provider session. An unclosed old
        socket keeps that session alive server-side; during the 2026-08-22
        Mac incident thousands of leaked reconnect sockets exhausted the
        provider until every new connection stalled on its first send.
        """

        target = ws if ws is not None else self._ws
        if ws is None or ws is self._ws:
            self._ws = None
        await self._close_ws_quietly(target)

    async def _note_disconnect(self, exc: BaseException, *, ws: Any | None = None) -> None:
        close_code, close_reason = ws_close_fields(exc)
        if close_code is not None or close_reason:
            logger.error(
                "realtime_trace event=upstream.closed provider=%s close_code=%s close_reason=%s",
                self._provider,
                close_code,
                close_reason,
            )
        if is_quota_close(close_reason, str(exc)):
            await self._note_quota_block(close_reason)
            return
        if ws is not None and self._ws is not ws:
            # Stale socket notification: free it, but the live socket state
            # is untouched.
            await self._abandon_upstream_ws(ws)
            return
        target = self._ws
        self._ws = None
        pump = self._pump
        self._pump = None
        if pump is not None and pump is not asyncio.current_task() and not pump.done():
            pump.cancel()
        self._cancel_upstream_event_pump()
        self._cancel_input_audio_pump()
        # Close the abandoned socket only after the pumps are cancelled so a
        # pump wakeup cannot re-enter this reset midway.
        await self._close_ws_quietly(target)
        self._out_pcm.clear()
        self._reply_text = ""
        self._last_output_transcript_emit_at = 0.0
        self._chunk_index = 0
        self._first_audio = True
        self._pending_tools = 0
        self._tool_response_ids.clear()
        self._tool_boundary_pending = False
        self._continuation_sent = False
        self._response_id = None
        self._upstream_tool_names = ()
        self._upstream_session_ready = False
        self._provider_mismatch = False
        self._session_ack_metadata = {}
        self._turn_audio_bytes = 0
        self._turn_audio_chunks = 0
        self._response_active = False
        self._assistant_open = False
        self._playback_active = False
        self._playback_since = 0.0
        self._echo_until = 0.0
        self._audio_accepting = False
        self._user_input_open = False
        self._interrupt_in_flight = False
        self._in_resampler.reset()
        self._out_resampler.reset()
        if self._turns_awaiting_transcript():
            await self._fallback_pending_turns("provider_disconnect")
        if self._closed or self._failed_permanent or self._durability_draining:
            return
        if not self._disconnect_announced:
            self._disconnect_announced = True
            await self._on_event(
                ErrorEvent(
                    at_ms=self._now(),
                    code="realtime_disconnect",
                    message=spoken_provider_disconnect(self._provider)[:240],
                    fatal=False,
                )
            )
        self._schedule_reconnect()

    async def _note_quota_block(self, close_reason: str) -> None:
        """Upstream spend limit / quota exhaustion.

        This is NOT a transport fault and retrying every few seconds cannot
        succeed — the provider closed with 1013 + insufficient_quota. Say the
        truth once, slow the retry to once a minute so the session self-heals
        if the owner raises the limit, and never masquerade as a generic
        disconnect.
        """

        await self._abandon_upstream_ws()
        pump = self._pump
        self._pump = None
        if pump is not None and pump is not asyncio.current_task() and not pump.done():
            pump.cancel()
        self._cancel_upstream_event_pump()
        self._cancel_input_audio_pump()
        logger.error(
            "realtime_trace event=provider.quota_blocked provider=%s close_reason=%s reconnect_delay_s=60",
            self._provider,
            close_reason,
        )
        if not self._closed and not self._failed_permanent:
            # One truthful notification per quota episode: the error event and
            # the 1013 close of the same session must not double-notify.
            self._reconnect_delay = max(60.0, float(getattr(self, "_reconnect_base", 1.0)))
            self._reconnect_floor = self._reconnect_delay
            if not self._quota_announced:
                self._quota_announced = True
                await self._on_event(
                    ErrorEvent(
                        at_ms=self._now(),
                        code="realtime_quota",
                        message=spoken_provider_quota_block(self._provider)[:240],
                        fatal=False,
                    )
                )
        if not (self._closed or self._failed_permanent or self._durability_draining):
            self._schedule_reconnect()

    def _schedule_v2_turn_commit(self) -> None:
        """TURN AUTHORITY V2: silence is EVIDENCE, not a command to answer.

        After speech_stopped, wait a bounded grace window. If the owner
        resumes (continuation), the commit cancels. Only a quiet grace expiry
        finalizes the logical owner turn and explicitly creates ONE response
        for it. Idempotent per turn id.
        """

        async def _commit_after_grace() -> None:
            try:
                await asyncio.sleep(self._turn_commit_grace_s)
            except asyncio.CancelledError:
                return
            turn_id = self._open_turn_id
            if not turn_id:
                return
            if self._v2_response_created_for_turn == turn_id:
                return
            self._v2_response_created_for_turn = turn_id
            logger.info(
                "realtime_trace event=ta.owner_turn_finalized turn=%s reason=bounded_end_confidence grace_s=%.2f",
                turn_id,
                self._turn_commit_grace_s,
            )
            logger.info(
                "realtime_trace event=ta.response_create_sent turn=%s reason=bounded_end_confidence",
                turn_id,
            )
            await self._send({"type": "response.create"})

        self._v2_pending_commit = asyncio.create_task(
            _commit_after_grace(), name="ev-v2-turn-commit"
        )

    def _cancel_v2_pending_commit(self) -> None:
        task = self._v2_pending_commit
        self._v2_pending_commit = None
        if task is not None and not task.done():
            task.cancel()

    def _schedule_reconnect(self) -> None:
        if self._closed or self._failed_permanent or self._durability_draining:
            return
        if self._reconnect_task is not None and not self._reconnect_task.done():
            return
        self._reconnect_task = asyncio.create_task(
            self._reconnect_loop(), name="ev-realtime-reconnect"
        )

    def _next_reconnect_delay(self) -> float:
        """Backoff after one failed attempt, floored while quota-blocked."""

        return max(self._reconnect_floor, min(8.0, self._reconnect_delay * 2))

    async def _reconnect_loop(self) -> None:
        self._reconnecting = True
        try:
            while not self._closed and not self._failed_permanent and self._ws is None:
                await asyncio.sleep(self._reconnect_delay)
                self._reconnect_delay = self._next_reconnect_delay()
                if await self.start():
                    self._reconnect_delay = self._reconnect_base
                    return
        except asyncio.CancelledError:
            raise
        finally:
            self._reconnecting = False

    async def _refresh_capability_manifest(self) -> None:
        loader = self._capability_manifest_loader
        if loader is None:
            return
        try:
            produced = loader()
            produced = await produced if asyncio.iscoroutine(produced) else produced
        except Exception as exc:  # noqa: BLE001 - expose a truthful fail-closed state
            self._capability_error = f"{type(exc).__name__}: {exc}"[:500]
            current = dict(self._capability_manifest or {})
            current["capability_error"] = self._capability_error
            self._capability_manifest = current
            logger.error(
                "realtime_trace event=capability.refresh_failed provider=%s error_type=%s",
                self._provider,
                type(exc).__name__,
            )
            return
        if not isinstance(produced, dict):
            return
        if isinstance(produced.get("capability_manifest"), dict):
            produced = produced["capability_manifest"]
        current = dict(self._capability_manifest or {})
        current.update(produced)
        self._capability_error = str(current.get("capability_error") or "") or None
        self._capability_manifest = current

    async def _refresh_tool_specs(self) -> None:
        loader = self._tool_specs_loader
        if loader is None:
            if self._load_tools_from_manifest and isinstance(self._capability_manifest, dict):
                self._tool_specs = approved_live_tool_specs(self._capability_manifest)
                self._load_tools_from_manifest = False
            return
        try:
            produced = loader()
            produced = await produced if asyncio.iscoroutine(produced) else produced
        except Exception as exc:  # noqa: BLE001 - fail closed and expose the reason
            self._capability_error = f"{type(exc).__name__}: {exc}"[:500]
            current = dict(self._capability_manifest or {})
            current["capability_error"] = self._capability_error
            self._capability_manifest = current
            self._tool_specs = []
            logger.error(
                "realtime_trace event=tool_projection.refresh_failed provider=%s error_type=%s",
                self._provider,
                type(exc).__name__,
            )
            return
        if isinstance(produced, dict):
            produced = approved_live_tool_specs(produced)
        if isinstance(produced, list):
            self._tool_specs = [item for item in produced if isinstance(item, dict)]
            self._capability_error = None
            self._load_tools_from_manifest = False

    async def refresh_live_instructions(self) -> bool:
        """Push updated capability instructions, and tools if the catalog changed.

        Audio / VAD settings are not resent. A stale tool catalog is not allowed
        to masquerade as the current build.
        """

        if self._closed or self._ws is None:
            return False
        previous_names = tuple(self.advertised_tool_names)
        await self._refresh_capability_manifest()
        await self._refresh_tool_specs()
        manifest = self._capability_manifest if isinstance(self._capability_manifest, dict) else None
        if self._provider == "openai":
            text = openai_realtime_instructions(capability_manifest=manifest) + capability_instructions(
                manifest
            )
        else:
            text = grok_voice_instructions(capability_manifest=manifest) + capability_instructions(
                manifest
            )
        session_payload: dict[str, Any] = {"instructions": text}
        if self._provider == "openai":
            # GA Realtime requires session.type on EVERY session.update;
            # omitting it made every tools refresh fail with
            # missing_required_parameter: 'session.type'.
            session_payload["type"] = "realtime"
        new_names = tuple(self.advertised_tool_names)
        tools_changed = new_names != previous_names or new_names != self._upstream_tool_names
        if tools_changed:
            realtime_tools = grok_voice_tools(self._tool_specs)
            session_payload["tools"] = realtime_tools
            session_payload["tool_choice"] = "auto" if realtime_tools else "none"
            self._upstream_session_ready = False
        sent = await self._send({"type": "session.update", "session": session_payload})
        logger.warning(
            "realtime_trace event=session.update.live_refresh provider=%s sent=%s tools_changed=%s tool_names=%s ui_ready=%s",
            self._provider,
            sent,
            tools_changed,
            list(new_names),
            (manifest or {}).get("computer_control", {}).get("generic_ui_control_ready")
            if isinstance(manifest, dict)
            else None,
        )
        return sent

    async def _emit_user_transcript(
        self,
        text: str,
        *,
        final: bool,
        item_id: str | None = None,
        source: str = "provider",
    ) -> None:
        spoken = (text or "").strip()
        if not spoken:
            return
        if final:
            now = time.monotonic()
            if (
                spoken == self._last_input_transcript
                and now - self._last_input_transcript_at < 8.0
            ):
                turn = self._turn_for_item(item_id)
                if turn is not None:
                    turn.transcription_received = True
                    turn.release_pcm()
                    note_pending(self.pending_voice_turn_count())
                return
            self._last_input_transcript = spoken
            self._last_input_transcript_at = now
            turn = self._turn_for_item(item_id)
            if turn is None:
                turn = self._commit_open_turn(item_id=item_id)
            turn.transcription_received = True
            turn.transcript_text = spoken
            turn.transcript_source = source
            turn.persistence_started = True
            turn.status = "transcription_received"
            note_status(
                "voice_memory.transcription_received",
                turn=turn.local_turn_id,
                source=source,
                chars=len(spoken),
            )
            note_latency_ms(
                None if not turn.committed_at else (now - turn.committed_at) * 1000
            )
            turn.release_pcm()
            note_pending(self.pending_voice_turn_count())
            logger.info(
                "realtime_trace event=input_transcript.completed chars=%s fp=%s source=%s",
                len(spoken),
                hashlib.sha256(spoken.encode("utf-8")).hexdigest()[:12],
                source,
            )
            await self._on_event(
                FinalTranscriptEvent(
                    at_ms=self._now(),
                    text=spoken,
                    confidence=None,  # not_reported — provider does not report calibrated confidence
                    provider="openai-realtime" if self._provider == "openai" else "grok-voice",
                    transcript_source=source,
                )
            )
            return
        await self._on_event(
            PartialTranscriptEvent(
                at_ms=self._now(),
                text=spoken,
                sequence=0,
                stable=False,
                confidence=0.0,
            )
        )

    async def _handle_upstream(self, event: dict) -> None:
        kind = str(event.get("type") or "")
        if self._output_is_stale(event) and (
            kind in _AUDIO_DELTA_TYPES
            or kind in _TRANSCRIPT_DELTA_TYPES
            or kind in _TRANSCRIPT_DONE_TYPES
            or kind in {"response.output_audio.done", "response.audio.done", "response.done"}
        ):
            if kind == "response.done":
                self._out_pcm.clear()
                self._reply_text = ""
                self._audio_accepting = False
                self._assistant_open = False
                self._response_active = False
            return
        if kind == "session.updated":
            session = event.get("session")
            session = session if isinstance(session, dict) else {}
            raw_tools = session.get("tools", [])
            function_tools, malformed_tools = _function_tools_from_payload(raw_tools)
            accepted = tuple(
                str(item.get("name"))
                for item in function_tools
            )
            self._upstream_tool_names = accepted
            self._upstream_session_ready = True
            expected = self.advertised_tool_names
            expected_schemas = self.advertised_tool_metadata
            acknowledged_schemas = _tool_schema_metadata_list(function_tools)
            expected_schema_map = {
                item["name"]: item.get("schema") for item in expected_schemas
            }
            acknowledged_schema_map = {
                item["name"]: item.get("schema") for item in acknowledged_schemas
            }
            schema_mismatch = expected_schema_map != acknowledged_schema_map
            self._computer_schema_eval = evaluate_provider_computer_schema(
                advertised_tools=self.advertised_function_tools,
                acknowledged_names=accepted,
                acknowledged_schemas=acknowledged_schemas,
            )
            sandbox_session = (
                isinstance(self._capability_manifest, dict)
                and self._capability_manifest.get("memory_scope") == "sandbox"
            )
            if sandbox_session:
                from app.device_gateway.sandbox_tools import note_provider_effective

                note_provider_effective(accepted, self.advertised_function_tools)
            computer_schema_mismatch = (not sandbox_session) and (
                not self._computer_schema_eval.get("tool_schema_match")
            )
            provider_hint = _event_provider_hint(event)
            self._provider_mismatch = bool(
                malformed_tools
                or tuple(sorted(accepted)) != tuple(sorted(expected))
                or schema_mismatch
                or (provider_hint is not None and provider_hint != self._provider)
            )
            self._session_ack_metadata = {
                "event": "session.updated",
                "provider": self._provider,
                "provider_hint": provider_hint,
                "model": session.get("model") or self._model,
                "acknowledged_tool_names": list(accepted),
                "acknowledged_tool_schemas": acknowledged_schemas,
                "malformed_tools": malformed_tools,
                "schema_mismatch": schema_mismatch,
                "computer_tool_schema_hash": self._computer_schema_eval.get(
                    "computer_tool_schema_hash"
                ),
                "computer_schema_match": self._computer_schema_eval.get("tool_schema_match"),
                "missing_computer_tools": self._computer_schema_eval.get("missing_tools"),
                "provider_mismatch": self._provider_mismatch,
            }
            audio = session.get("audio") if isinstance(session.get("audio"), dict) else {}
            audio_in = audio.get("input") if isinstance(audio.get("input"), dict) else {}
            transcription = (
                audio_in.get("transcription")
                if isinstance(audio_in.get("transcription"), dict)
                else {}
            )
            self._session_ack_metadata["acknowledged_audio_input_keys"] = sorted(audio_in)
            self._session_ack_metadata["input_transcription_model"] = transcription.get(
                "model"
            ) or transcription.get("language_hint")
            session_id = session.get("id")
            if isinstance(session_id, str) and session_id.strip():
                self._provider_session_id = session_id.strip()
                self._session_ack_metadata["provider_session_id"] = self._provider_session_id
            self._input_transcription_confirmed = bool(transcription)
            if transcription.get("model"):
                self._input_transcription_model = transcription.get("model")
            self._session_ack_metadata["input_transcription_requested"] = (
                self._input_transcription_requested
            )
            self._session_ack_metadata["input_transcription_confirmed"] = (
                self._input_transcription_confirmed
            )
            note_transcription_config(
                requested=self._input_transcription_requested,
                provider_confirmed=self._input_transcription_confirmed,
                model=self._input_transcription_model,
                provider_session_id=self._provider_session_id,
            )
            logger.info(
                "realtime_trace event=input_transcription.ack provider=%s model=%s confirmed=%s keys=%s session=%s",
                self._provider,
                self._input_transcription_model,
                self._input_transcription_confirmed,
                sorted(audio_in),
                self._provider_session_id,
            )
            # NO PROVIDER-PROTOCOL ASSUMPTIONS: log the EFFECTIVE
            # turn_detection the provider actually acknowledged.
            ack_turn_detection = (
                session.get("audio", {}).get("input", {}).get("turn_detection")
                if isinstance(session.get("audio"), dict)
                else None
            )
            if not isinstance(ack_turn_detection, dict):
                ack_turn_detection = {}
            logger.warning(
                "realtime_trace event=turn_detection.effective provider=%s type=%s create_response=%s interrupt_response=%s eagerness=%s v2=%s",
                self._provider,
                ack_turn_detection.get("type"),
                ack_turn_detection.get("create_response"),
                ack_turn_detection.get("interrupt_response"),
                ack_turn_detection.get("eagerness"),
                self._turn_authority_v2,
            )
            logger.warning(
                "realtime_trace event=session.updated.received provider=%s model=%s acknowledged_tool_names=%s acknowledged_tool_schemas=%s malformed_tools=%s mismatch=%s",
                self._provider,
                self._session_ack_metadata["model"],
                list(accepted),
                self._session_ack_metadata["acknowledged_tool_schemas"],
                malformed_tools,
                self._provider_mismatch,
            )
            await self._on_event(
                RealtimeDiagnosticsEvent(
                    at_ms=self._now(),
                    diagnostics={
                        **self.diagnostics_snapshot(),
                        "phase": "session.updated.received",
                    },
                )
            )
            if self._provider_mismatch:
                message = (
                    "Realtime provider acknowledged a different function set: "
                    f"expected {list(expected)}, received {list(accepted)}."
                )
                if malformed_tools:
                    message = "Realtime provider acknowledgement contained malformed tool metadata."
                elif schema_mismatch:
                    message = "Realtime provider acknowledged function names with different schemas."
                elif provider_hint is not None and provider_hint != self._provider:
                    message = (
                        "Realtime provider acknowledgement identified a different provider: "
                        f"expected {self._provider}, received {provider_hint}."
                    )
                logger.error("%s", message)
                await self._on_event(
                    ErrorEvent(
                        at_ms=self._now(),
                        code="realtime_tools_rejected",
                        message=message[:240],
                        fatal=False,
                    )
                )
            else:
                logger.warning(
                    "realtime_trace event=tools.exposed_not_called provider=%s model=%s acknowledged_tool_names=%s computer_schema=%s",
                    self._provider,
                    session.get("model") or self._model,
                    list(accepted),
                    self._computer_schema_eval,
                )
            if computer_schema_mismatch and not self._schema_refresh_attempted:
                advertised_computer = [
                    name for name in expected if name in REQUIRED_COMPUTER_TOOLS
                ]
                if advertised_computer:
                    self._schema_refresh_attempted = True
                    logger.warning(
                        "realtime_trace event=tool_schema.stale_refresh provider=%s missing=%s",
                        self._provider,
                        self._computer_schema_eval.get("missing_tools"),
                    )
                    await self.refresh_live_instructions()
            return
        if kind in _VOICE_MEMORY_TRACE_TYPES:
            logger.info(
                "realtime_trace event=voice_memory.provider_event type=%s item_id=%s",
                kind,
                _event_item_id(event),
            )
        if kind == "ping":
            await self._send({"type": "pong"})
            return
        if kind in _SPEECH_STARTED_TYPES:
            if (
                self._playback_active
                or self._assistant_open
                or self._response_active
                or time.monotonic() < self._echo_until
            ):
                return
            # User started a turn. Do not send response.cancel — that errors
            # with "no active response" and can kill the next spoken answer.
            self._ensure_open_turn()
            if self._turn_authority_v2:
                # CONTINUATION: speech restarted inside the grace window — the
                # owner was still forming the thought. Cancel any pending
                # commit; floor stays OWNER. (TA05)
                if self._v2_pending_commit is not None and not self._v2_pending_commit.done():
                    self._v2_pending_commit.cancel()
                    self._v2_pending_commit = None
                    logger.info(
                        "realtime_trace event=ta.continuation_detected turn=%s",
                        self._open_turn_id,
                    )
            return
        if kind in _SPEECH_STOPPED_TYPES:
            self._commit_open_turn(item_id=_event_item_id(event))
            if self._turn_authority_v2:
                self._schedule_v2_turn_commit()
            return
        if kind in _AUDIO_COMMITTED_TYPES:
            self._commit_open_turn(item_id=_event_item_id(event))
            return
        if kind == "response.created":
            self._interrupt_in_flight = False
            if self._continuation_sent:
                # A response created after function_call_output is the
                # continuation. Any preceding response.done was a tool
                # boundary rather than a user-visible answer.
                self._tool_boundary_pending = False
            self._response_active = True
            self._audio_accepting = True
            self._last_output_transcript_emit_at = 0.0
            self._last_audio_emit_at = 0.0
            self._turn_audio_bytes = 0
            self._turn_audio_chunks = 0
            created = event.get("response") if isinstance(event.get("response"), dict) else {}
            self._response_id = (
                str(event.get("response_id") or created.get("id") or event.get("id") or "")
                or None
            )
            return
        if kind == "response.cancelled":
            self._out_pcm.clear()
            self._reply_text = ""
            self._last_output_transcript_emit_at = 0.0
            self._chunk_index = 0
            self._first_audio = True
            self._assistant_open = False
            self._response_active = False
            self._audio_accepting = False
            self._tool_boundary_pending = False
            self._continuation_sent = False
            self._response_id = None
            self._turn_audio_bytes = 0
            self._turn_audio_chunks = 0
            return
        if kind in _INPUT_TRANSCRIPT_TYPES:
            text = _transcript_text(event)
            item_id = _event_item_id(event)
            if "completed" in kind:
                if text:
                    await self._emit_user_transcript(
                        text, final=True, item_id=item_id, source="provider"
                    )
                else:
                    logger.info("realtime_trace event=input_transcript.empty type=%s", kind)
            elif text:
                await self._emit_user_transcript(text, final=False, item_id=item_id)
            return
        if kind in {"conversation.item.done", "conversation.item.created", "response.output_item.added"}:
            item = event.get("item") if isinstance(event.get("item"), dict) else {}
            item_id = _event_item_id(event) or (
                str(item.get("id")).strip() if isinstance(item.get("id"), str) else None
            )
            if item.get("role") == "assistant" and item_id:
                self._assistant_item_id = item_id
            if item.get("role") == "user":
                self._bind_item_id(item_id)
            nested = _item_user_transcript(item)
            if nested:
                await self._emit_user_transcript(
                    nested, final=True, item_id=item_id, source="provider"
                )
            return
        if kind in _TRANSCRIPT_DELTA_TYPES:
            if not self._audio_accepting or self._output_is_stale(event):
                return
            delta = str(event.get("delta") or event.get("text") or "")
            if delta:
                self._reply_text += delta
                now = time.monotonic()
                if now - self._last_output_transcript_emit_at < _OUTPUT_TRANSCRIPT_MIN_INTERVAL_S:
                    return
                self._last_output_transcript_emit_at = now
                await self._on_event(
                    PartialTranscriptEvent(
                        at_ms=self._now(),
                        text=self._reply_text,
                        sequence=self._chunk_index,
                        stable=False,
                        confidence=0.0,
                    )
                )
            return
        if kind in _AUDIO_DELTA_TYPES:
            if not self._audio_accepting or self._output_is_stale(event):
                return
            self._response_active = True
            self._assistant_open = True
            await self._buffer_audio(event)
            return
        if kind == "response.function_call_arguments.done":
            await self._run_tool(event)
            return
        if kind == "response.output_item.done":
            raw_item = event.get("item")
            item = raw_item if isinstance(raw_item, dict) else {}
            if str(item.get("type") or "") == "function_call":
                await self._run_tool(item)
            return
        if kind in {"response.output_audio.done", "response.audio.done"}:
            await self._flush_audio(force=True)
            if self._pending_tools <= 0:
                # Spoken PCM is finished. Clear the echo latch even if
                # response.done is late or missing — otherwise turn 2 is deaf.
                self._assistant_open = False
                self._response_active = False
                self._last_audio_emit_at = time.monotonic()
                logger.warning(
                    "realtime_trace event=spoken_audio.done provider=%s audio_chunks=%s audio_bytes=%s",
                    self._provider,
                    self._turn_audio_chunks,
                    self._turn_audio_bytes,
                )
            return
        if kind == "response.done":
            await self._flush_audio(force=True)
            if self._pending_tools <= 0:
                response = event.get("response")
                response = response if isinstance(response, dict) else {}
                response_id = str(
                    response.get("id") or event.get("response_id") or ""
                )
                tool_boundary = bool(
                    self._tool_boundary_pending
                    or (response_id and response_id in self._tool_response_ids)
                )
                if tool_boundary:
                    continuation_content = bool(
                        self._continuation_sent
                        and (self._reply_text.strip() or self._turn_audio_chunks)
                    )
                    if not continuation_content:
                        if response_id:
                            self._tool_response_ids.discard(response_id)
                        logger.warning(
                            "realtime_trace event=response.done.tool_boundary provider=%s response_id_fingerprint=%s output_types=%s awaiting_continuation=true",
                            self._provider,
                            _safe_id_fingerprint(response_id),
                            [
                                item.get("type")
                                for item in response.get("output", [])
                                if isinstance(item, dict)
                            ],
                        )
                        # Realtime may have emitted a short preamble before
                        # the function call. Do not surface it as the
                        # completed answer; the continuation owns the
                        # authoritative spoken reply.
                        self._reply_text = ""
                        self._last_output_transcript_emit_at = 0.0
                        self._chunk_index = 0
                        self._first_audio = True
                        self._assistant_open = False
                        self._response_active = False
                        self._tool_boundary_pending = False
                        return
                text = (self._reply_text or _transcript_text(event) or "").strip()
                logger.warning(
                    "realtime_trace event=final_spoken_text provider=%s text_chars=%s audio_chunks=%s audio_bytes=%s",
                    self._provider,
                    len(text),
                    self._turn_audio_chunks,
                    self._turn_audio_bytes,
                )
                if self._continuation_sent:
                    logger.warning(
                        "realtime_trace event=response.continuation.completed provider=%s text_chars=%s audio_chunks=%s",
                        self._provider,
                        len(text),
                        self._turn_audio_chunks,
                    )
                # A function-call response normally ends with no spoken
                # content. The function output has just triggered the
                # follow-up response; never surface that empty boundary as a
                # user-visible reply.
                if text:
                    await self._on_event(
                        ReplyEvent(
                            at_ms=self._now(),
                            text=text,
                            model=self._model,
                        )
                    )
                self._reply_text = ""
                self._last_output_transcript_emit_at = 0.0
                self._chunk_index = 0
                self._first_audio = True
                self._assistant_open = False
                self._response_active = False
                self._audio_accepting = False
                self._tool_boundary_pending = False
                self._continuation_sent = False
                self._response_id = None
                self._turn_audio_bytes = 0
                self._turn_audio_chunks = 0
                self._out_resampler.reset()
            return
        if kind in _FUNCTION_ERROR_TYPES:
            self._function_call_error = True
            message, code = _realtime_error_fields(event)
            logger.error(
                "realtime_trace event=function_call.provider_error provider=%s code=%s message_type=%s",
                self._provider,
                code or kind,
                type(message).__name__,
            )
            await self._on_event(
                ErrorEvent(
                    at_ms=self._now(),
                    code="realtime_function_call_error",
                    message="The realtime provider rejected the function call; no ordinary-chat fallback was used.",
                    fatal=False,
                )
            )
            return
        if kind in _SESSION_EXPIRY_TYPES:
            logger.warning(
                "realtime_trace event=session.expired provider=%s reconnect=true",
                self._provider,
            )
            await self._note_disconnect(ConnectionError("realtime session expired"))
            return
        if kind == "error":
            message, code = _realtime_error_fields(event)
            logger.error(
                "realtime_trace event=provider.error provider=%s code=%s message=%s",
                self._provider,
                code,
                str(message)[:200],
            )
            combined = f"{code} {message}".lower()
            if is_quota_close(message, code):
                # Spend-limit refusals arrive as error events too, not only as
                # 1013 close codes. Route them to the truthful quota path so
                # the owner hears "raise the limit" instead of a reconnect
                # loop into a wall.
                await self._note_disconnect(
                    ConnectionError(f"{code} {message}".strip())
                )
                return
            if "tool_choice" in combined or "unknown parameter" in combined:
                self._response_tool_choice_supported = False
                logger.warning(
                    "realtime_trace event=response.tool_choice.unsupported provider=%s",
                    self._provider,
                )
            if _is_benign_realtime_error(message, code):
                return
            if code.lower() in {"session_expired", "session_expiration", "session_closed"}:
                await self._note_disconnect(ConnectionError(message))
                return
            await self._on_event(
                ErrorEvent(
                    at_ms=self._now(),
                    code="realtime",
                    message=message[:240],
                    fatal=False,
                )
            )

    async def _buffer_audio(self, event: dict) -> None:
        raw = event.get("delta") or event.get("audio") or ""
        if not raw:
            return
        try:
            pcm = base64.b64decode(raw)
        except Exception:  # noqa: BLE001
            return
        if _audio_cv_trace_enabled():
            # CV01 PROVIDER_AUDIO_EVENT_RECEIVED — upstream arrival timing
            # for continuity forensics (env-gated; off in production).
            import time as _t

            logger.warning(
                "cv_trace event=cv01.provider_delta mono_s=%.3f bytes=%d",
                _t.monotonic(),
                len(pcm),
            )
        self._out_pcm.extend(pcm)
        first_bytes = int(self._upstream_rate * 2 * 0.08)
        next_bytes = int(self._upstream_rate * 2 * 0.10)
        threshold = first_bytes if self._first_audio else next_bytes
        while len(self._out_pcm) >= threshold:
            chunk = bytes(self._out_pcm[:threshold])
            del self._out_pcm[:threshold]
            await self._emit_pcm(chunk)
            self._first_audio = False
            threshold = next_bytes

    async def _flush_audio(self, *, force: bool) -> None:
        if not self._out_pcm:
            return
        if not force and len(self._out_pcm) < int(self._upstream_rate * 2 * 0.08):
            return
        chunk = bytes(self._out_pcm)
        self._out_pcm.clear()
        await self._emit_pcm(chunk, flush=force)
        self._first_audio = False

    async def _emit_pcm(self, pcm: bytes, *, flush: bool = False) -> None:
        if not self._audio_accepting:
            return
        if not pcm and not flush:
            return
        if self._upstream_rate != 16000:
            pcm = self._out_resampler.feed(pcm, flush=flush)
            if not pcm:
                return
        elif not pcm:
            return
        audio_b64 = base64.b64encode(pcm).decode("ascii")
        text = self._reply_text.strip() if self._chunk_index == 0 else ""
        event = TtsChunkEvent(
            at_ms=self._now(),
            index=self._chunk_index,
            text=text,
            audio_b64=audio_b64,
            content_type="audio/pcm",
            duration_ms=int((len(pcm) / 2) / 16.0),
            sample_rate=16000,
            provider="openai-realtime" if self._provider == "openai" else "grok-voice",
        )
        self._chunk_index += 1
        self._turn_audio_bytes += len(pcm)
        self._turn_audio_chunks += 1
        self._last_audio_emit_at = time.monotonic()
        if _audio_cv_trace_enabled():
            # CV04/CV05 — chunk emitted toward the client (post-pacer).
            logger.warning(
                "cv_trace event=cv04.emit_to_client mono_s=%.3f index=%d audio_ms=%d cum_audio_ms=%d",
                self._last_audio_emit_at,
                event.index,
                event.duration_ms or 0,
                int(self._turn_audio_bytes / 32),
            )
        log = logger.warning if event.index == 0 else logger.debug
        log(
            "realtime_trace event=final_spoken_audio.chunk provider=%s chunk_index=%s audio_bytes=%s text_chars=%s",
            self._provider,
            event.index,
            len(pcm),
            len(text),
        )
        await self._on_event(event)

    async def _run_tool(self, event: dict) -> None:
        name = str(event.get("name") or "")
        call_id = str(event.get("call_id") or event.get("id") or "")
        if call_id and call_id in self._handled_tool_calls:
            logger.warning(
                "realtime_trace event=function_call.duplicate provider=%s call_id_fingerprint=%s",
                self._provider,
                _safe_id_fingerprint(call_id),
            )
            return
        if not call_id:
            self._function_call_error = True
            logger.error(
                "realtime_trace event=function_call.rejected provider=%s reason=missing_call_id function_name=%s",
                self._provider,
                name or "<empty>",
            )
            await self._on_event(
                ErrorEvent(
                    at_ms=self._now(),
                    code="realtime_invalid_tool_call",
                    message="Realtime function call was rejected because it had no call id.",
                    fatal=False,
                )
            )
            return
        if call_id:
            self._handled_tool_calls.add(call_id)
        self._tool_boundary_pending = True
        response = event.get("response")
        response = response if isinstance(response, dict) else {}
        response_id = str(
            event.get("response_id") or response.get("id") or ""
        )
        if response_id:
            self._tool_response_ids.add(response_id)
            self._response_id = response_id
        raw_args = event.get("arguments")
        arguments, argument_error = _decode_function_arguments(raw_args)
        logger.warning(
            "realtime_trace event=response.function_call_arguments.done provider=%s function_name=%s call_id_fingerprint=%s arguments_json_valid=%s argument_keys=%s argument_count=%s",
            self._provider,
            name,
            _safe_id_fingerprint(call_id),
            argument_error is None,
            sorted(arguments),
            len(arguments),
        )
        self._pending_tools += 1
        output = "{}"
        if argument_error:
            self._function_call_error = True
            output = json.dumps(
                {
                    "ok": False,
                    "name": name,
                    "error": "invalid_arguments",
                    "reason": argument_error,
                },
                separators=(",", ":"),
            )
            await self._on_event(
                ErrorEvent(
                    at_ms=self._now(),
                    code="realtime_invalid_arguments",
                    message=argument_error[:240],
                    fatal=False,
                )
            )
        else:
            effective, validation_error = self._validate_function_call(name, arguments)
            logger.warning(
                "realtime_trace event=function_call.validation provider=%s function_name=%s call_id_fingerprint=%s valid=%s",
                self._provider,
                name or "<empty>",
                _safe_id_fingerprint(call_id),
                validation_error is None,
            )
            if validation_error:
                self._function_call_error = True
                output = json.dumps(
                    {
                        "ok": False,
                        "name": name,
                        "error": "invalid_tool_call",
                        "reason": validation_error,
                    },
                    separators=(",", ":"),
                )
                await self._on_event(
                    ErrorEvent(
                        at_ms=self._now(),
                        code="realtime_invalid_tool_call",
                        message=validation_error[:240],
                        fatal=False,
                    )
                )
            elif self._on_tool is not None and name:
                try:
                    output = await self._on_tool(name, effective, call_id)
                    if not isinstance(output, str):
                        output = json.dumps(output, default=str)
                except Exception as exc:  # noqa: BLE001
                    self._function_call_error = True
                    logger.error(
                        "realtime_trace event=function_call.dispatch provider=%s function_name=%s call_id_fingerprint=%s result=failed error_type=%s",
                        self._provider,
                        name,
                        _safe_id_fingerprint(call_id),
                        type(exc).__name__,
                    )
                    await self._on_event(
                        ErrorEvent(
                            at_ms=self._now(),
                            code="realtime_tool_failure",
                            message="The realtime function failed; no successful action is being claimed.",
                            fatal=False,
                        )
                    )
                    output = json.dumps(
                        {"ok": False, "error": "tool_execution_failed"},
                        separators=(",", ":"),
                    )
            else:
                self._function_call_error = True
                output = json.dumps(
                    {"ok": False, "name": name, "error": "tool_handler_unavailable"},
                    separators=(",", ":"),
                )
        if len(output.encode("utf-8")) > _MAX_FUNCTION_OUTPUT_BYTES:
            self._function_call_error = True
            output = json.dumps(
                {"ok": False, "name": name, "error": "tool_output_too_large"},
                separators=(",", ":"),
            )
        pending_confirmation = _function_output_is_pending(output)
        if pending_confirmation and call_id:
            self._pending_confirmation_calls[call_id] = name
        if name in {"look", "observe_camera", "screen_look", "ui_action"}:
            output = await self._deliver_camera_images(name, call_id, output)
        self._pending_tools = max(0, self._pending_tools - 1)
        output_sent = await self._send_function_output(call_id, output)
        try:
            output_payload = json.loads(output)
        except (TypeError, json.JSONDecodeError):
            output_payload = {}
        inner = (
            output_payload.get("result")
            if isinstance(output_payload, dict)
            and isinstance(output_payload.get("result"), dict)
            else {}
        )
        output_ok = isinstance(output_payload, dict) and (
            output_payload.get("ok") is True
            or output_payload.get("executed") is True
            or (isinstance(inner, dict) and inner.get("ok") is True)
        )
        goal = output_payload.get("goal") if isinstance(output_payload, dict) else None
        must_continue = bool(
            isinstance(output_payload, dict)
            and (
                output_payload.get("must_continue")
                or (isinstance(goal, dict) and goal.get("must_continue"))
                or (isinstance(inner, dict) and inner.get("must_continue"))
            )
        )
        result_label = (
            "success"
            if output_ok and not must_continue
            else "progress"
            if must_continue or output_ok
            else "failed"
        )
        logger.warning(
            "realtime_trace event=function_call.dispatch provider=%s function_name=%s call_id_fingerprint=%s result=%s output_sent=%s output_bytes=%s confirmation_pending=%s verified=%s must_continue=%s",
            self._provider,
            name,
            _safe_id_fingerprint(call_id),
            result_label,
            output_sent,
            len(output.encode("utf-8")),
            pending_confirmation,
            bool(isinstance(output_payload, dict) and output_payload.get("verified")),
            must_continue,
        )
        if output_sent and self._pending_tools == 0:
            goal_status = goal.get("status") if isinstance(goal, dict) else None
            terminal_speech = bool(
                (isinstance(output_payload, dict) and output_payload.get("goal_complete"))
                or goal_status in {"complete", "failed", "cancelled"}
                or (
                    isinstance(output_payload, dict)
                    and output_payload.get("cancelled")
                )
            )
            create = self._response_create_after_tool(
                must_continue=must_continue and not terminal_speech,
                terminal_speech=terminal_speech or (not must_continue),
            )
            continuation_sent = await self._send(create)
            if continuation_sent:
                self._continuation_sent = True
                self._audio_accepting = True
            logger.warning(
                "realtime_trace event=response.create.continuation provider=%s function_name=%s call_id_fingerprint=%s sent=%s",
                self._provider,
                name,
                _safe_id_fingerprint(call_id),
                continuation_sent,
            )

    def _validate_function_call(self, name: str, arguments: dict) -> tuple[dict, str | None]:
        if not name:
            return {}, "Realtime returned an empty function name."
        if not self._upstream_session_ready:
            return {}, "Realtime provider has not acknowledged the session tool projection."
        if self._provider_mismatch or name not in self._upstream_tool_names:
            return {}, f"Realtime provider did not acknowledge live function '{name}'."
        specs = grok_voice_tools(self._tool_specs)
        spec = next(
            (
                item
                for item in specs
                if isinstance(item, dict) and str(item.get("name") or "") == name
            ),
            None,
        )
        if spec is None:
            return {}, f"Unknown or unapproved live function '{name}'."
        from app.gateway.validation import validate_arguments

        parameters = spec.get("parameters") or {}
        args = dict(arguments or {})
        properties = parameters.get("properties") or {}
        if name in REQUIRED_COMPUTER_TOOLS and properties:
            args = {key: value for key, value in args.items() if key in properties}
        effective, issues = validate_arguments(args, parameters)
        if issues:
            return {}, "; ".join(issues)
        return effective, None

    async def _deliver_camera_images(self, name: str, call_id: str, output: str) -> str:
        """Inject captured JPEGs as Realtime input_image items, then return compact tool JSON.

        Function output stays text-only. Pixels travel on conversation.item.create.
        """

        observations = pop_observations(call_id)
        try:
            payload = json.loads(output) if output else {}
        except (TypeError, json.JSONDecodeError):
            payload = {}
        body = payload.get("result") if isinstance(payload.get("result"), dict) else payload
        if not isinstance(body, dict):
            body = {}
        delivered = 0
        for index, observation in enumerate(observations):
            event_id = f"cam-{call_id}-{index}"
            item = build_realtime_image_item(
                observation.jpeg,
                mime=observation.mime,
                detail=observation.detail,
                event_id=event_id,
                prompt=(
                "Camera observation from the owner's MacBook."
                if name == "look"
                else "Window screenshot from the owner's Mac. Describe only visible UI."
                if name == "screen_look"
                else f"Camera observation {index + 1} from a bounded watch."
            ),
            )
            log_camera(
                "camera.realtime_image_sent",
                request_id=observation.request_id,
                extra={
                    "width": observation.width,
                    "height": observation.height,
                    "encoded_bytes": len(observation.jpeg),
                    "sequence": observation.sequence,
                    "session_model": self._model,
                },
            )
            sent = await self._send(item, timeout_s=8.0)
            if sent:
                delivered += 1
                log_camera(
                    "camera.realtime_image_accepted",
                    request_id=observation.request_id,
                    extra={"sent": True, "event_id": event_id},
                )
            else:
                log_camera(
                    "camera.failure",
                    request_id=observation.request_id,
                    extra={"error": "realtime_image_send_failed"},
                )
        image_ok = delivered > 0
        compact = {
            "ok": bool(body.get("ok") and image_ok) if observations else bool(body.get("ok")),
            "name": name,
            "spoken": body.get("spoken"),
            "error": body.get("error") if not image_ok and observations else body.get("error"),
            "image_delivered": image_ok,
            "model_image_delivered": image_ok,
            "frames": delivered,
            "width": body.get("width"),
            "height": body.get("height"),
            "encoded_bytes": body.get("encoded_bytes"),
            "request_id": body.get("request_id"),
            "frame_id": body.get("frame_id"),
            "app": body.get("app"),
            "window": body.get("window"),
            "local_ocr": body.get("local_ocr"),
            "source": body.get("source"),
        }
        if observations and not image_ok:
            compact["ok"] = False
            compact["spoken"] = (
                "The camera frame was captured but the live model did not receive "
                "the image. I did not see anything."
            )
            compact["error"] = "realtime_image_rejected"
        if not observations and name in {"look", "observe_camera", "screen_look"}:
            compact["image_delivered"] = False
            compact["model_image_delivered"] = False
        log_camera(
            "camera.tool_completed",
            request_id=str(compact.get("request_id") or call_id),
            extra={
                "frames": delivered,
                "ok": compact.get("ok"),
                "image_delivered": image_ok,
            },
        )
        return json.dumps(compact, default=str, separators=(",", ":"))

    async def _send_function_output(self, call_id: str, output: str) -> bool:
        if not call_id:
            return False
        sent = await self._send(
            {
                "type": "conversation.item.create",
                "item": {
                    "type": "function_call_output",
                    "call_id": call_id,
                    "output": output,
                },
            }
        )
        logger.warning(
            "realtime_trace event=function_call_output.sent provider=%s call_id_fingerprint=%s sent=%s output_bytes=%s",
            self._provider,
            _safe_id_fingerprint(call_id),
            sent,
            len(output.encode("utf-8")),
        )
        return sent

    async def continue_after_approval(
        self,
        name: str,
        result: dict,
        *,
        call_id: str | None = None,
    ) -> bool:
        """Give an approved result back to Realtime and request spoken output."""

        if self._closed or self._ws is None:
            return False
        key = str(call_id or "")
        if key and key in self._pending_confirmation_calls:
            self._pending_confirmation_calls.pop(key, None)
            # The hold result was already returned to the original function
            # call so Realtime could speak the confirmation request. The
            # approved result is a new authoritative conversation turn.
        compact = json.dumps(
            {"tool": name, "approved_result": result},
            default=str,
            separators=(",", ":"),
        )[:8000]
        self._audio_accepting = True
        if not await self._send(
            {
                "type": "conversation.item.create",
                "item": {
                    "type": "message",
                    "role": "user",
                    "content": [
                        {
                            "type": "input_text",
                            "text": (
                                "The previously approved EV function completed. "
                                "Speak the verified result briefly and do not infer "
                                f"anything beyond this evidence: {compact}"
                            ),
                        }
                    ],
                },
            }
        ):
            return False
        return await self._send({"type": "response.create"})


def _decode_function_arguments(raw: Any) -> tuple[dict, str | None]:
    if raw is None or raw == "":
        return {}, None
    if isinstance(raw, dict):
        return dict(raw), None
    if not isinstance(raw, str):
        return {}, "Function arguments must be a JSON object."
    if len(raw.encode("utf-8")) > _MAX_FUNCTION_ARGUMENT_BYTES:
        return {}, "Function arguments exceed the realtime size limit."
    try:
        decoded = json.loads(raw)
    except json.JSONDecodeError as exc:
        return {}, f"Function arguments are not valid JSON: {exc.msg}."
    if not isinstance(decoded, dict):
        return {}, "Function arguments must decode to a JSON object."
    return decoded, None


def _function_output_is_pending(raw: str) -> bool:
    try:
        payload = json.loads(raw) if raw else {}
    except json.JSONDecodeError:
        return False
    if not isinstance(payload, dict):
        return False
    body = payload.get("result") if isinstance(payload.get("result"), dict) else payload
    return bool(
        payload.get("confirmation_required")
        or payload.get("needs_confirm")
        or payload.get("hold")
        or body.get("confirmation_required")
        or body.get("needs_confirm")
        or body.get("hold")
        or body.get("error") == "confirmation_required"
    )


def _parse_event(message: Any) -> dict | None:
    if isinstance(message, dict):
        return message
    if isinstance(message, (bytes, bytearray)):
        try:
            return json.loads(message.decode("utf-8"))
        except Exception:  # noqa: BLE001
            return None
    if isinstance(message, str):
        try:
            return json.loads(message)
        except json.JSONDecodeError:
            return None
    return None


def _part_transcript(part: object) -> str:
    if isinstance(part, str) and part.strip():
        return part.strip()
    if not isinstance(part, dict):
        return ""
    for key in ("transcript", "text"):
        value = part.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


def _event_response_id(event: dict) -> str | None:
    raw = event.get("response_id")
    if isinstance(raw, str) and raw.strip():
        return raw.strip()
    response = event.get("response")
    if isinstance(response, dict):
        rid = response.get("id")
        if isinstance(rid, str) and rid.strip():
            return rid.strip()
    return None


def _event_item_id(event: dict) -> str | None:
    raw = event.get("item_id")
    if isinstance(raw, str) and raw.strip():
        return raw.strip()
    item = event.get("item")
    if isinstance(item, dict):
        item_id = item.get("id")
        if isinstance(item_id, str) and item_id.strip():
            return item_id.strip()
    return None


def _item_user_transcript(item: dict) -> str:
    if item.get("role") != "user":
        return ""
    for key in ("transcript", "text"):
        value = item.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    raw = item.get("content")
    if isinstance(raw, list):
        for part in raw:
            text = _part_transcript(part)
            if text:
                return text
        return ""
    return _part_transcript(raw)


def _transcript_text(event: dict) -> str:
    for key in ("transcript", "text", "delta"):
        value = event.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    nested = event.get("item")
    if isinstance(nested, dict):
        for key in ("transcript", "text"):
            value = nested.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
        content = nested.get("content")
        if isinstance(content, list):
            for part in content:
                text = _part_transcript(part)
                if text:
                    return text
    content = event.get("content")
    if isinstance(content, list):
        for part in content:
            text = _part_transcript(part)
            if text:
                return text
    return _part_transcript(content) if isinstance(content, dict) else ""


def _audio_cv_trace_enabled() -> bool:
    """Audio continuity forensics toggle (P0 periodic-stall investigation).

    Env-gated so the diagnostic hot-path logging never runs in production.
    """
    import os

    return os.environ.get("EV_AUDIO_CV_TRACE") == "1"


def _realtime_error_fields(event: dict) -> tuple[str, str]:
    err = event.get("error")
    if isinstance(err, dict):
        message = str(err.get("message") or err.get("code") or "realtime error")
        code = str(err.get("code") or "")
        return message, code
    return str(event.get("message") or event.get("error") or "realtime error"), ""


def _is_benign_realtime_error(message: str, code: str = "") -> bool:
    blob = f"{code} {message}".lower()
    return any(
        token in blob
        for token in (
            "no active response",
            "cancellation failed",
            "already cancelled",
            "already canceled",
            "response_cancel_not_active",
            "no in-progress",
            "already has an active response",
            "active response in progress",
            "response_cancel_none",
            "conversation.item.truncate",
            "item_truncate",
            "does not exist",
            "already truncated",
        )
    )


async def _default_connect(url: str, additional_headers: dict | None = None):
    try:
        from websockets.asyncio.client import connect
    except ImportError as exc:  # pragma: no cover - env without uvicorn[standard]
        raise RuntimeError(
            "realtime voice needs the websockets package (install uvicorn[standard])"
        ) from exc
    return await connect(
        url,
        additional_headers=additional_headers or {},
        open_timeout=20,
        ping_interval=20,
        ping_timeout=20,
    )
