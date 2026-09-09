"""Semantic capability bus for Muse. Not a second registry — projects existing truth."""

from __future__ import annotations

from typing import Any

from app.contracts import ToolSpec

SEMANTIC_TOOLS: list[dict[str, Any]] = [
    {
        "name": "capability.discover",
        "description": "List currently available semantic capabilities for a domain (files, browser, computer, communications, calendar, code, research, memory, goals). Use when you need a capability you do not already have.",
        "parameters": {
            "type": "object",
            "additionalProperties": False,
            "properties": {"domain": {"type": "string"}},
            "required": [],
        },
        "read_only": True,
        "risk_class": "R0",
    },
    {
        "name": "memory.search",
        "description": (
            "Retrieve owner Memory, including who they actually message "
            "(recency vs volume), last chats, correspondence desk, and stored "
            "summaries. Pass the owner's question as query. Do not invent names "
            "or counts if this returns nothing."
        ),
        "parameters": {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "query": {"type": "string"},
                "k": {"type": "integer", "minimum": 1, "maximum": 12},
            },
            "required": ["query"],
        },
        "read_only": True,
        "risk_class": "R0",
        "permission": "memory:read",
    },
    {
        "name": "life.mail",
        "description": "Read live Apple Mail on this Mac (Envelope Index, Mail.app stays closed). Use for last mail, inbox, who sent it, when it arrived, gist, or readout. Pass the owner's utterance as query, including follow-ups about the last mail (when/who/about) that do not repeat the word mail. Not Gmail web and not stored-archive memory.search.",
        "parameters": {
            "type": "object",
            "additionalProperties": False,
            "properties": {"query": {"type": "string"}},
            "required": ["query"],
        },
        "read_only": True,
        "risk_class": "R0",
        "permission": "mail:read",
    },
    {
        "name": "life.messages",
        "description": "Read live messages on this Mac (iMessage chat.db and WhatsApp Desktop sqlite; apps stay closed). Use for who texted, mixed recents, latest chats, last talk with a named person. Pass the owner's utterance as query. Not WhatsApp Web.",
        "parameters": {
            "type": "object",
            "additionalProperties": False,
            "properties": {"query": {"type": "string"}},
            "required": ["query"],
        },
        "read_only": True,
        "risk_class": "R0",
        "permission": "messages:read",
    },
    {
        "name": "life.send",
        "description": (
            "Send on this Mac: iMessage/SMS (channel messages), WhatsApp, or mail. "
            "Requires recipient and body — never invent either. WhatsApp opens "
            "compose; iMessage and mail can deliver. Speak the tool's spoken result; "
            "never say Not Connected."
        ),
        "parameters": {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "to": {"type": "string"},
                "text": {"type": "string"},
                "channel": {
                    "type": "string",
                    "enum": ["messages", "whatsapp", "mail"],
                },
            },
            "required": ["to", "text"],
        },
        "read_only": False,
        "risk_class": "R2",
        "permission": "messaging:send",
        "sensitive": True,
    },
    {
        "name": "people.lookup",
        "description": "Resolve a person or relationship from Core/Memory.",
        "parameters": {
            "type": "object",
            "additionalProperties": False,
            "properties": {"query": {"type": "string"}},
            "required": ["query"],
        },
        "read_only": True,
        "risk_class": "R0",
    },
    {
        "name": "goal.status",
        "description": "Read the focused GoalContract / CognitiveSession.",
        "parameters": {"type": "object", "additionalProperties": False, "properties": {}},
        "read_only": True,
        "risk_class": "R0",
    },
    {
        "name": "goal.ensure",
        "description": "Create or attach a durable GoalContract when work must survive a provider disconnect (multi-step, background, wait, coding, research-action). Do not use for hello/small talk, and do not use for a single create/save of one list, note, or file.",
        "parameters": {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "objective": {"type": "string"},
                "constraints": {"type": "object"},
                "prepare_only": {"type": "boolean"},
            },
            "required": ["objective"],
        },
        "read_only": False,
        "risk_class": "R1",
    },
    {
        "name": "goal.patch",
        "description": "Patch the active GoalContract: constraints, prepare_only, cancel remaining mutations. Increments steering_version. Same goal — do not create a second one.",
        "parameters": {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "constraints": {"type": "object"},
                "prepare_only": {"type": "boolean"},
                "objective": {"type": "string"},
            },
            "required": [],
        },
        "read_only": False,
        "risk_class": "R1",
    },
    {
        "name": "goal.cancel",
        "description": "Cancel the focused durable goal and stop remaining work.",
        "parameters": {"type": "object", "additionalProperties": False, "properties": {}},
        "read_only": False,
        "risk_class": "R1",
    },
    {
        "name": "digital.discover",
        "description": "Current Digital Operations capability graph (Gmail, WhatsApp, Calendar, Contacts, browser) without credentials.",
        "parameters": {
            "type": "object",
            "additionalProperties": False,
            "properties": {"service": {"type": "string"}},
            "required": [],
        },
        "read_only": True,
        "risk_class": "R0",
    },
    {
        "name": "digital.act",
        "description": "Perform a semantic Digital Operations effect (gmail.search, gmail.read, whatsapp.read, calendar.list, contacts.resolve, …). Never pass tokens, cookies, passwords, or CSS selectors. Prefer background/API over foreground UI.",
        "parameters": {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "service": {
                    "type": "string",
                    "enum": ["gmail", "contacts", "calendar", "whatsapp", "browser", "files", "phone"],
                },
                "operation": {"type": "string"},
                "args": {"type": "object"},
                "confirmed": {"type": "boolean"},
            },
            "required": ["service", "operation"],
        },
        "read_only": False,
        "risk_class": "R2",
        "permission": "mail:act",
    },
    {
        "name": "research.search",
        "description": "Web research. Returns evidence, not a finished owner-history claim.",
        "parameters": {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "query": {"type": "string"},
                "limit": {"type": "integer", "minimum": 1, "maximum": 8},
            },
            "required": ["query"],
        },
        "read_only": True,
        "risk_class": "R1",
    },
    {
        "name": "files.act",
        "description": "Desired filesystem effect (search/read/write/organize) as a goal sentence. Executor chooses the least disruptive tactic. Finder windows should stay 0. One owner request for one file: write complete contents once. If this turn already wrote that file, reuse the same path (overwrite or skip). Do not create sibling files to verify, rename, or rephrase the artifact.",
        "parameters": {
            "type": "object",
            "additionalProperties": False,
            "properties": {"effect": {"type": "string"}},
            "required": ["effect"],
        },
        "read_only": False,
        "risk_class": "R2",
    },
    {
        "name": "code.act",
        "description": "Inspect, patch, or test in an allowed project. Background only — do not open an editor. Describe the desired effect, not click paths.",
        "parameters": {
            "type": "object",
            "additionalProperties": False,
            "properties": {"effect": {"type": "string"}},
            "required": ["effect"],
        },
        "read_only": False,
        "risk_class": "R2",
    },
    {
        "name": "computer.observe",
        "description": "Observe Mac/app/UI state without mutating. Prefer semantic adapters over AX.",
        "parameters": {
            "type": "object",
            "additionalProperties": False,
            "properties": {"goal": {"type": "string"}},
            "required": ["goal"],
        },
        "read_only": True,
        "risk_class": "R1",
    },
    {
        "name": "computer.perform_effect",
        "description": "Request a desired Mac effect (not a click script). Executor picks API/adapter/AX. Set background_preferred true. If only a visible UI can work, the executor returns FOREGROUND_REQUIRED instead of stealing focus.",
        "parameters": {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "effect": {"type": "string"},
                "background_preferred": {"type": "boolean"},
            },
            "required": ["effect"],
        },
        "read_only": False,
        "risk_class": "R2",
    },
    {
        "name": "look.capture",
        "description": (
            "See what is in front of the owner right now. Call this BEFORE speaking "
            "whenever they ask you to look, see, watch, describe, identify, or tell "
            "them about something they are holding, showing, or pointing at, or to "
            "memorize/keep/remember this visible thing. Pass their words as prompt. "
            "Use the camera (screen=false) unless they said screen or screenshot. "
            "Do not say you cannot look. Do not answer from guesswork."
        ),
        "parameters": {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "prompt": {"type": "string"},
                "screen": {"type": "boolean"},
            },
            "required": [],
        },
        "read_only": True,
        "risk_class": "R1",
    },
]


def tool_specs() -> list[ToolSpec]:
    out: list[ToolSpec] = []
    for raw in SEMANTIC_TOOLS:
        out.append(
            ToolSpec(
                name=str(raw["name"]),
                description=str(raw.get("description") or ""),
                parameters=dict(raw.get("parameters") or {}),
                sensitive=bool(raw.get("sensitive")),
                read_only=bool(raw.get("read_only", True)),
                permission=str(raw.get("permission") or "memory:read"),
                risk_class=str(raw.get("risk_class") or "R0"),
            )
        )
    return out


def public_descriptors(*, domain: str | None = None) -> list[dict[str, Any]]:
    """Capability Graph subset for Muse — no credentials, no selectors."""

    needle = (domain or "").strip().lower()
    rows: list[dict[str, Any]] = []
    for spec in SEMANTIC_TOOLS:
        name = str(spec["name"])
        if needle and needle not in name and needle not in str(spec.get("description") or "").lower():
            continue
        rows.append(
            {
                "name": name,
                "description": spec.get("description"),
                "risk": spec.get("risk_class"),
                "read_only": spec.get("read_only"),
                "background_capable": name
                not in {"computer.perform_effect", "look.capture"},
                "foreground_required": False,
                "focus_risk": "low" if spec.get("read_only") else "medium",
                "window_activation": 0 if spec.get("read_only") else None,
                "supports_verification": True,
            }
        )
    try:
        from app.digital.fabric import all_descriptors

        for item in all_descriptors():
            public = item.as_public()
            if needle and needle not in str(public.get("service") or "") and needle not in str(
                public.get("operation") or ""
            ):
                continue
            rows.append(
                {
                    "name": f"{public.get('service')}.{public.get('operation')}",
                    "availability": public.get("availability"),
                    "risk": public.get("risk"),
                    "background_capable": bool(public.get("supports_background")),
                    "foreground_required": bool(public.get("requires_foreground")),
                    "focus_risk": "high" if public.get("requires_foreground") else "low",
                    "window_activation": 1 if public.get("requires_foreground") else 0,
                    "supports_verification": bool(public.get("verification_method")),
                    "family": "digital",
                }
            )
    except Exception:
        pass
    return rows[:80]
