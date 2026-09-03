"""Computer-control strategy: adapters, routing, budgets, envelopes, schema truth.

The Realtime model still plans. This module is the server-side discipline so
Evie does not rediscover Music via 24 Accessibility clicks, treat ok=true as
goal completion, or ship a stale provider schema as current.
"""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from typing import Any
from urllib.parse import quote_plus

# Supervised-era primitives. Shadow advertises UI verbs instead of
# inspect_ui/ui_action/screen_look/app_action; schema match is against
# whatever computer-family tools were actually advertised.
REQUIRED_COMPUTER_TOOLS = (
    "computer_status",
    "list_apps",
    "open_app",
    "activate_app",
    "close_app",
    "inspect_ui",
    "ui_action",
    "screen_look",
    "app_action",
)
COMPUTER_SCHEMA_TOOLS = frozenset(REQUIRED_COMPUTER_TOOLS) | frozenset(
    {
        "open_url",
        "read",
        "see",
        "click",
        "double_click",
        "right_click",
        "type",
        "paste",
        "key",
        "scroll",
        "drag",
        "computer",
    }
)

REQUIRED_SCHEMA_PROPERTIES = {
    "inspect_ui": ("app", "name", "query"),
    "app_action": ("app", "action", "playlist", "index"),
    "ui_action": ("action", "element_ref"),
    "screen_look": ("app",),
    "open_app": ("name",),
    "read": ("query",),
    "see": ("target",),
    "click": ("ref",),
}

STRATEGY_ORDER = (
    "semantic_adapter",
    "native_api",
    "apple_events",
    "accessibility",
    "keyboard",
    "screen_vision",
    "coordinate",
)

BUDGET_CAPS = {
    "semantic": 4,
    "ax": 8,
    "keyboard": 6,
    "vision": 6,
    "coordinate": 3,
    "recovery": 3,
    "global": 24,
}

NON_PROGRESS_SWITCH_AFTER = 2

MILESTONES = (
    "NEW",
    "APP_RESOLVED",
    "APP_OPEN",
    "CONTROL_METHOD_SELECTED",
    "TARGET_FOUND",
    "ACTION_EXECUTED",
    "STATE_CHANGED",
    "VERIFICATION_OBTAINED",
    "GOAL_COMPLETE",
)

APP_ADAPTERS: dict[str, dict[str, Any]] = {
    "Music": {
        "bundle_ids": ("com.apple.Music",),
        "preferred": "semantic_adapter",
        "semantic_adapter": "music",
        "supported_actions": (
            "play",
            "play_playlist_track",
            "pause",
            "next",
            "previous",
            "status",
            "find_playlist",
            "list_tracks",
            "list_playlists",
        ),
        "verification": "semantic_player_state",
        "fallbacks": ("accessibility", "screen_vision", "coordinate"),
    },
    "Safari": {
        "bundle_ids": ("com.apple.Safari",),
        "preferred": "semantic_adapter",
        "semantic_adapter": "safari",
        "supported_actions": (
            "search",
            "navigate",
            "play",
            "status",
            "open_item",
            "new_tab",
            "close_tab",
            "next_tab",
            "previous_tab",
        ),
        "verification": "player_or_url",
        "fallbacks": ("accessibility", "keyboard", "screen_vision", "coordinate"),
    },
    "Notes": {
        "bundle_ids": ("com.apple.Notes",),
        "preferred": "semantic_adapter",
        "semantic_adapter": "notes",
        "supported_actions": ("create", "append", "read", "status"),
        "verification": "note_body",
        "fallbacks": ("accessibility", "keyboard", "screen_vision"),
    },
    "Finder": {
        "bundle_ids": ("com.apple.finder",),
        "preferred": "semantic_adapter",
        "semantic_adapter": "finder",
        "supported_actions": ("play", "open_item", "open_folder", "status"),
        "verification": "player_or_selection",
        "fallbacks": ("accessibility", "keyboard", "screen_vision"),
    },
    "Spotify": {
        "bundle_ids": ("com.spotify.client",),
        "preferred": "semantic_adapter",
        "semantic_adapter": "spotify",
        "supported_actions": (
            "play",
            "pause",
            "next",
            "previous",
            "status",
            "search",
            "open_item",
        ),
        "verification": "now_playing",
        "fallbacks": ("accessibility", "keyboard", "screen_vision", "coordinate"),
    },
    "Chrome": {
        "bundle_ids": ("com.google.Chrome",),
        "preferred": "semantic_adapter",
        "semantic_adapter": "chrome",
        "supported_actions": (
            "search",
            "navigate",
            "play",
            "status",
            "open_item",
            "new_tab",
            "close_tab",
            "next_tab",
            "previous_tab",
        ),
        "verification": "player_or_url",
        "fallbacks": ("accessibility", "keyboard", "screen_vision", "coordinate"),
    },
}

PLAY_ACTIONS = frozenset(
    {"play", "play_track", "play_playlist", "play_playlist_track"}
)
APP_ACTION_ALIASES = {
    "play_playlist_track": "play",
    "play_track": "play",
    "play_playlist": "play",
    "now_playing": "status",
    "current": "status",
    "open_first_result": "navigate",
    "open_url": "navigate",
    "open_location": "navigate",
    "click": "open_item",
    "press": "open_item",
    "open_newest": "open_item",
    "create_note": "create",
    "append_note": "append",
    "read_note": "read",
    "open_tab": "new_tab",
    "make_tab": "new_tab",
    "another_tab": "new_tab",
    "shut_tab": "close_tab",
    "next_tab_item": "next_tab",
    "prev_tab": "previous_tab",
    "last_tab": "previous_tab",
}

_SUCCESS_CLAIM_RE = re.compile(
    r"\b("
    r"it'?s playing|i(?:'m| am) playing|now playing|"
    r"i (?:opened|sent|changed|created|deleted|clicked|typed|played)|"
    r"it'?s (?:open|on|done|ready)|i turned (?:it )?on"
    r")\b",
    re.I,
)
_BUDGET_SPEECH_RE = re.compile(r"\b(action budget|out of budget|step budget)\b", re.I)
_COMPUTER_TASK_RE = re.compile(
    r"\b("
    r"music|safari|notes?|finder|calculator|downloads|playlist|"
    r"spotify|chrome|google chrome|"
    r"inspect(?: the)? ui|screen look|click|type into|scroll|"
    r"using calculator|open (?:the )?(?:app|application)|"
    r"(?:open|close|quit|launch|switch to)\s+[A-Za-z][\w .+-]{1,40}|"
    r"first (?:result|link)|this (?:page|view)|new tab|close tab|"
    r"which app|front(?:most)? app|"
    r"(?:the |my |this )?(?:screen|desktop|display|monitor)|"
    r"(?:the |this )(?:front |active )window|"
    r"app window|"
    r"(?:read|write|edit|create)\s+(?:a\s+)?(?:local\s+)?files?|"
    r"files? on (?:my )?(?:the )?(?:desktop|documents|downloads)"
    r")\b",
    re.I,
)
_NAVIGATION_URL_RE = re.compile(
    r"^(?:https?://)?(?:www\.)?"
    r"(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,24}"
    r"(?::\d{2,5})?(?:/[^\s]*)?$",
    re.I,
)
_CAMERA_SCENE_RE = re.compile(
    r"\b("
    r"camera|selfie|my face|holding|the room|this room|"
    r"in front of (?:the )?camera|out(?:side)? (?:of )?(?:the |my )?window|"
    r"through the window|look outside"
    r")\b",
    re.I,
)
_SCREEN_SCENE_RE = re.compile(
    r"\b("
    r"(?:the |my |this )?(?:screen|desktop|display|monitor)|"
    r"(?:the |this )(?:front |active )window|"
    r"active window|front(?:most)? (?:app|window)|app window|"
    r"which app|what app|which application|"
    r"currently open (?:app|application)|"
    r"what(?:'s| is) (?:currently )?(?:open|on (?:my |the )?(?:screen|desktop|display|computer|mac))|"
    r"look at (?:the |my |this )?(?:screen|window|desktop|display|monitor)|"
    r"take a (?:look|photo|picture) (?:at|of) (?:the |my |this )?(?:screen|window|desktop|display|monitor)"
    r")\b",
    re.I,
)
_APP_NAME_RE = re.compile(
    r"\b(music|safari|notes?|finder|calculator|textedit|mail|messages|"
    r"calendar|spotify|chrome)\b",
    re.I,
)


def normalize_app_action(action: str | None) -> str:
    raw = str(action or "").strip().lower()
    return APP_ACTION_ALIASES.get(raw, raw)


def adapter_for(app: str | None, bundle_id: str | None = None) -> dict[str, Any] | None:
    needle = str(app or "").strip()
    bundle = str(bundle_id or "").strip().lower()
    if needle:
        for name, spec in APP_ADAPTERS.items():
            if needle.lower() == name.lower() or needle.lower() in name.lower():
                return {"app": name, **spec}
    if bundle:
        for name, spec in APP_ADAPTERS.items():
            if bundle in {item.lower() for item in spec["bundle_ids"]}:
                return {"app": name, **spec}
    return None


def control_for_app(app: str | None, bundle_id: str | None = None) -> dict[str, Any]:
    spec = adapter_for(app, bundle_id)
    if spec is None:
        return {
            "preferred": "accessibility",
            "semantic_adapter": None,
            "supported_actions": [],
            "fallbacks": ["accessibility", "keyboard", "screen_vision", "coordinate"],
            "verification": "inspect_ui_or_screen",
        }
    return {
        "preferred": spec["preferred"],
        "semantic_adapter": spec["semantic_adapter"],
        "supported_actions": list(spec["supported_actions"]),
        "fallbacks": list(spec["fallbacks"]),
        "verification": spec["verification"],
    }


_COMMAND_LABELS = frozenset(
    {
        "open",
        "in",
        "on",
        "for",
        "and",
        "then",
        "new",
        "tab",
        "the",
        "a",
        "to",
        "with",
        "using",
        "please",
        "safari",
        "chrome",
        "browser",
        "search",
        "play",
        "watch",
        "click",
        "first",
        "result",
        "nothing",
        "else",
        "just",
        "only",
        "app",
        "window",
        "here",
        "there",
        "now",
        "go",
        "visit",
        "launch",
    }
)


def _collapse_spoken_domain(text: str) -> str:
    """Turn spoken 'youtube . com' into 'youtube.com' without gluing the next word.

    Only spaced dots are domain separators. A sentence period in 'youtube.com. Open'
    must not become the fake host youtube.com.open.
    """
    return re.sub(
        r"(?<=[A-Za-z0-9])\s+\.\s+(?=[A-Za-z0-9])",
        ".",
        (text or "").strip(),
    )


def _strip_glued_command_labels(text: str) -> str:
    """Drop leftover command words glued on as extra labels (youtube.com.open)."""
    raw = (text or "").strip()
    if not raw:
        return ""
    prefix = ""
    rest = raw
    lower = rest.lower()
    for scheme in ("https://", "http://"):
        if lower.startswith(scheme):
            prefix = rest[: len(scheme)]
            rest = rest[len(scheme) :]
            break
    host, sep, path = rest.partition("/")
    labels = [part for part in host.split(".") if part]
    while len(labels) >= 3 and labels[-1].lower().rstrip(".,);!?") in _COMMAND_LABELS:
        labels.pop()
    host = ".".join(labels)
    if not host:
        return raw
    return prefix + host + (sep + path if sep else "")


def clean_computer_query(text: str, app: str | None = None) -> str:
    """Owner search/open text without leftover spoken clauses or glued hosts."""
    query = (text or "").strip().strip(" \t\"'`“”'")
    if not query:
        return ""
    query = re.sub(r"\s+", " ", query)
    query = re.sub(r"\s+continue=true\s*$", "", query, flags=re.I)
    query = re.sub(
        r"\s+(?:nothing else|that's it|thats it|only that|please|thanks|for me)\s*$",
        "",
        query,
        flags=re.I,
    )
    query = re.sub(
        r"\s+in(?:\s+a|\s+the)?\s+new\s+tab\s*$",
        "",
        query,
        flags=re.I,
    )
    query = re.sub(
        r"\s+(?:in|on|using|with)\s+(?:the\s+)?(?:google\s+)?"
        r"(?:chrome|safari|browser|spotify|notes|finder)\b.*$",
        "",
        query,
        flags=re.I,
    )
    query = re.sub(
        r"\s*(?:,|and|,?\s*then)\s+(?:open|click|press|play|go to|visit|close|quit)\b.*$",
        "",
        query,
        flags=re.I,
    )
    if app:
        query = re.sub(
            rf"\s+(?:in|on|using|with)\s+{re.escape(app)}\s*$",
            "",
            query,
            flags=re.I,
        )
    query = query.strip(" \"'")
    collapsed = _collapse_spoken_domain(query)
    if " " not in collapsed and "." in collapsed:
        collapsed = _strip_glued_command_labels(collapsed)
        if collapsed:
            query = collapsed
    return query.strip(" \"'")[:200]


def navigation_url_from_text(text: str) -> str | None:
    """Return an https URL when the text is a destination, not a search phrase."""
    raw = clean_computer_query(text or "")
    raw = raw.strip().strip(" \t\"'`“”'")
    raw = raw.rstrip(".,);!?")
    if not raw or " " in raw or "@" in raw:
        return None
    if raw.lower() in {"localhost", "about:blank"}:
        return None
    if not _NAVIGATION_URL_RE.fullmatch(raw):
        return None
    suffix = Path(raw.split("://", 1)[-1]).suffix.lower()
    if suffix in {
        ".txt",
        ".md",
        ".html",
        ".htm",
        ".csv",
        ".json",
        ".pdf",
        ".png",
        ".jpg",
        ".jpeg",
        ".gif",
        ".webp",
        ".py",
        ".swift",
        ".js",
        ".ts",
        ".css",
        ".log",
        ".sh",
    }:
        return None
    if raw.lower().startswith("http://") or raw.lower().startswith("https://"):
        return raw
    return f"https://{raw}"


def _search_query_from_goal(text: str) -> str:
    """Extract only the owner's search text, without leftover spoken clauses."""
    raw = (text or "").strip()
    quoted = re.search(r"[\"“](.+?)[\"”]", raw)
    if quoted is None:
        quoted = re.search(
            r"'((?:https?://)?(?:www\.)?[a-z0-9.-]+\.[a-z]{2,24}(?:/[^\s']*)?)'",
            raw,
            re.I,
        )
    if quoted is None:
        quoted = re.search(r"'([A-Za-z0-9][^']{0,80}?)'", raw)
    if quoted:
        candidate = quoted.group(1).strip()
        if candidate:
            return clean_computer_query(candidate)[:200]
    match = re.search(r"\bsearch\s+for\s+(.+)$", raw, re.I)
    if match is None:
        match = re.search(
            r"\bsearch\s+(?:in|on|using|with)\s+(?:the\s+)?(?:google\s+)?"
            r"(?:chrome|safari|browser)\s+for\s+(.+)$",
            raw,
            re.I,
        )
    if match is None:
        match = re.search(r"\b(?:look\s+up|find)\s+(.+)$", raw, re.I)
    if match is None:
        match = re.search(r"\bgoogle\s+(?!chrome\b)(.+)$", raw, re.I)
    if match is None:
        match = re.search(
            r"\bsearch\s+(?!in\b|on\b|using\b|with\b)(.+)$",
            raw,
            re.I,
        )
    if match is None:
        return ""
    query = match.group(1).strip().rstrip(".")
    query = re.sub(
        r"^(?:in|on|using|with)\s+(?:google\s+)?(?:chrome|safari)\s+for\s+",
        "",
        query,
        flags=re.I,
    )
    return clean_computer_query(query)


_FIRST_RESULT_RE = re.compile(
    r"first(?:\s+search)?\s+result(?:\s+link)?|"
    r"top(?:\s+search)?\s+result|"
    r"open the first(?:\s+(?:result|link|video|item|one))?|"
    r"click the (?:first|top)|"
    r"play(?:\s+the)?\s+first|"
    r"first\s+(?:video|item|one|hit|track|song|episode)|"
    r"the first (?:one|item|video|link) that appears|"
    r"result link",
    re.I,
)

_FIRST_ON_PAGE_RE = re.compile(
    r"first\s+video|"
    r"play(?:\s+the)?\s+first|"
    r"first\s+(?:one|item|hit|track|song|episode)(?:\s+that\s+appears)?|"
    r"open(?:\s+the)?\s+first\s+(?:video|item|one|hit)|"
    r"click(?:\s+the)?\s+first\s+(?:video|item|one|hit)|"
    r"the first (?:one|item|video|link) that appears",
    re.I,
)

_WEB_RESEARCH_RE = re.compile(
    r"search(?:\s+it|\s+this|\s+that)?(?:\s+on)?(?:\s+the)?\s+web|"
    r"search the web|"
    r"look(?:\s+it|\s+this|\s+that)?\s+up|"
    r"google(?:\s+it|\s+this|\s+that)\b|"
    r"on the web|"
    r"give me (?:info|information|details)|"
    r"what (?:this|that)(?:\s+\w+){0,8}\s+(?:is )?about|"
    r"tell me what (?:this|that)",
    re.I,
)


def wants_first_result_text(text: str) -> bool:
    """True when the owner asked to leave the SERP or open the first hit."""
    lower = (text or "").lower()
    if not lower:
        return False
    if re.search(
        r"don't open|do not open|just tell|don't click|do not click|"
        r"don't play|do not play",
        lower,
    ):
        return False
    return bool(_FIRST_RESULT_RE.search(lower) or _FIRST_ON_PAGE_RE.search(lower))


def wants_first_on_page_item(text: str) -> bool:
    """True when they asked to open/play the first on-screen item, not just a SERP hit."""
    lower = (text or "").lower()
    if not lower:
        return False
    if re.search(
        r"don't open|do not open|just tell|don't click|do not click|"
        r"don't play|do not play",
        lower,
    ):
        return False
    return bool(_FIRST_ON_PAGE_RE.search(lower))


def looks_like_web_research(text: str) -> bool:
    """Spoken web lookup / 'what is this about' — not a Mac browser-control goal."""
    raw = (text or "").strip()
    if not raw:
        return False
    lower = raw.lower()
    if re.search(r"\b(?:safari|chrome|new tab|close tab)\b", lower):
        return False
    if wants_first_on_page_item(raw) or wants_first_result_text(raw) or wants_play_media(raw):
        return False
    query = _search_query_from_goal(raw)
    if query and navigation_url_from_text(query):
        return False
    if wants_first_result_text(raw) and not _WEB_RESEARCH_RE.search(lower):
        return False
    return bool(_WEB_RESEARCH_RE.search(lower))


def web_search_query_from_text(text: str) -> str:
    """Concrete lookup string for search_web, from the owner's words."""
    raw = (text or "").strip()
    if not raw:
        return ""
    stripped = re.sub(
        r"^(?:please\s+)?(?:can you\s+)?(?:would you\s+)?"
        r"(?:search(?:\s+it|\s+this|\s+that)?(?:\s+on)?(?:\s+the)?\s+web(?:\s+for)?|"
        r"look(?:\s+it|\s+this|\s+that)?\s+up(?:\s+for)?|google)\s*",
        "",
        raw,
        flags=re.I,
    )
    stripped = re.sub(
        r"\s*(?:and\s+)?(?:give me|tell me)\s+(?:info|information|details|what).*$",
        "",
        stripped,
        flags=re.I,
    )
    stripped = re.sub(r"\s+on the web\s*$", "", stripped, flags=re.I)
    stripped = stripped.strip(" ?.,!")
    if not stripped or stripped.lower() in {
        "it",
        "this",
        "that",
        "this book",
        "that book",
        "this one",
        "that one",
    }:
        return raw[:200]
    return stripped[:200]


def looks_like_opened_content_item(url: str) -> bool:
    """True when a URL is already a specific item, not a site home or feed."""
    raw = (url or "").strip()
    if not raw:
        return False
    lower = raw.lower()
    if any(
        marker in lower
        for marker in (
            "google.com/search",
            "bing.com/search",
            "duckduckgo.com/?q",
            "search.yahoo.com",
        )
    ):
        return False
    return bool(
        re.search(
            r"/watch(\?|/|$)|[?&]v=|/status/\d|/shorts/|/reel[s]?/|"
            r"/videos?/[\w-]+|/dp/[a-z0-9]+|/p/\d+|"
            r"vimeo\.com/\d+|dailymotion\.com/video/",
            lower,
        )
    )


_INLINE_URL_RE = re.compile(
    r"(https?://[^\s,;\"'<>]+)|"
    r"(?:www\.)?(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,24}"
    r"(?:/[^\s,;\"'<>]*)?",
    re.I,
)
_PLAY_MEDIA_RE = re.compile(
    r"\b(?:play|watch|start)\b.{0,120}\b(?:video|clip|movie|film|episode)\b|"
    r"\b(?:video|clip|movie|film)\b.{0,80}\b(?:play|watch)\b|"
    r"(?:click|open|press)\s+(?:the\s+)?first\s+video|"
    r"play(?:\s+the)?\s+first\s+video|"
    r"\bwatch\s+(?:the\s+)?(?:first|this|that)",
    re.I,
)
_LOCAL_MEDIA_RE = re.compile(
    r"\b(?:finder|downloads|desktop|movies folder|documents folder|"
    r"quicktime|local (?:video|file|movie))\b|"
    r"\.(?:mp4|mov|m4v|mkv|avi|webm|mpg|mpeg)\b",
    re.I,
)
_AUDIO_NOT_VIDEO_RE = re.compile(
    r"\b(?:playlist|spotify|\bsong\b|\btrack\b|apple music)\b",
    re.I,
)
_VIDEO_INDEX_HOST_RE = re.compile(
    r"youtube\.com|youtu\.be|vimeo\.com|dailymotion\.com",
    re.I,
)


def navigation_url_in_utterance(text: str) -> str | None:
    """Pick a destination URL even when it is buried in a spoken sentence."""
    raw = _collapse_spoken_domain(text or "")
    whole = navigation_url_from_text(raw)
    if whole:
        return whole
    for match in _INLINE_URL_RE.finditer(raw):
        candidate = match.group(0).rstrip(".,);!?\"'")
        found = navigation_url_from_text(candidate)
        if found:
            return found
    return None


def wants_play_media(text: str) -> bool:
    """True when the owner asked to play/watch a video, not a song or playlist."""
    raw = text or ""
    lower = raw.lower()
    if not lower:
        return False
    if re.search(r"don't play|do not play|don't watch|do not watch", lower):
        return False
    if _AUDIO_NOT_VIDEO_RE.search(lower) and not re.search(
        r"\b(?:video|youtube|vimeo|finder|\.mp4|\.mov)\b",
        lower,
    ):
        return False
    if _PLAY_MEDIA_RE.search(raw):
        return True
    if re.search(r"\b(?:play|watch)\b", lower) and re.search(
        r"youtube|vimeo|dailymotion|finder|\.mp4|\.mov|\.mkv|video",
        lower,
    ):
        return True
    return False


def looks_like_local_media_goal(text: str) -> bool:
    """True when the video lives on disk / Finder, not in a browser tab."""
    raw = text or ""
    if not raw or not _LOCAL_MEDIA_RE.search(raw):
        return False
    lower = raw.lower()
    if re.search(
        r"\b(?:video|clip|movie|film|play|watch|\.mp4|\.mov|\.mkv|\.m4v|\.avi|\.webm)\b",
        lower,
    ):
        return True
    return False


def media_query_from_goal(text: str) -> str:
    """Title or filename to play. Empty means the first/ordinal on-screen item."""
    raw = (text or "").strip()
    if not raw:
        return ""
    quoted = re.search(r"[\"“](.+?)[\"”]", raw)
    if quoted:
        candidate = quoted.group(1).strip()
        if candidate and not navigation_url_from_text(candidate):
            return candidate[:200]
    match = re.search(
        r"\bsearch(?:\s+(?:on|in))?\s+(?:youtube|yt|vimeo|dailymotion)\s+for\s+(.+)$",
        raw,
        re.I,
    )
    if match is None:
        match = re.search(
            r"\b(?:youtube|vimeo|dailymotion)\s+search(?:\s+for)?\s+(.+)$",
            raw,
            re.I,
        )
    if match:
        query = match.group(1)
        query = re.sub(r"\s+(?:and\s+)?(?:then\s+)?(?:play|watch|open)\b.*$", "", query, flags=re.I)
        query = query.strip(" \"'")
        if query and not navigation_url_from_text(query):
            return query[:200]
    match = re.search(
        r"\b(?:play|watch|open)\s+(?:the\s+)?(?:video|clip|movie|file|film)\s+"
        r"(?:named|called|titled)\s+(.+)$",
        raw,
        re.I,
    )
    if match:
        query = match.group(1)
        query = re.sub(
            r"\s+(?:in|from|on|with)\s+(?:finder|safari|chrome|youtube|downloads|desktop).*$",
            "",
            query,
            flags=re.I,
        )
        return query.strip(" \"'")[:200]
    match = re.search(
        r"\b(?:play|watch)\s+(.+?)\s+on\s+(?:youtube|yt|vimeo|dailymotion)\b",
        raw,
        re.I,
    )
    if match:
        query = re.sub(
            r"^(?:the\s+)?(?:video|clip|movie)\s+",
            "",
            match.group(1),
            flags=re.I,
        ).strip(" \"'")
        if query and not re.search(
            r"^(?:the\s+)?(?:first|second|third|1st|2nd|a|this)\b",
            query,
            re.I,
        ):
            return query[:200]
    return ""


def video_search_url(dest: str | None, query: str) -> str:
    """Site search URL for a named video. YouTube is the default web index."""
    encoded = quote_plus((query or "").strip())
    host = (dest or "").lower()
    if "vimeo" in host:
        return f"https://vimeo.com/search?q={encoded}"
    if "dailymotion" in host:
        return f"https://www.dailymotion.com/search/{encoded}"
    return f"https://www.youtube.com/results?search_query={encoded}"


def default_web_video_index_url() -> str:
    return "https://www.youtube.com/"


def _host_looks_like_video_index(url: str | None) -> bool:
    return bool(_VIDEO_INDEX_HOST_RE.search(url or ""))


def resolve_media_computer_goal(
    text: str,
    target_app: str | None = None,
) -> tuple[str, dict[str, Any]] | None:
    """Play a named or ordinal video from a site, a search, or Finder."""
    raw = (text or "").strip()
    if not raw:
        return None
    local = looks_like_local_media_goal(raw)
    play = wants_play_media(raw)
    first_item = wants_first_on_page_item(raw)
    if not local and not play and not first_item:
        return None
    if local:
        query = media_query_from_goal(raw) or "first"
        return "app_action", {"app": "Finder", "action": "play", "query": query}
    if not play and not (
        first_item and _host_looks_like_video_index(navigation_url_in_utterance(raw) or "")
    ):
        return None
    app = _browser_app_from_text(raw, target_app) or "Safari"
    dest = navigation_url_in_utterance(raw)
    title = media_query_from_goal(raw)
    if title and (dest is None or _host_looks_like_video_index(dest)):
        search = video_search_url(dest, title)
        return "app_action", {
            "app": app,
            "action": "navigate",
            "query": search,
            "url": search,
        }
    if dest:
        return "app_action", {
            "app": app,
            "action": "navigate",
            "query": dest,
            "url": dest,
        }
    if title:
        search = video_search_url(None, title)
        return "app_action", {
            "app": app,
            "action": "navigate",
            "query": search,
            "url": search,
        }
    return "app_action", {"app": app, "action": "play", "query": "first"}


def _browser_chrome_is_exclusive(text: str) -> bool:
    """Tab actions must not steal a search / first-result / play goal in the same utterance."""
    raw = (text or "").strip()
    if not raw:
        return False
    if _search_query_from_goal(raw) or wants_first_result_text(raw) or wants_play_media(raw):
        return False
    if re.search(r"\b(?:search\s+for|look\s+up)\b", raw, re.I):
        return False
    if re.search(r"\bgoogle\s+(?!chrome\b)", raw, re.I):
        return False
    return True


def wants_screen_observation(text: str) -> bool:
    """True when the owner wants the Mac window/screen, not the room camera."""
    raw = (text or "").strip()
    if not raw:
        return False
    from app.ev.laptop_files import is_system_confirmation, looks_like_file_task

    if is_system_confirmation(raw) or looks_like_file_task(raw):
        return False
    if wants_play_media(raw) or wants_first_on_page_item(raw):
        return False
    if _CAMERA_SCENE_RE.search(raw) and not re.search(
        r"\b(screen|desktop|display|monitor|which app|what app|front(?:most)? app)\b",
        raw,
        re.I,
    ):
        return False
    return bool(_SCREEN_SCENE_RE.search(raw))


def look_should_use_screen(text: str) -> bool:
    """Route a look/photo call onto screen capture when the prompt is the Mac."""
    return wants_screen_observation(text)


def resolve_screen_observation_goal(
    text: str,
    target_app: str | None = None,
) -> tuple[str, dict[str, Any]] | None:
    """Map 'what's on the screen / which app is open' onto window vision."""
    del target_app
    raw = (text or "").strip()
    if not wants_screen_observation(raw):
        return None
    return "screen_look", {"target": "active_window", "goal": raw}


def _target_app_from_utterance(text: str, target_app: str | None = None) -> str:
    """Best-effort app name from 'open Slack and …' / 'in Messages, …'."""
    named = str(target_app or "").strip()
    if named and named.lower() not in {"front", "this", "that", "it"}:
        return named
    raw = (text or "").strip()
    if not raw:
        return ""
    browser = _browser_app_from_text(raw, None)
    if browser:
        return browser
    opened = re.search(
        r"\b(?:open|launch|start)\s+(?:up\s+)?(?:the\s+)?"
        r"(?P<name>.+?)"
        r"(?:\s+app)?\s*(?:,|\s+and|\s+then)\s+"
        r"(?:search|type|enter|write|click|press|find|look|play|go to|visit|"
        r"open a(?:nother)? tab)\b",
        raw,
        re.I,
    )
    if opened:
        name = opened.group("name").strip(" .")
        name = re.sub(r"^(?:the\s+)?(?:app\s+)", "", name, flags=re.I).strip()
        name = re.sub(r"\s+app$", "", name, flags=re.I).strip()
        if name and not re.search(
            r"\b(new tab|result|link|this|that|it)\b", name, re.I
        ):
            return name
    in_app = re.search(
        r"\b(?:in|inside|on|using|from)\s+(?:the\s+)?(?P<app>[A-Za-z][\w .+-]{0,40}?)"
        r"(?:\s+app)?\s*(?:,|:|and then|\sand\s)",
        raw,
        re.I,
    )
    if in_app:
        candidate = in_app.group("app").strip()
        if candidate.lower() not in {"the", "this", "that", "front", "a", "new"}:
            return candidate
    return ""


def _browser_app_from_text(text: str, target_app: str | None) -> str | None:
    lowered = (text or "").lower()
    app = str(target_app or "").strip().lower()
    if "chrome" in app or re.search(r"\b(?:google\s+)?chrome\b", lowered):
        return "Chrome"
    if "safari" in app or re.search(r"\bsafari\b", lowered):
        return "Safari"
    return None


def _resolve_close_app(text: str) -> tuple[str, dict[str, Any]] | None:
    """Close the named app. Tab closes are chrome actions, not app quits."""
    lowered = (text or "").strip().lower()
    if not lowered:
        return None
    if re.search(r"\b(?:close|shut)\s+(?:this\s+|the\s+|current\s+)?tab\b", lowered):
        return None
    verb = re.search(r"\b(?:close|quit|exit|dismiss|hide)\b", lowered)
    if verb is None:
        return None
    negation = re.search(
        r"\b(?:don't|do not|dont)\s+(?:close|quit|exit|hide|dismiss)\b",
        lowered,
    )
    if negation is not None and negation.start() <= verb.start():
        return None
    rest = lowered[verb.end() :]
    rest = re.sub(r"^(?:\s+(?:the|this|my|all|every|any))*", "", rest)
    rest = re.sub(r"^(?:\s+(?:app|application|windows?|window)\s+(?:of\s+|for\s+)?)", "", rest)
    name = re.split(
        r"\s+(?:by|without|instead|and then|,|\()|\s+for me\b",
        rest,
        maxsplit=1,
    )[0]
    name = re.sub(r"^(?:the\s+)?", "", name).strip()
    name = re.sub(r"\s+(?:app|application|windows?)$", "", name).strip(" .")
    if not name or re.search(r"\b(result|link|tab|dialog|this|that|it)\b", name):
        return None
    if re.search(r"\b(and|then|search|click|type|write|press)\b", name):
        return None
    return "close_app", {"name": name}


def _resolve_app_lifecycle_and_browser_chrome(
    text: str,
    target_app: str | None = None,
) -> tuple[str, dict[str, Any]] | None:
    """Close/quit/open-tab must win over Safari/Chrome status and search."""
    raw = (text or "").strip()
    if not raw:
        return None
    lowered = raw.lower()
    browser = _browser_app_from_text(raw, target_app)
    chrome_app = browser or "front"
    wants_tab = bool(
        re.search(
            r"\b(?:new|another|fresh)\s+tab\b|\bopen a(?:nother)? tab\b|\bopen another tab\b",
            lowered,
        )
    )
    if wants_tab and _browser_chrome_is_exclusive(raw):
        return "app_action", {"app": chrome_app if browser else "front", "action": "new_tab"}
    if re.search(r"\b(?:close|shut)\s+(?:this\s+|the\s+|current\s+)?tab\b", lowered):
        return "app_action", {"app": chrome_app if browser else "front", "action": "close_tab"}
    if re.search(r"\b(?:next|following)\s+tab\b", lowered) and _browser_chrome_is_exclusive(raw):
        return "app_action", {"app": chrome_app if browser else "front", "action": "next_tab"}
    if re.search(r"\b(?:previous|prior|last)\s+tab\b", lowered) and _browser_chrome_is_exclusive(raw):
        return "app_action", {"app": chrome_app if browser else "front", "action": "previous_tab"}
    closed = _resolve_close_app(raw)
    if closed is not None:
        return closed
    media = resolve_media_computer_goal(raw, target_app)
    if media is not None:
        return media
    open_only = re.match(
        r"^(?:please\s+)?(?:open|launch|start)\s+(?:up\s+)?(?:the\s+)?(?:app\s+)?"
        r"(?P<name>.+?)(?:\s+app)?\.?\s*$",
        lowered,
    )
    if open_only:
        name = open_only.group("name").strip(" .")
        name = re.sub(r"^(?:the\s+)?(?:app\s+)", "", name).strip()
        name = re.sub(r"\s+app$", "", name).strip()
        if name and not re.search(
            r"\b(and|then|click|type|search|press|write|first result|tab)\b", name
        ):
            dest_name = navigation_url_from_text(name)
            if dest_name:
                return "app_action", {
                    "app": browser or "Safari",
                    "action": "navigate",
                    "query": dest_name,
                    "url": dest_name,
                }
            return "open_app", {"name": name}
    query = _search_query_from_goal(raw)
    dest = navigation_url_from_text(query)
    if dest is None and re.search(r"\b(go to|open|visit|navigate to)\b", lowered):
        after = re.search(
            r"\b(?:go to|open|visit|navigate to)\s+(?P<dest>\S+)",
            raw,
            re.I,
        )
        if after:
            dest = navigation_url_from_text(after.group("dest"))
    if dest is None:
        dest = navigation_url_in_utterance(raw)
    if dest is not None and (
        browser
        or query
        or re.search(r"\b(go to|visit|navigate to|open)\b", lowered)
    ):
        app_name = browser or "Safari"
        return "app_action", {
            "app": app_name,
            "action": "navigate",
            "query": dest,
            "url": dest,
        }
    webish = bool(
        browser
        or wants_tab
        or re.search(r"\b(safari|chrome|browser)\b", lowered)
        or (
            re.search(r"\bsearch\s+for\b", lowered)
            and not re.search(r"\b(spotify|music|notes|playlist)\b", lowered)
        )
    )
    if query and webish:
        return "app_action", {
            "app": browser or "Safari",
            "action": "search",
            "query": query,
        }
    return None


def resolve_in_app_computer_goal(
    text: str,
    target_app: str | None = None,
) -> tuple[str, dict[str, Any]] | None:
    """Map a plain-language Mac goal onto one semantic adapter call.

    The F4 ``computer`` broker states a goal, not a primitive. Chrome/Safari
    search, Notes write, Spotify/Music playback must not fall through to
    search_web or a disabled executor. Close/quit and browser chrome must not
    collapse into a status read of the named browser.
    """

    raw = (text or "").strip()
    if not raw:
        return None
    lowered = raw.lower()
    app = str(target_app or "").strip().lower()

    lifecycle = _resolve_app_lifecycle_and_browser_chrome(raw, target_app)
    if lifecycle is not None:
        return lifecycle

    def named(*needles: str) -> bool:
        for needle in needles:
            if needle and needle in app:
                return True
            if needle and re.search(rf"\b{re.escape(needle)}\b", lowered):
                return True
        return False

    if named("chrome", "google chrome"):
        media = resolve_media_computer_goal(raw, "Chrome")
        if media is not None:
            return media
        query = _search_query_from_goal(raw)
        dest = navigation_url_from_text(query) or navigation_url_in_utterance(raw)
        if dest:
            return "app_action", {
                "app": "Chrome",
                "action": "navigate",
                "query": dest,
                "url": dest,
            }
        if query:
            return "app_action", {"app": "Chrome", "action": "search", "query": query}
        return "app_action", {"app": "Chrome", "action": "status"}
    if named("safari"):
        media = resolve_media_computer_goal(raw, "Safari")
        if media is not None:
            return media
        query = _search_query_from_goal(raw)
        dest = navigation_url_from_text(query) or navigation_url_in_utterance(raw)
        if dest:
            return "app_action", {
                "app": "Safari",
                "action": "navigate",
                "query": dest,
                "url": dest,
            }
        if query:
            return "app_action", {"app": "Safari", "action": "search", "query": query}
        return "app_action", {"app": "Safari", "action": "status"}
    if named("spotify"):
        query = _search_query_from_goal(raw)
        if any(word in lowered for word in ("search", "find", "look up")):
            if query:
                return "app_action", {"app": "Spotify", "action": "search", "query": query}
        if any(word in lowered for word in ("pause", "paused")):
            return "app_action", {"app": "Spotify", "action": "pause"}
        if query or "play" in lowered:
            return "app_action", {
                "app": "Spotify",
                "action": "play" if "search" not in lowered else "search",
                "query": query,
            }
        return "app_action", {"app": "Spotify", "action": "status"}
    if named("notes", "note"):
        if re.search(r"\b(read|what's in|what is in|show me)\b", lowered) and not re.search(
            r"\b(create|write|jot|make|append|add)\b", lowered
        ):
            return "app_action", {"app": "Notes", "action": "read"}
        body = ""
        match = re.search(
            r"(?:that says|saying|with(?: the)?(?: text)?|reading)\s+(.+)$",
            raw,
            re.I,
        )
        if match:
            body = match.group(1).strip(" \"'")[:500]
        elif re.search(r"\b(?:create|write|jot|make|add)\b", lowered):
            body = re.sub(
                r"^(?:please\s+)?(?:create|write|jot|make|add)\s+(?:a\s+)?(?:new\s+)?note\s*",
                "",
                raw,
                flags=re.I,
            ).strip(" \"'")[:500]
        if body:
            action = "append" if "append" in lowered or "add to" in lowered else "create"
            return "app_action", {"app": "Notes", "action": action, "text": body, "value": body}
        return "app_action", {"app": "Notes", "action": "status"}
    if named("music") and any(word in lowered for word in ("play", "pause", "playlist", "track")):
        args: dict[str, Any] = {"app": "Music", "action": "pause" if "pause" in lowered else "play"}
        playlist = re.search(
            r"(?:the|my)\s+([A-Za-z0-9][\w &'’\-]{0,48}?)\s+playlist",
            raw,
            re.I,
        )
        if playlist is None:
            playlist = re.search(
                r"\bplaylist\s+(?:called|named)?\s*([A-Za-z0-9][\w &'’\-]{1,48})",
                raw,
                re.I,
            )
        if playlist:
            args["playlist"] = playlist.group(1).strip()
        ordinal = re.search(r"\b(first|second|third|1st|2nd|3rd)\b", lowered)
        if ordinal:
            args["index"] = {"first": 1, "1st": 1, "second": 2, "2nd": 2, "third": 3, "3rd": 3}[
                ordinal.group(1)
            ]
        return "app_action", args
    browser = resolve_browser_computer_goal(raw, target_app)
    if browser is not None:
        return browser
    return None


def resolve_browser_computer_goal(
    text: str,
    target_app: str | None = None,
) -> tuple[str, dict[str, Any]] | None:
    """Search or open a site in the browser even when they did not name Safari.

    Domain destinations stay navigate. Spoken research ('search the web and
    tell me') is not a Mac goal — that is search_web.
    """
    raw = (text or "").strip()
    if not raw or looks_like_web_research(raw):
        return None
    query = _search_query_from_goal(raw)
    dest = navigation_url_from_text(query) if query else navigation_url_in_utterance(raw)
    first = wants_first_result_text(raw) or wants_first_on_page_item(raw)
    looking = bool(re.search(r"\b(?:search|google|look\s+up)\b", raw, re.I))
    if not dest and re.search(r"\b(?:music|spotify|playlist|track)\b", raw, re.I):
        return None
    if wants_play_media(raw) or looks_like_local_media_goal(raw):
        return resolve_media_computer_goal(raw, target_app)
    if not dest and not query:
        return None
    if not dest and not looking and not first:
        return None
    app = _browser_app_from_text(raw, target_app) or "Safari"
    if dest:
        return "app_action", {
            "app": app,
            "action": "navigate",
            "query": dest,
            "url": dest,
        }
    return "app_action", {"app": app, "action": "search", "query": query}


def resolve_generic_computer_goal(
    text: str,
    target_app: str | None = None,
) -> tuple[str, dict[str, Any]] | None:
    """Open, close, or operate any installed app when no semantic adapter applies."""

    raw = (text or "").strip()
    if not raw:
        return None
    lowered = raw.lower()
    app = _target_app_from_utterance(raw, target_app)

    closed = _resolve_close_app(raw)
    if closed is not None:
        return closed

    open_only = re.match(
        r"^(?:please\s+)?(?:open|launch|start)\s+(?:up\s+)?(?:the\s+)?(?:app\s+)?"
        r"(?P<name>.+?)(?:\s+app)?\.?\s*$",
        lowered,
    )
    if open_only:
        name = open_only.group("name").strip(" .")
        name = re.sub(r"^(?:the\s+)?(?:app\s+)", "", name).strip()
        name = re.sub(r"\s+app$", "", name).strip()
        if name and not re.search(
            r"\b(and|then|click|type|search|press|write|first result)\b", name
        ):
            return "open_app", {"name": name}

    click = re.search(
        r"\b(?:click|press|tap|hit)\s+(?:on\s+)?(?:the\s+)?(?P<q>.+)$",
        raw,
        re.I,
    )
    if click:
        query = clean_computer_query(click.group("q").strip(" ."), app=app)
        query = re.sub(
            r"\s+(?:button|link|tab|item)\s*$",
            "",
            query,
            flags=re.I,
        )
        if app:
            query = re.sub(
                rf"\s+(?:in|on|using|with)\s+{re.escape(app)}\s*$",
                "",
                query,
                flags=re.I,
            ).strip()
        payload: dict[str, Any] = {
            "action": "open_item",
            "query": (query or click.group("q").strip(" ."))[:200],
            "app": app or "front",
        }
        return "app_action", payload

    typed = re.search(r"\b(?:type|enter|write)\s+(?P<q>.+)$", raw, re.I)
    if typed:
        from app.ev.luna_code import looks_like_code_request

        if looks_like_code_request(raw):
            return None
        query = clean_computer_query(typed.group("q").strip(), app=app)
        if app:
            query = re.sub(
                rf"\s+(?:in|on|using|with)\s+{re.escape(app)}\s*$",
                "",
                query,
                flags=re.I,
            ).strip()
        return "app_action", {
            "action": "search",
            "query": (query or typed.group("q").strip())[:500],
            "text": (query or typed.group("q").strip())[:500],
            "app": app or "front",
        }

    search = re.search(r"\bsearch(?:\s+for)?\s+(?P<q>.+)$", raw, re.I)
    if search and app:
        query = _search_query_from_goal(raw) or clean_computer_query(
            search.group("q"), app=app
        )
        query = re.sub(
            rf"\s+(?:in|on|using|with)\s+{re.escape(app)}\s*$",
            "",
            query,
            flags=re.I,
        ).strip()
        dest = navigation_url_from_text(query)
        if dest and _browser_app_from_text(app, app):
            return "app_action", {
                "app": app,
                "action": "navigate",
                "query": dest,
                "url": dest,
            }
        return "app_action", {"app": app, "action": "search", "query": query}

    return None


def preferred_strategy_for_goal(*, app: str | None, bundle_id: str | None = None) -> str:
    spec = adapter_for(app, bundle_id)
    if spec is not None:
        return "semantic"
    return "ax"


def classify_tool_strategy(name: str, arguments: dict[str, Any] | None = None) -> str:
    args = arguments or {}
    action = str(args.get("action") or "").lower()
    app = str(args.get("app") or "").lower()
    if name in {"app_action", "computer"}:
        if "calculator" in app:
            return "keyboard"
        return "semantic"
    if name in {"screen_look", "see"}:
        return "vision"
    if name == "ui_action" and action in {"click_at", "screen_click"}:
        return "coordinate"
    if name in {"click", "double_click", "right_click", "drag"} and (
        args.get("x") is not None or args.get("x_normalized") is not None
    ):
        return "coordinate"
    if name == "ui_action" and action in {"keyboard", "paste", "menu"}:
        return "keyboard"
    if name in {"type", "paste", "key"}:
        return "keyboard"
    if name in {"inspect_ui", "ui_action", "read", "click", "double_click",
                "right_click", "scroll", "drag"}:
        return "ax"
    if name in {
        "open_app",
        "activate_app",
        "close_app",
        "list_apps",
        "computer_status",
        "open_url",
    }:
        return "lifecycle"
    return "other"


def next_strategy(current: str) -> str | None:
    order = ("semantic", "ax", "keyboard", "vision", "coordinate")
    try:
        index = order.index(current)
    except ValueError:
        return "ax"
    if index + 1 < len(order):
        return order[index + 1]
    return None


def looks_like_computer_task(text: str) -> bool:
    raw = (text or "").strip()
    if not raw:
        return False
    from app.ev.laptop_files import looks_like_file_task
    from app.memory.visual import wants_keep_visible

    if wants_keep_visible(raw):
        return False
    if re.search(r"\bopen (?:the )?camera\b", raw, re.I) and re.search(
        r"\b(?:remember|look|see|showing|hold)\b", raw, re.I
    ):
        return False
    if looks_like_file_task(raw):
        return True
    if _APP_NAME_RE.search(raw) or _COMPUTER_TASK_RE.search(raw):
        return True
    return bool(re.search(r"\b(open|launch|quit|play the|find my)\b", raw, re.I))


def computer_tools_from_specs(tools: list[dict] | tuple[dict, ...] | None) -> list[dict]:
    out: list[dict] = []
    for item in tools or ():
        if not isinstance(item, dict):
            continue
        name = str(item.get("name") or "")
        if name in COMPUTER_SCHEMA_TOOLS:
            out.append(item)
    out.sort(key=lambda item: str(item.get("name") or ""))
    return out


def computer_tool_schema_hash(tools: list[dict] | tuple[dict, ...] | None) -> str:
    canonical: list[dict[str, Any]] = []
    for item in computer_tools_from_specs(tools):
        raw_params = item.get("parameters")
        parameters: dict[str, Any] = raw_params if isinstance(raw_params, dict) else {}
        canonical.append(
            {
                "name": item.get("name"),
                "properties": sorted(
                    str(key)
                    for key in (parameters.get("properties") or {})
                    if isinstance(parameters.get("properties"), dict)
                ),
                "required": sorted(
                    str(key) for key in (parameters.get("required") or [])
                    if isinstance(parameters.get("required"), list)
                ),
            }
        )
    blob = json.dumps(canonical, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:16]


def evaluate_provider_computer_schema(
    *,
    advertised_tools: list[dict] | tuple[dict, ...] | None,
    acknowledged_names: list[str] | tuple[str, ...] | None,
    acknowledged_schemas: list[dict] | tuple[dict, ...] | None,
) -> dict[str, Any]:
    advertised = computer_tools_from_specs(advertised_tools)
    local_hash = computer_tool_schema_hash(advertised)
    advertised_names = [str(item.get("name") or "") for item in advertised]
    names = {str(name) for name in (acknowledged_names or ()) if name}
    missing = [name for name in advertised_names if name and name not in names]
    schema_by_name = {
        str(item.get("name")): item
        for item in (acknowledged_schemas or ())
        if isinstance(item, dict) and item.get("name")
    }
    property_gaps: dict[str, list[str]] = {}
    for tool, required in REQUIRED_SCHEMA_PROPERTIES.items():
        if tool not in advertised_names:
            continue
        meta = schema_by_name.get(tool) or {}
        present = set(meta.get("property_names") or [])
        if not present:
            continue
        gap = [key for key in required if key not in present]
        if gap:
            property_gaps[tool] = gap
    # F4 advertises the `computer` broker, not the supervised primitives.
    # Requiring open_app/inspect_ui in the provider session marks a live Mac
    # as unavailable and Evie refuses in-app control.
    if "computer" in advertised_names and "computer" in names:
        missing = []
        property_gaps = {}
    match = not missing and not property_gaps and (not advertised_names or bool(names))
    return {
        "computer_tool_schema_hash": local_hash,
        "tool_schema_match": match,
        "missing_tools": missing,
        "property_gaps": property_gaps,
        "computer_control_ready": match,
        "provider_tools_confirmed": bool(names) and not missing,
    }


def computer_envelope(
    result: dict[str, Any] | None,
    *,
    method: str | None = None,
    progress: str | None = None,
    failure_code: str | None = None,
    recoverable: bool | None = None,
    suggested_fallbacks: list[str] | None = None,
) -> dict[str, Any]:
    out = dict(result or {})
    ok = out.get("ok") is True
    executed = bool(out.get("executed", ok))
    verified = bool(out.get("verified"))
    raw_goal = out.get("goal")
    goal: dict[str, Any] = raw_goal if isinstance(raw_goal, dict) else {}
    terminal = str(goal.get("status") or "") in {"complete", "failed", "cancelled"}
    goal_complete = bool(goal.get("status") == "complete" and (goal.get("verified") or verified))
    must_continue = bool(out.get("must_continue"))
    if "must_continue" not in out:
        must_continue = (not terminal) and (not verified) and not out.get("cancelled")
    code = failure_code or (None if ok or verified else str(out.get("error") or "") or "failed")
    if code in {"", "None"}:
        code = None
    if recoverable is None:
        recoverable = bool(must_continue) and code not in {
            "cancelled",
            "playlist_not_found",
            "not_found",
            "permission_denied",
            "accessibility_denied",
        }
    spoken = str(out.get("spoken") or "")
    if _BUDGET_SPEECH_RE.search(spoken):
        spoken = "I couldn't get that done."
        out["spoken"] = spoken
    out["ok"] = ok
    out["executed"] = executed
    out["verified"] = verified
    out["goal_progress"] = progress or out.get("goal_progress") or goal.get("status") or "planning"
    out["goal_complete"] = bool(out.get("goal_complete", goal_complete))
    out["complete"] = bool(out["goal_complete"])
    out["must_continue"] = must_continue and not out["goal_complete"]
    out["method"] = method or out.get("method") or "unknown"
    out.setdefault("observed_state", out.get("observed") or goal.get("observed") or {})
    out["failure"] = {
        "code": None if (ok and verified) or out["goal_complete"] else code,
        "recoverable": bool(recoverable) and not out["goal_complete"],
    }
    if suggested_fallbacks is not None:
        out["suggested_fallbacks"] = list(suggested_fallbacks)
    else:
        out.setdefault("suggested_fallbacks", [])
    return out


def speech_claims_success(text: str) -> bool:
    return bool(_SUCCESS_CLAIM_RE.search(text or ""))


def speech_is_grounded(text: str, *, verified: bool, goal_complete: bool, failed: bool) -> bool:
    """False-success gate: a success claim requires verified completion."""

    if not speech_claims_success(text):
        return True
    if failed:
        return False
    return bool(verified and goal_complete)


def user_facing_terminal_speech(failure_code: str | None, fallback: str | None = None) -> str:
    code = str(failure_code or "")
    mapping = {
        "playlist_not_found": "I couldn't find that playlist.",
        "not_found": "I couldn't find that.",
        "cancelled": "Stopped.",
        "step_budget": "I couldn't get that done.",
        "strategy_budget_exhausted": "I couldn't get that done.",
        "cycle_detected": "That path wasn't working, so I stopped.",
        "ordinal_mismatch": "That wasn't the requested track.",
        "find_only": "You asked me to find it, not play it.",
    }
    if code in mapping:
        return mapping[code]
    return fallback or "I couldn't finish that."


def progress_milestone_for(name: str, result: dict[str, Any], *, previous: str | None) -> str:
    if result.get("goal_complete") or (
        isinstance(result.get("goal"), dict) and result["goal"].get("status") == "complete"
    ):
        return "GOAL_COMPLETE"
    if result.get("verified"):
        return "VERIFICATION_OBTAINED"
    if name in {"open_app", "activate_app"} and result.get("ok"):
        return "APP_OPEN"
    if name == "app_action" and result.get("error") not in {"playlist_not_found", "not_found"}:
        if result.get("playlist") or result.get("tracks") or result.get("playlists"):
            return "TARGET_FOUND"
        if result.get("executed"):
            return "ACTION_EXECUTED"
        return "CONTROL_METHOD_SELECTED"
    if name == "inspect_ui":
        if result.get("target_found"):
            return "TARGET_FOUND"
        return "APP_RESOLVED" if previous in {None, "NEW"} else (previous or "APP_RESOLVED")
    if name == "ui_action" and result.get("executed"):
        return "ACTION_EXECUTED"
    if name == "screen_look" and result.get("ok"):
        return "ACTION_EXECUTED"
    return previous or "NEW"


def is_progress(previous: str | None, current: str | None) -> bool:
    if not current or current == previous:
        return False
    try:
        return MILESTONES.index(current) > MILESTONES.index(previous or "NEW")
    except ValueError:
        return current != previous
