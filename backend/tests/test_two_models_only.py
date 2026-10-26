"""The workspace runs exactly two models: the Muse Spark brain (OpenRouter) and
Gemini Live (Google AI Studio).

This is the guard that keeps a retired model from drifting back in through a
copy-pasted provider, a default string, a prompt, a test fixture or a client.
It scans every tracked file outside the history and artifact folders below.
Every remaining mention is listed with the reason it is allowed; anything else
fails the test and must either be removed or be added here with a reason a
reviewer can disagree with.
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

from app.config import Settings

REPO = Path(__file__).resolve().parents[2]
THIS_FILE = "backend/tests/test_two_models_only.py"
# History, research and generated artifacts: not code that can call a model.
SKIP_PREFIXES = (
    "docs/", "pitch-deck/", "tmp/", "voice-sample/", ".opencode/",
    "communication.md", "backend/uv.lock", "backend/eval/contract_v1.json",
    "macos/Resources/orb/",
)
SCAN_SUFFIXES = {".py", ".js", ".sh", ".swift", ".md", ".mdc", ".toml", ".txt", ".example", ".json", ".plist", ".yaml", ".yml"}

# `mimo_kernel` is the retained wire name of the single-brain topology (the
# shipped PWA reads `cognitive.mimo_kernel`) and is removed before matching.
FORBIDDEN = re.compile(
    r"(?i)(?<![a-z])mimo|xiaomi|deepseek|\bx\.ai\b|(?<![a-z0-9])xai(?![a-z0-9])"
    r"|typesafe|(?<![a-z0-9])jev(?![a-z0-9])|gpt-\d|gpt-realtime|grok"
    r"|whisper-1|text-embedding-3|qwen|llama|ollama|(?<![e])mlx|local_llm"
    r"|muse.?voice|voice.?transcribe|perplexity"
    r"|gemini-(?:1|2)\.|gemini-3\.[0-7]\b|gemini-3-"
)
RETAINED_WIRE_NAMES = ("MIMO_KERNEL", "mimo_kernel")

_OCR = "self-hosted OCR provider: OCR is a permitted local-model class (AGENTS.md)"
_IMPORT = "parses another assistant's chat export (data import, not a model call)"

ALLOWED: dict[str, tuple[frozenset[str], str]] = {
    "backend/app/config.py": (frozenset({"deepseek"}), "vision_provider option name for the OCR provider"),
    ".env.example": (frozenset({"deepseek"}), "OCR provider settings"),
    "backend/app/scripts/chat_history_import.py": (frozenset({"grok"}), _IMPORT),
    "backend/tests/test_chat_history_import.py": (frozenset({"grok"}), _IMPORT),
    "backend/tests/test_vision_ocr.py": (frozenset({"deepseek"}), _OCR),
    "backend/tests/test_model_registry.py": (frozenset({"local_llm"}), "asserts no local_llm task is registered"),
    "backend/clients/pwa/presence.js": (frozenset({"mlx"}), "substring of an embedded base64 image"),
}
ALLOWED_PREFIXES: dict[str, tuple[frozenset[str], str]] = {
    "backend/app/vision/": (frozenset({"deepseek"}), _OCR),
}


def _tracked_files() -> list[Path]:
    out = subprocess.run(
        ["git", "ls-files", "-z"], cwd=REPO, capture_output=True, check=True
    ).stdout.decode()
    files: list[Path] = []
    for rel in filter(None, out.split("\0")):
        if rel == THIS_FILE or rel.startswith(SKIP_PREFIXES):
            continue
        path = REPO / rel
        if (path.suffix in SCAN_SUFFIXES or path.name.startswith(".env")) and path.is_file():
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


def test_no_retired_model_names_anywhere_outside_history() -> None:
    offenders: dict[str, list[str]] = {}
    for path in _tracked_files():
        rel = path.relative_to(REPO).as_posix()
        extra = _tokens(path) - set(_allowed_for(rel))
        if extra:
            offenders[rel] = sorted(extra)
    assert not offenders, (
        "retired model names found; remove them, or add the "
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
        found = set().union(*(_tokens(p) for p in _tracked_files()
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
