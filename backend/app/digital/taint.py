"""External content taint and prompt-injection quarantine.

PERMANENT SECURITY LAW: Gmail, WhatsApp, webpages, documents, files, search
results, and other non-owner sources are ORIGIN=EXTERNAL_CONTENT with
AUTHORITY=DATA. They never become OWNER_INSTRUCTION.
"""

from __future__ import annotations

import re
from typing import Any

from app.digital.types import ContentAuthority, ContentOrigin

_IMPERATIVE_TO_AGENT = re.compile(
    r"(?is)\b("
    r"ignore (?:all |any )?(?:previous |prior |above )?instructions"
    r"|disregard (?:your |all )?(?:system |previous )?prompts?"
    r"|you are now"
    r"|new (?:system )?instructions?:"
    r"|system prompt"
    r"|override (?:your )?(?:policy|safety|instructions)"
    r"|exfiltrat(?:e|ion)"
    r"|upload (?:your |the )?(?:secrets?|keys?|tokens?|credentials?)"
    r"|tell evie to"
    r"|instruct evie to"
    r"|run this command"
    r"|execute this (?:command|code|script)"
    r"|delete all (?:files|memories|data)"
    r"|send (?:me )?(?:your )?(?:api key|password|refresh token)"
    r")\b"
)

_SECRETISH = re.compile(
    r"(?i)\b(sk-[A-Za-z0-9_-]{16,}|ghp_[A-Za-z0-9]{20,}|AKIA[0-9A-Z]{16}|"
    r"(?:api[_-]?key|refresh_token|access_token|password)\s*[:=]\s*\S{8,})\b"
)


def taint_external(
    payload: Any,
    *,
    source: str,
    external_id: str | None = None,
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Wrap provider content so callers cannot forget the authority split."""
    body = payload if isinstance(payload, dict) else {"text": payload}
    text = _collect_text(body)
    instruction = bool(_IMPERATIVE_TO_AGENT.search(text))
    wrapped = {
        "origin": ContentOrigin.EXTERNAL_CONTENT.value,
        "authority": ContentAuthority.DATA.value,
        "source": source,
        "external_id": external_id,
        "potential_external_instruction": instruction,
        "content": body,
    }
    if extra:
        wrapped["provenance"] = extra
    return wrapped


def scan_injection(text: str) -> dict[str, Any]:
    blob = text or ""
    return {
        "origin": ContentOrigin.EXTERNAL_CONTENT.value,
        "authority": ContentAuthority.DATA.value,
        "potential_external_instruction": bool(_IMPERATIVE_TO_AGENT.search(blob)),
        "secret_like": bool(_SECRETISH.search(blob)),
    }


def model_context_layers(
    *,
    owner_request: str,
    policy: str,
    core_state: dict[str, Any] | None = None,
    external: list[dict[str, Any]] | None = None,
    tool_evidence: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Explicit layers. Callers must not concatenate these into one blob."""
    extern = []
    for item in external or []:
        wrapped = dict(item)
        wrapped.setdefault("origin", ContentOrigin.EXTERNAL_CONTENT.value)
        wrapped.setdefault("authority", ContentAuthority.DATA.value)
        if wrapped.get("potential_external_instruction") is None:
            wrapped["potential_external_instruction"] = scan_injection(
                _collect_text(wrapped)
            )["potential_external_instruction"]
        extern.append(wrapped)
    return {
        "OWNER_REQUEST": owner_request,
        "SYSTEM_POLICY": policy,
        "TRUSTED_CORE_STATE": core_state or {},
        "EXTERNAL_CONTENT": extern,
        "TOOL_EVIDENCE": tool_evidence or [],
    }


def assert_not_owner_authority(payload: dict[str, Any]) -> None:
    if payload.get("authority") == ContentAuthority.OWNER_INSTRUCTION.value:
        raise PermissionError("external content cannot be tagged OWNER_INSTRUCTION")
    if payload.get("origin") == ContentOrigin.EXTERNAL_CONTENT.value and payload.get("authority") != ContentAuthority.DATA.value:
        raise PermissionError("EXTERNAL_CONTENT must have AUTHORITY=DATA")


def redacted_for_model(payload: dict[str, Any], *, max_chars: int = 4000) -> dict[str, Any]:
    """Strip credentials and bound size before any model context."""
    content = payload.get("content") if isinstance(payload.get("content"), dict) else payload
    text = _collect_text(content)[:max_chars]
    text = _SECRETISH.sub("[credential redacted]", text)
    return {
        "origin": ContentOrigin.EXTERNAL_CONTENT.value,
        "authority": ContentAuthority.DATA.value,
        "source": payload.get("source"),
        "external_id": payload.get("external_id"),
        "potential_external_instruction": bool(payload.get("potential_external_instruction")),
        "text": text,
    }


def _collect_text(obj: Any) -> str:
    if obj is None:
        return ""
    if isinstance(obj, str):
        return obj
    if isinstance(obj, dict):
        return " ".join(_collect_text(v) for v in obj.values())
    if isinstance(obj, (list, tuple)):
        return " ".join(_collect_text(v) for v in obj)
    return str(obj)
