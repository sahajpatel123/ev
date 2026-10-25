"""The workspace runs exactly two models: the Muse Spark brain (OpenRouter) and
Gemini Live (Google AI Studio).

This is the guard that keeps a retired model from drifting back in through a
copy-pasted provider, a default string, or a line in a prompt. Every remaining
mention of a retired name in production code is listed below with the reason
it is allowed; anything else fails the test and must either be removed or be
added here with a reason a reviewer can disagree with.
"""

from __future__ import annotations

import re
from pathlib import Path

from app.config import Settings

REPO = Path(__file__).resolve().parents[2]
SCAN_ROOTS = ("backend/app", "backend/clients", "scripts")
SCAN_SUFFIXES = {".py", ".js", ".sh"}

# Retired model / vendor names. `mimo_kernel` is the retained wire name of the
# single-brain topology (the shipped PWA reads `cognitive.mimo_kernel`) and is
# removed from the haystack before matching.
FORBIDDEN = re.compile(
    r"(?i)mimo|xiaomi|deepseek|x\.ai\b|typesafe|\bjev\b|jev-\d|\bgpt-\d|grok"
    r"|gemini-(?:1|2)\.|gemini-3\.[0-7]\b|gemini-3-|whisper-1|text-embedding-3"
    r"|api\.openai\.com|perplexity/"
)
RETAINED_WIRE_NAMES = ("MIMO_KERNEL", "mimo_kernel")

_BLOCKLIST = "retired-name blocklist/refusal text (names what is refused)"
_GROK_REF = "comment about the reference Grok harness (behavior spec only, R15)"
_OPENAI_COMPAT = "generic OpenAI-compatible ASR/TTS/embedding adapter default (off unless the owner configures that endpoint)"
_OCR = "self-hosted DeepSeek-OCR provider: OCR is a permitted local-model class (AGENTS.md)"

ALLOWED: dict[str, tuple[frozenset[str], str]] = {
    "backend/app/agent/brain.py": (frozenset({"deepseek", "mimo"}), _BLOCKLIST),
    "backend/app/gateway/providers.py": (frozenset({"deepseek", "mimo"}), _BLOCKLIST),
    "backend/app/cognitive/mode.py": (frozenset({"mimo"}), "legacy EV_COGNITIVE_MODE=mimo input alias"),
    "backend/app/device_gateway/mobile_v2.py": (
        frozenset({"deepseek", "gpt-4", "gpt-5", "grok", "jev", "mimo"}),
        "phone two-model enforcement: names the brains the phone path refuses",
    ),
    "scripts/start_talk_sidecar.py": (
        frozenset({"deepseek", "grok", "jev", "mimo", "xiaomi"}),
        "sidecar retired-brain refusal and leftover-env sets",
    ),
    "backend/app/agent/capability_bridge.py": (frozenset({"grok"}), _GROK_REF),
    "backend/app/agent/compaction.py": (frozenset({"grok"}), _GROK_REF),
    "backend/app/agent/tools.py": (frozenset({"grok"}), _GROK_REF),
    "backend/app/memory/extraction.py": (frozenset({"grok"}), "comment about an imported Grok chat export"),
    "backend/app/scripts/chat_history_import.py": (frozenset({"grok"}), "parses Grok chat exports (data import, not a model)"),
    "backend/app/config.py": (
        frozenset({"deepseek", "gpt-4", "text-embedding-3", "whisper-1"}),
        _OPENAI_COMPAT + "; vision_provider option name deepseek_ocr",
    ),
    "backend/app/voice/asr.py": (frozenset({"gpt-4", "whisper-1"}), _OPENAI_COMPAT),
    "backend/app/voice/wake.py": (frozenset({"gpt-4", "whisper-1"}), _OPENAI_COMPAT),
    "backend/app/voice/tts.py": (frozenset({"gpt-4"}), _OPENAI_COMPAT),
}
ALLOWED_PREFIXES: dict[str, tuple[frozenset[str], str]] = {
    "backend/app/vision/": (frozenset({"deepseek"}), _OCR),
}


def _production_files() -> list[Path]:
    files: list[Path] = []
    for root in SCAN_ROOTS:
        for path in sorted((REPO / root).rglob("*")):
            if path.suffix in SCAN_SUFFIXES and "__pycache__" not in path.parts and path.is_file():
                files.append(path)
    return files


def _tokens(path: Path) -> set[str]:
    text = path.read_text(encoding="utf-8", errors="ignore")
    for wire in RETAINED_WIRE_NAMES:
        text = text.replace(wire, "")
    return {m.group(0).lower() for m in FORBIDDEN.finditer(text)}


def _allowed_for(rel: str) -> frozenset[str]:
    if rel in ALLOWED:
        return ALLOWED[rel][0]
    for prefix, (tokens, _reason) in ALLOWED_PREFIXES.items():
        if rel.startswith(prefix):
            return tokens
    return frozenset()


def test_no_retired_model_names_in_production_code() -> None:
    offenders: dict[str, list[str]] = {}
    for path in _production_files():
        rel = path.relative_to(REPO).as_posix()
        extra = _tokens(path) - set(_allowed_for(rel))
        if extra:
            offenders[rel] = sorted(extra)
    assert not offenders, (
        "retired model names found in production code; remove them, or add the "
        f"file to ALLOWED with a reason: {offenders}"
    )


def test_every_allowlist_entry_is_still_needed() -> None:
    """An exception that no longer matches anything is stale and must go."""

    stale: list[str] = []
    for rel, (tokens, _reason) in ALLOWED.items():
        found = _tokens(REPO / rel)
        if not found:
            stale.append(f"{rel}: no retired names remain")
        elif not found & tokens:
            stale.append(f"{rel}: none of {sorted(tokens)} remain")
    for prefix, (tokens, _reason) in ALLOWED_PREFIXES.items():
        found = set().union(*(_tokens(p) for p in _production_files()
                              if p.relative_to(REPO).as_posix().startswith(prefix)))
        if not found & tokens:
            stale.append(f"{prefix}: none of {sorted(tokens)} remain")
    assert not stale, stale


def test_registry_is_the_offline_doubles_plus_the_brain() -> None:
    from app.gateway.providers import PROVIDER_REGISTRY

    assert set(PROVIDER_REGISTRY) == {"echo", "mock", "muse"}


def test_the_two_model_ids_are_settings_with_verified_defaults() -> None:
    assert Settings.model_fields["brain_model"].default == "meta/muse-spark-1.3-contributor"
    assert Settings.model_fields["gemini_live_model"].default == "gemini-3.8-live-extended-thinking"


# Settings whose string default is a vendor/model-shaped id. Anything new that
# looks like a model id must be a deliberate, reviewed addition here.
_MODEL_ID_SHAPE = re.compile(r"^[a-z0-9][a-z0-9._-]*/[a-z0-9][a-z0-9._:-]*$")
_REVIEWED_MODEL_ID_DEFAULTS = {
    "brain_model",  # the brain
    "voiceprint_model",  # local speaker verification (speechbrain), a permitted local class
}


def test_no_other_setting_defaults_to_a_vendor_model_id() -> None:
    found = {
        name
        for name, field in Settings.model_fields.items()
        if isinstance(field.default, str) and _MODEL_ID_SHAPE.match(field.default)
    }
    assert found == _REVIEWED_MODEL_ID_DEFAULTS, found ^ _REVIEWED_MODEL_ID_DEFAULTS


def test_retired_settings_are_gone() -> None:
    retired = {
        "mimo_model", "mimo_enabled", "mimo_reasoning_effort", "openrouter_model",
        "deepseek_flash_model", "deepseek_reasoning_effort", "deepseek_api_key",
        "deepseek_base_url", "deepseek_model", "xai_api_key", "xai_base_url",
        "xai_model", "agent_openai_api_key", "agent_openai_base_url",
        "agent_openai_model", "local_model_name", "decider_model",
        "decider_endpoint", "decider_timeout_seconds", "phone_asr_model",
    }
    assert not retired & set(Settings.model_fields)
