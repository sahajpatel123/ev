"""Spoken-unit splitting and the owner-facing speech / playback gate."""

from __future__ import annotations

import io
import json
import re
import wave
from dataclasses import dataclass

_HARD = re.compile(r"^(.*?[.!?][\"']*)(\s+|$)")
_SOFT = re.compile(r"^(.{16,}?(?:,|;|:| — | – ))")
_WORDS = re.compile(r"^((?:\S+\s+){4}\S+)(\s+)")
_SEARCH = re.compile(
    r"\b(search|look up|look it up|google|find online|on the (?:web|internet)|wikipedia)\b",
    re.IGNORECASE,
)
_CHECK = re.compile(
    r"\b(calendar|schedule|what's next|what is next|remind|remember|memory|"
    r"health|sleep|status|vitals|where is|who is)\b",
    re.IGNORECASE,
)
_WAIT = re.compile(
    r"\b(send|email|message|text|call|book|write|do that|go ahead)\b",
    re.IGNORECASE,
)
_EVIE_PREFIX = re.compile(
    r"^(?:hey|ok|okay|hi|hello)?\s*"
    r"(?:evie+|eevee|evy|evi|eve|ivy|every|ee\s*vee)(?:\s+here)?\b[\s,!.?\-]*",
    re.IGNORECASE,
)
_QUESTION = re.compile(
    r"^(what|what's|whats|how|who|where|why|when|is|are|can|do|does|did|will|would|could)\b",
    re.IGNORECASE,
)
_SOFT_ASK = re.compile(r"\b(please|could you|can you|would you)\b", re.IGNORECASE)

LISTEN_ACKS = ("Yes?", "Hmm.", "Mhm.", "Yes.")
# Short tokens that are real speech, not Whisper leftovers.
_SAFE_SHORT_TOKENS = {
    "ok",
    "okay",
    "yes",
    "no",
    "hi",
    "hey",
    "yo",
    "wow",
    "nah",
    "yep",
    "nope",
    "bye",
    "sup",
    "hmm",
    "mhm",
    "mm",
    "huh",
    "oh",
    "ah",
    "ya",
    "yeah",
}
_TIMEOUT_LEFTOVER = re.compile(
    r"\b(?:asr[_ ]?timeout|timed? out|timeout leftover|gateway timeout|"
    r"speech recognition took too long)\b",
    re.IGNORECASE,
)
_PRESENCE_CHECK = re.compile(
    r"\b(?:"
    r"can you hear me|do you hear me|you hear me|"
    r"are you (?:there|here|listening)|"
    r"you (?:there|listening)"
    r")\b",
    re.IGNORECASE,
)

_TOOL_CALLS_KEY = re.compile(r'"tool_calls"\s*:')
_FN_SHAPE = re.compile(
    r"\b(?:function_call|tool_call|tool_calls)\b|"
    r"\b(?:search_web|get_weather|search_memory|calculate|place_call|"
    r"send_message|open_url|set_reminder|list_messages|list_mail|"
    r"resolve_contact|get_upcoming_alerts)\s*\(",
    re.IGNORECASE,
)
_CALLING_TOOL = re.compile(
    r"^(?:calling tool|invoking|tool result|function result|backend call)\b",
    re.IGNORECASE,
)
_TRACE = re.compile(
    r"^(?:traceback \(most recent call last\)|exception:|error:|debug:|trace:)",
    re.IGNORECASE,
)
_FENCE = re.compile(r"^```(?:json)?\s*|\s*```$", re.IGNORECASE)
PTT_CLIENT_OWNER = "push_to_talk"
PTT_ECHO_GRACE_SECONDS = 4.0
# After playback *ends*, leftover speaker audio is still in the room.
# Window is duration_s + this tail — not a fixed clock from generation.
# Matches ears `echo_tail_s` (0.6) plus slack for ASR lag.
ECHO_TAIL_SECONDS = 1.0
ECHO_GRACE_SECONDS = ECHO_TAIL_SECONDS
# Conversational read-aloud is ~0.35s/word (170 wpm). Neural TTS (edge /
# Kokoro / Siri-class) is slower, with pauses: live EVIE mp3 of a 20-word
# stuck-mic line played ~12.7s. 0.7s/word covers that when duration_ms
# is missing (mp3). Prefer parsed audio bytes when we have them.
SPOKEN_SECONDS_PER_WORD = 0.7
_WAKE_ONLY_NAME = re.compile(
    r"^(?:hey |ok |okay |hi |hello )?"
    r"(?:evie+|eevee|evy|evi|eve|evil|every|ee vee|ivy)"
    r"(?: here)?$",
    re.IGNORECASE,
)
_NEW_TURN_HINT = re.compile(
    r"\b(what|whats|what's|how|who|where|why|when|remind|text|call|send|"
    r"weather|calendar|next|time|date|search|look|show|open|make|change|"
    r"actually|also|then|set|add|buy)\b",
    re.IGNORECASE,
)
_LAST_SPOKEN: dict[str, dict] = {}


def starts_with_evie(text: str) -> bool:
    return bool(_EVIE_PREFIX.search((text or "").strip()))


def strip_wake_prefix(text: str) -> str:
    """Remove a leading Evie / hey-Evie address so the rest is the command.

    Sentence-final punctuation on the command is kept so live turn-taking
    can still see a finished thought. Trailing punctuation glued to the
    name is already consumed by ``_EVIE_PREFIX``.
    """

    raw = (text or "").strip()
    stripped = _EVIE_PREFIX.sub("", raw, count=1)
    if stripped == raw:
        return raw
    return stripped.strip(" ,-")


def is_presence_check(text: str) -> bool:
    """True when the owner is checking whether EVIE can hear them."""

    return bool(_PRESENCE_CHECK.search((text or "").strip()))


def is_unreadable_transcript(text: str) -> bool:
    """True for empty, timeout dumps, and DHM-class ASR leftovers.

    A 1–3 letter consonant clump ("DHM"), a timeout leftover, or a raw
    error dump is not owner speech and must not be spoken as the answer.
    """

    raw = (text or "").strip()
    if not raw:
        return True
    if any(_TRACE.search(line.strip()) for line in raw.splitlines() if line.strip()):
        return True
    if _TIMEOUT_LEFTOVER.search(raw) and len(raw.split()) <= 8:
        return True
    letters = re.sub(r"[^A-Za-z]+", "", raw)
    compact = letters.lower()
    if not compact:
        digits = re.sub(r"\s+", "", raw)
        return not digits.isdigit()
    if compact in _SAFE_SHORT_TOKENS:
        return False
    vowels = set("aeiouy")
    if len(compact) <= 3 and not any(char in vowels for char in compact):
        return True
    tokens = raw.split()
    return (
        len(tokens) == 1
        and len(compact) <= 2
        and compact not in _SAFE_SHORT_TOKENS
    )


def normalize_spoken(text: str) -> str:
    return re.sub(r"[^a-z0-9 ]+", " ", (text or "").lower()).strip()


def is_wake_only_name(text: str) -> bool:
    """True when the clip is only Eve/EVIE (and the spoken aliases)."""

    normalized = normalize_spoken(text)
    if not normalized:
        return True
    if _WAKE_ONLY_NAME.fullmatch(normalized):
        return True
    from app.voice.wake import PhraseWakeEngine, WhisperPhraseWakeEngine

    return normalized in PhraseWakeEngine.WAKE_PHRASES or normalized in {
        "eve",
        "hey eve",
        "hi eve",
        "hello eve",
        "ok eve",
        "okay eve",
        "eve here",
        *WhisperPhraseWakeEngine.WAKE_PHRASES,
    }


def is_listen_ack_text(text: str) -> bool:
    normalized = normalize_spoken(text)
    if not normalized:
        return False
    acks = {normalize_spoken(item) for item in LISTEN_ACKS}
    acks.update({"yes", "hmm", "mhm", "mm", "uh huh"})
    return normalized in acks


def is_echo_of_last_reply(heard: str, last_reply: str | None) -> bool:
    """True when the mic likely heard our own last spoken line."""

    heard_n = normalize_spoken(heard)
    last_n = normalize_spoken(last_reply or "")
    if not heard_n:
        return False
    if is_listen_ack_text(heard_n):
        return True
    if not last_n:
        return False
    if heard_n == last_n:
        return True
    if len(heard_n) >= 4 and len(last_n) >= 4 and (
        heard_n in last_n or last_n in heard_n
    ):
        return True
    heard_words = set(heard_n.split())
    last_words = set(last_n.split())
    if not heard_words or not last_words:
        return False
    overlap = len(heard_words & last_words) / max(len(heard_words), 1)
    return overlap >= 0.7 and len(heard_words) <= len(last_words) + 2


def looks_like_new_owner_turn(heard: str) -> bool:
    """True only for a real command/question — not leftover Whisper junk.

    Three-plus words is not enough: "Thanks for watching" is a classic
    ASR tail, not barge-in. Wake-only Eve stays a listen door, not a turn.
    """

    if is_wake_only_name(heard):
        return False
    text = heard or ""
    if text.strip().endswith("?"):
        return True
    if _NEW_TURN_HINT.search(text):
        return True
    return bool(_SOFT_ASK.search(text))


def _skip_id3v2(audio: bytes) -> int:
    if len(audio) < 10 or audio[:3] != b"ID3":
        return 0
    size = (
        (audio[6] & 0x7F) << 21
        | (audio[7] & 0x7F) << 14
        | (audio[8] & 0x7F) << 7
        | (audio[9] & 0x7F)
    )
    return 10 + size


_MPEG_SR = {
    3: (44100, 48000, 32000),
    2: (22050, 24000, 16000),
    0: (11025, 12000, 8000),
}
_MPEG_BITRATE = {
    (3, 3): (0, 32, 64, 96, 128, 160, 192, 224, 256, 288, 320, 352, 384, 416, 448, 0),
    (3, 2): (0, 32, 48, 56, 64, 80, 96, 112, 128, 160, 192, 224, 256, 320, 384, 0),
    (3, 1): (0, 32, 40, 48, 56, 64, 80, 96, 112, 128, 160, 192, 224, 256, 320, 0),
    (2, 3): (0, 32, 48, 56, 64, 80, 96, 112, 128, 144, 160, 176, 192, 224, 256, 0),
    (2, 2): (0, 8, 16, 24, 32, 40, 48, 56, 64, 80, 96, 112, 128, 144, 160, 0),
    (2, 1): (0, 8, 16, 24, 32, 40, 48, 56, 64, 80, 96, 112, 128, 144, 160, 0),
}


def _mp3_frame(header: int) -> tuple[int, int, int] | None:
    """Return (frame_bytes, samples, sample_rate) or None if not a frame."""

    if header & 0xFFE00000 != 0xFFE00000:
        return None
    version_id = (header >> 19) & 3
    layer_id = (header >> 17) & 3
    bitrate_idx = (header >> 12) & 0xF
    sr_idx = (header >> 10) & 3
    padding = (header >> 9) & 1
    if version_id == 1 or layer_id == 0 or bitrate_idx in {0, 15} or sr_idx == 3:
        return None
    version_key = 3 if version_id == 3 else 2
    table = _MPEG_BITRATE.get((version_key, layer_id))
    rates = _MPEG_SR.get(version_id)
    if not table or not rates:
        return None
    bitrate = table[bitrate_idx] * 1000
    sample_rate = rates[sr_idx]
    if version_id == 3:
        samples = 384 if layer_id == 3 else 1152
    else:
        samples = 384 if layer_id == 3 else (1152 if layer_id == 2 else 576)
    if layer_id == 3:
        length = (12 * bitrate // sample_rate + padding) * 4
    elif version_id == 3:
        length = 144 * bitrate // sample_rate + padding
    else:
        length = 72 * bitrate // sample_rate + padding
    if length < 4:
        return None
    return length, samples, sample_rate


def _mp3_duration_s(audio: bytes) -> float | None:
    """Best-effort MPEG duration from Xing frames or a CBR/VBR walk."""

    offset = _skip_id3v2(audio)
    if offset + 4 > len(audio):
        return None
    header = int.from_bytes(audio[offset : offset + 4], "big")
    first = _mp3_frame(header)
    if first is None:
        return None
    frame_len, samples, sample_rate = first
    # Xing / Info: frame count is exact for VBR (edge-tts, lame).
    channels_mono = ((header >> 6) & 3) == 3
    side = (
        (17 if channels_mono else 32)
        if ((header >> 19) & 3) == 3
        else (9 if channels_mono else 17)
    )
    xing_at = offset + 4 + side
    tag = audio[xing_at : xing_at + 4]
    if tag in {b"Xing", b"Info"} and xing_at + 8 <= len(audio):
        flags = int.from_bytes(audio[xing_at + 4 : xing_at + 8], "big")
        if flags & 1 and xing_at + 12 <= len(audio):
            frames = int.from_bytes(audio[xing_at + 8 : xing_at + 12], "big")
            if frames > 0 and sample_rate:
                return frames * samples / float(sample_rate)
    frames = 0
    total_samples = 0
    pos = offset
    end = len(audio)
    while pos + 4 <= end:
        hdr = int.from_bytes(audio[pos : pos + 4], "big")
        parsed = _mp3_frame(hdr)
        if parsed is None:
            pos += 1
            continue
        length, frame_samples, _rate = parsed
        if length <= 0 or pos + length > end + 1:
            break
        frames += 1
        total_samples += frame_samples
        pos += length
        if frames > 200_000:
            break
    if frames == 0 or not sample_rate:
        return None
    return total_samples / float(sample_rate)


def audio_duration_s(audio: bytes | None, *, content_type: str | None = None) -> float | None:
    """Seconds of playable audio, or None when the payload is not parseable."""

    if not audio:
        return None
    kind = (content_type or "").lower()
    if audio.startswith(b"RIFF") or "wav" in kind:
        try:
            with wave.open(io.BytesIO(audio), "rb") as src:
                rate = src.getframerate()
                frames = src.getnframes()
            if rate and frames:
                return frames / float(rate)
        except (wave.Error, EOFError):
            return None
        return None
    if (
        audio[:3] == b"ID3"
        or audio[:2] in {b"\xff\xfb", b"\xff\xfa", b"\xff\xf3", b"\xff\xf2", b"\xff\xe3"}
        or "mpeg" in kind
        or "mp3" in kind
    ):
        return _mp3_duration_s(audio)
    return None


def estimate_spoken_duration_s(
    text: str,
    *,
    duration_ms: int | None = None,
    audio: bytes | None = None,
    content_type: str | None = None,
) -> float:
    """How long this line occupies the speaker, including long mp3 replies.

    Prefer measured ``duration_ms`` (wav) or parsed audio bytes (mp3).
    Non-wav TTS often leaves ``duration_ms`` unset — do not use a fast
    0.35s/word read-aloud rate or a 6–12s neural reply goes unguarded.
    """

    if duration_ms:
        return max(0.6, float(duration_ms) / 1000.0)
    parsed = audio_duration_s(audio, content_type=content_type)
    if parsed is not None and parsed >= 0.4:
        return parsed
    words = len((text or "").split())
    return max(0.8, words * SPOKEN_SECONDS_PER_WORD)


def echo_hold_seconds(duration_s: float | None = None) -> float:
    """Residual ASR window: playback length plus a room tail."""

    return max(0.0, float(duration_s or 0.0)) + ECHO_TAIL_SECONDS


def remember_spoken(
    device_id: str,
    text: str,
    *,
    duration_s: float = 1.5,
    now=None,
) -> None:
    """Record what EVIE just said so the next mic clip can be rejected as echo."""

    key = str(device_id or "")
    if not key:
        return
    from app.utils.text import utcnow

    stamp = now or utcnow()
    _LAST_SPOKEN[key] = {
        "text": text or "",
        "at": stamp,
        "duration_s": max(0.2, float(duration_s)),
    }


def last_spoken(device_id: str) -> dict | None:
    return _LAST_SPOKEN.get(str(device_id or ""))


def clear_spoken(device_id: str | None = None) -> None:
    if device_id is None:
        _LAST_SPOKEN.clear()
        return
    _LAST_SPOKEN.pop(str(device_id), None)


def is_playback_window(device_id: str, now=None) -> bool:
    row = last_spoken(device_id)
    if row is None:
        return False
    from app.utils.text import utcnow

    stamp = now or utcnow()
    spoken_at = row.get("at")
    if spoken_at is None:
        return False
    try:
        age = (stamp - spoken_at).total_seconds()
    except TypeError:
        return False
    return 0 <= age <= float(row.get("duration_s") or 0) + 0.25


def should_drop_as_echo(
    heard: str,
    *,
    last_reply: str | None,
    spoken_at=None,
    now=None,
    playing: bool = False,
    duration_s: float | None = None,
) -> bool:
    """Drop our own playback / residual ASR. Wake-only Eve is never echo.

    Residual window is ``duration_s + ECHO_TAIL_SECONDS`` from when we
    started speaking (i.e. until playback_end + tail), not a 3.5s clock
    from generation.
    """

    if is_wake_only_name(heard):
        return False
    if playing and not looks_like_new_owner_turn(heard):
        return True
    if is_echo_of_last_reply(heard, last_reply):
        return True
    if spoken_at is None or now is None:
        return False
    try:
        age = (now - spoken_at).total_seconds()
    except TypeError:
        return False
    window = echo_hold_seconds(duration_s)
    if age < 0 or age > window:
        return False
    return not looks_like_new_owner_turn(heard)


def choose_listen_ack(text: str) -> str:
    """Human listen cue after the owner says Evie — varies with the rest."""

    rest = strip_wake_prefix(text)
    if not rest:
        return "Yes?"
    if _SOFT_ASK.search(rest):
        return "Mhm."
    if _QUESTION.search(rest) or rest.endswith("?"):
        return "Hmm."
    return "Yes."


def listen_ack_style():
    """Warmer, quieter delivery than a clipped command reply."""

    from app.voice.contracts import SpeechStyle

    return SpeechStyle(warmth=0.95, urgency=0.08, brevity=0.9, mode="casual")


def answer_speech_style():
    """Default style for the actual answer after the listen-ack."""

    from app.voice.contracts import SpeechStyle

    return SpeechStyle(warmth=0.72, urgency=0.32, brevity=0.45, mode="command")


def choose_voice_filler(text: str) -> str:
    """Short spoken bridge while chat/TTS continue — ChatGPT-voice style."""

    raw = (text or "").strip()
    if _SEARCH.search(raw):
        return "Searching."
    if _WAIT.search(raw):
        return "One second."
    if _CHECK.search(raw):
        return "Checking."
    return "On it."


def pop_speakable(buffer: str, *, flush: bool = False) -> tuple[str | None, str]:
    """Take the next speakable chunk off ``buffer``.

    Prefers a sentence end, then a clause pause, then eight words — so the
    first audio can start while DeepSeek is still generating the rest.
    """

    if not buffer:
        return None, buffer
    leading = len(buffer) - len(buffer.lstrip())
    text = buffer[leading:]
    hard = _HARD.match(text)
    if hard and len(hard.group(1).split()) >= 2:
        return hard.group(1).strip(), text[hard.end() :]
    soft = _SOFT.match(text)
    if soft:
        return soft.group(1).strip(), text[soft.end() :]
    words = _WORDS.match(text)
    if words:
        return words.group(1).strip(), text[words.end() :]
    if flush and text.strip():
        return text.strip(), ""
    return None, buffer if leading == 0 else buffer[:leading] + text


def _json_object_span(text: str) -> str | None:
    start = text.find("{")
    end = text.rfind("}")
    if start < 0 or end <= start:
        return None
    return text[start : end + 1]


def is_tool_chatter(text: str) -> bool:
    """True when ``text`` is backend/tool-loop residue, not owner-facing speech."""

    raw = (text or "").strip()
    if not raw:
        return False
    if _TOOL_CALLS_KEY.search(raw) and '"reply"' not in raw:
        return True
    if _FN_SHAPE.search(raw) and len(raw.split()) < 8:
        return True
    if _CALLING_TOOL.search(raw):
        return True
    if _TRACE.search(raw):
        return True
    return raw.startswith("{") and ("tool_calls" in raw or "function_call" in raw)


def owner_facing_speech(text: str) -> str | None:
    """Return the clear owner-facing reply, or None if nothing should be spoken.

    Tool-call JSON, function-call traces, and raw error dumps stay internal.
    A structured envelope with a ``reply`` field yields only that reply.
    """

    raw = (text or "").strip()
    if not raw:
        return None
    if any(_TRACE.search(line.strip()) for line in raw.splitlines()):
        return None
    fenced = _FENCE.sub("", raw).strip()
    candidate = _json_object_span(fenced) or fenced
    try:
        payload = json.loads(candidate)
    except json.JSONDecodeError:
        payload = None
    if isinstance(payload, dict):
        reply = payload.get("reply")
        if isinstance(reply, str) and reply.strip():
            raw = reply.strip()
        elif payload.get("tool_calls") or payload.get("function_call"):
            return None
    lines: list[str] = []
    for line in raw.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        if is_tool_chatter(stripped):
            continue
        if stripped.startswith("{") and "tool_calls" in stripped:
            continue
        lines.append(stripped)
    cleaned = " ".join(lines).strip()
    if not cleaned or is_tool_chatter(cleaned):
        return None
    if is_unreadable_transcript(cleaned):
        return None
    return cleaned


@dataclass(frozen=True)
class PlaybackDecision:
    play_tts: bool
    invoke_say: bool
    reason: str


def session_playback_owner(verifier_name: str | None) -> str:
    """Which surface may speak for this session: ``ears``, ``client``, or ``none``."""

    if verifier_name == PTT_CLIENT_OWNER:
        return "client"
    return "ears"


def ears_device_matches_winner(
    *,
    ears_device_id: str,
    winner_name: str | None = None,
    winner_id: str | None = None,
    winner_type: str | None = None,
) -> bool:
    """True when the always-on Mac is the same physical winner as ``winner_*``."""

    ears = (ears_device_id or "").strip().lower()
    name = (winner_name or "").strip().lower().replace(" ", "-")
    wid = str(winner_id or "").strip().lower()
    dtype = (winner_type or "").strip().lower()
    if ears and ears == wid:
        return True
    if name and ears and (name == ears or name in ears or ears in name):
        return True
    return dtype in {"mac", "desktop"} and (ears.startswith("mac") or "mac" in ears)


def tts_is_playable(tts) -> bool:
    """True when a TTS payload has bytes or a fetchable ref."""

    if tts is None:
        return False
    if isinstance(tts, dict):
        return bool(tts.get("audio_b64") or tts.get("audio_ref"))
    return bool(getattr(tts, "audio", None) or getattr(tts, "audio_ref", None))


def ears_should_handle_follow_up(
    *,
    verifier_name: str | None,
    last_utterance_at,
    now,
    busy: bool,
) -> bool:
    """False when the menu-bar already owns this turn (busy or just-finished PTT)."""

    if busy:
        return False
    if verifier_name == PTT_CLIENT_OWNER and last_utterance_at is not None and now is not None:
        try:
            age = (now - last_utterance_at).total_seconds()
        except TypeError:
            return True
        if 0 <= age < PTT_ECHO_GRACE_SECONDS:
            return False
    return True


def decide_playback(
    *,
    has_tts_audio: bool = False,
    audio_ref: str | None = None,
    already_played: bool = False,
    owner: str = "ears",
    surface: str = "ears",
) -> PlaybackDecision:
    """One speaker per turn. ``say`` is never a parallel voice next to TTS."""

    if already_played:
        return PlaybackDecision(False, False, "already_played")
    if owner not in {surface, "any"}:
        return PlaybackDecision(False, False, "other_surface_owns")
    if has_tts_audio or audio_ref:
        return PlaybackDecision(True, False, "tts")
    return PlaybackDecision(False, False, "silent")


def concat_wav_bytes(parts: list[bytes]) -> bytes | None:
    """Join 16-bit WAV payloads into one file. Returns None if nothing usable."""

    usable = [part for part in parts if part]
    if not usable:
        return None
    if len(usable) == 1:
        return usable[0]
    frames: list[bytes] = []
    rate = width = channels = None
    for part in usable:
        try:
            with wave.open(io.BytesIO(part), "rb") as src:
                if rate is None:
                    rate = src.getframerate()
                    width = src.getsampwidth()
                    channels = src.getnchannels()
                elif (
                    src.getframerate() != rate
                    or src.getsampwidth() != width
                    or src.getnchannels() != channels
                ):
                    continue
                frames.append(src.readframes(src.getnframes()))
        except (wave.Error, EOFError):
            continue
    if not frames or rate is None or width is None or channels is None:
        return usable[0]
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as out:
        out.setnchannels(channels)
        out.setsampwidth(width)
        out.setframerate(rate)
        out.writeframes(b"".join(frames))
    return buffer.getvalue()
