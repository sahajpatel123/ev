"""General in-app item actions: open/close a *thing* inside an app.

One logic for every app, not a recipe per app:

- The utterance is normalized to ``{verb, app, item, kind, background}``
  (``"open John's chat in WhatsApp"`` → open / WhatsApp / John / chat).
- An app registry declares how each app can be driven (browser tab, URL
  scheme, AppleScript, or the live EV.app control channel).
- Drivers are tried best-first and each one returns truthful evidence; when
  no driver can reach the item, the app is opened and the gap is spoken.

Sending (``send_message``) and reading (``recall``) keep their own paths;
this module is only about navigating to a thing that already exists.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any
from urllib.parse import quote

from sqlalchemy.ext.asyncio import AsyncSession

_VERB_OPEN = r"open|show|view|display|go\s+to|switch\s+to|bring\s+up|pull\s+up|jump\s+to|take\s+me\s+to"
_VERB_PLAY = r"play|start\s+playing|resume"
_VERB_CLOSE = r"close|dismiss"
_VERB_RE = re.compile(rf"^(?P<verb>{_VERB_OPEN}|{_VERB_PLAY}|{_VERB_CLOSE})\s+(?P<rest>.+)$", re.I)
_BACKGROUND_RE = re.compile(
    r"\b(?:in\s+the\s+background|without\s+(?:opening|switching|bringing|stealing)|"
    r"don'?t\s+(?:open|switch|bring|steal)|no\s+focus)\b",
    re.I,
)
_INQUIRY_RE = re.compile(
    r"^\s*(?:what|which|who|when|where|why|did|do|does|is|are|any|check)\b", re.I
)
_RANDOM_RE = re.compile(r"\b(?:random(?:ly)?|shuffled?|shuffle)\b", re.I)
_COLLECTION_RE = re.compile(
    r"\bfrom\s+(?:my\s+|the\s+)?(?P<collection>.+?)"
    r"(?=\s+(?:in|inside|on|using)\s+[A-Za-z])",
    re.I,
)

_KINDS: tuple[tuple[str, str], ...] = (
    ("chat", r"chats?|conversations?|threads?"),
    ("contact", r"contacts?|profiles?"),
    ("playlist", r"playlists?|albums?"),
    ("track", r"tracks?|songs?"),
    ("note", r"notes?|memos?"),
    ("folder", r"folders?|directories"),
    ("file", r"files?|documents?"),
    ("channel", r"channels?|workspaces?|boards?|servers?"),
    ("inbox", r"inbox"),
    ("tab", r"tabs?"),
    ("place", r"places?|locations?|maps?"),
)
_KIND_WORD_RE = re.compile(
    "|".join(f"(?P<{name}>{pattern})" for name, pattern in _KINDS), re.I
)


@dataclass(frozen=True)
class InAppTarget:
    label: str
    aliases: tuple[str, ...]
    web: bool = False
    url_template: str | None = None
    apple_script: str | None = None
    kinds: tuple[str, ...] = ()


TARGETS: tuple[InAppTarget, ...] = (
    InAppTarget(
        "WhatsApp",
        ("whatsapp", "whats app", "wa"),
        web=True,
        url_template="whatsapp://send?phone={digits}",
        kinds=("chat",),
    ),
    InAppTarget(
        "Messages",
        ("messages", "imessage", "i message"),
        url_template="sms://{digits}",
        kinds=("chat",),
    ),
    InAppTarget("Mail", ("mail", "email"), kinds=("inbox",)),
    InAppTarget(
        "Music",
        ("music", "apple music"),
        apple_script="music_play",
        kinds=("playlist", "track"),
    ),
    InAppTarget("Notes", ("notes",), apple_script="notes_show", kinds=("note",)),
    InAppTarget("Finder", ("finder", "files"), kinds=("folder", "file")),
    InAppTarget("Maps", ("maps", "apple maps"), url_template="maps://?q={quote}", kinds=("place",)),
    InAppTarget(
        "Spotify",
        ("spotify",),
        url_template="spotify:search:{quote}",
        kinds=("playlist", "track"),
    ),
    InAppTarget("Slack", ("slack",), web=True, kinds=("channel",)),
    InAppTarget("Telegram", ("telegram",), web=True, kinds=("chat",)),
    InAppTarget("Safari", ("safari",), kinds=("tab",)),
    InAppTarget("Google Chrome", ("chrome", "google chrome", "browser"), kinds=("tab",)),
    InAppTarget(
        "YouTube",
        ("youtube", "you tube"),
        web=True,
        url_template="https://www.youtube.com/results?search_query={quote}",
        kinds=("track", "tab"),
    ),
)

_ALIAS_INDEX: tuple[tuple[str, str], ...] = tuple(
    sorted(
        ((alias, target.label) for target in TARGETS for alias in target.aliases),
        key=lambda pair: len(pair[0]),
        reverse=True,
    )
)


def target_for(app_label: str | None) -> InAppTarget | None:
    raw = (app_label or "").strip().lower()
    if not raw:
        return None
    for target in TARGETS:
        if raw == target.label.lower() or raw in target.aliases:
            return target
    return None


@dataclass(frozen=True)
class InAppIntent:
    verb: str
    app: str | None
    item: str | None
    kind: str
    background: bool
    random: bool = False
    collection: str = ""

    def as_args(self) -> dict[str, Any]:
        payload = {
            "app": self.app,
            "item": self.item,
            "kind": self.kind,
            "verb": self.verb,
            "background": self.background,
        }
        if self.random:
            payload["random"] = True
        if self.collection:
            payload["playlist"] = self.collection
        return payload


def _strip_vocatives(text: str) -> str:
    core = (text or "").strip()
    for _ in range(4):
        nxt = re.sub(
            r"^(?:hey|hi|hello|ok|okay|please|evie|just|can\s+you|could\s+you|"
            r"will\s+you|would\s+you|i\s+want\s+you\s+to|i\s+need\s+you\s+to)[,!]?\s+",
            "",
            core,
            count=1,
            flags=re.I,
        ).strip()
        if nxt == core:
            break
        core = nxt
    return core


def _find_kind(text: str) -> str | None:
    match = _KIND_WORD_RE.search(text or "")
    if not match:
        return None
    return match.lastgroup


_ITEM_STOPWORDS = frozenset(
    {
        "the",
        "a",
        "an",
        "my",
        "our",
        "your",
        "in",
        "on",
        "at",
        "inside",
        "some",
        "any",
        "first",
        "second",
        "it",
        "this",
        "that",
        "them",
        "you",
        "me",
        "mine",
        "please",
    }
)


def _clean_item(value: str) -> str:
    item = (value or "").strip().strip("\"'")
    item = re.sub(r"^(?:the|my|our)\s+", "", item, flags=re.I)
    item = re.sub(r"\s+(?:in|on|at|inside)\s+\w+\s*$", "", item, flags=re.I)
    item = re.sub(r"[.?!,;:]+$", "", item).strip()
    if item.lower() in _ITEM_STOPWORDS:
        return ""
    if not item or len(item) < 2 or len(item) > 80:
        return ""
    return item


def parse_in_app_intent(text: str | None) -> InAppIntent | None:
    """Normalize an in-app navigation ask, or None when it is not one.

    ``open John's chat in WhatsApp`` → open/WhatsApp/John/chat.
    Sends and questions are left to their own pipelines.
    """

    raw = (text or "").strip()
    if not raw or _INQUIRY_RE.search(raw):
        return None
    from app.ev.send_intent import parse_send_intent

    if parse_send_intent(raw) is not None:
        return None
    core = _strip_vocatives(raw)
    match = _VERB_RE.match(core)
    if not match:
        return None
    verb_word = match.group("verb").lower()
    verb = "open"
    if re.match(rf"^(?:{_VERB_PLAY})$", verb_word):
        verb = "play"
    elif re.match(rf"^(?:{_VERB_CLOSE})$", verb_word):
        verb = "close"
    rest = match.group("rest").strip()
    background = bool(_BACKGROUND_RE.search(rest))
    rest = _BACKGROUND_RE.sub("", rest).strip()
    random = bool(_RANDOM_RE.search(rest))
    collection = ""
    collected = _COLLECTION_RE.search(rest)
    if collected:
        collection = _clean_item(collected.group("collection"))
        # Keep the app preposition; drop "from <collection>" so the item parser
        # does not treat the playlist as the app.
        rest = (rest[: collected.start()] + " " + rest[collected.end() :]).strip()
        rest = _RANDOM_RE.sub(" ", rest)
        rest = re.sub(r"\s+", " ", rest).strip()
    # "show me the note X" / "open WhatsApp and then John's chat"
    rest = re.sub(r"^(?:me|us)\s+", "", rest, flags=re.I).strip()
    rest = re.sub(r"^(?:and|then|,)\s+(?:open\s+|show\s+|go\s+to\s+)?", "", rest, flags=re.I).strip()
    if re.search(
        r"\bopen\s+(?:the\s+)?[A-Za-z][\w .+-]{1,40}?\s+(?:and|then|,)\s+"
        r"(?:open|go to|visit|navigate)\b",
        core,
        re.I,
    ):
        # "open Safari and open YouTube inside it" is a computer compound.
        return None

    app: str | None = None
    app_alias: str | None = None
    hosted = re.search(
        r"\b(?:in|inside|on|using|from|with)\s+(?:the\s+)?"
        r"(?P<slot>[A-Za-z][\w .+-]{0,40})$",
        rest,
        re.I,
    )
    if hosted:
        slot = re.sub(r"\s+app$", "", hosted.group("slot").strip(), flags=re.I)
        for alias, label in _ALIAS_INDEX:
            if re.search(rf"(?<![\w]){re.escape(alias)}(?![\w])", slot, re.I):
                app = label
                app_alias = alias
                break
    if app is None:
        for alias, label in _ALIAS_INDEX:
            if re.search(rf"(?<![\w]){re.escape(alias)}(?![\w])", rest, re.I):
                app = label
                app_alias = alias
                break
    item_rest = rest
    if app_alias:
        item_rest = re.sub(
            rf"(?<![\w]){re.escape(app_alias)}(?![\w])", " ", item_rest, count=1, flags=re.I
        ).strip()
        item_rest = re.sub(r"\b(?:in|on|at|inside|from)\s*$", "", item_rest, flags=re.I).strip()
        item_rest = re.sub(r"^\s*(?:in|on|at|inside|from)\s+", "", item_rest, flags=re.I).strip()
        item_rest = re.sub(
            r"^(?:and|then|,)\s+(?:open\s+|show\s+|go\s+to\s+)?", "", item_rest, flags=re.I
        ).strip()
        play_led = re.match(rf"^(?:{_VERB_PLAY})\s+(?P<item>.+)$", item_rest, re.I)
        if play_led:
            verb = "play"
            item_rest = play_led.group("item").strip()

    kind = _find_kind(item_rest) or _find_kind(rest)
    item = ""

    # "John's chat / John's playlist"
    possessive = re.search(
        r"(?P<item>.+?)['’]s\s+(?:chat|conversation|thread|profile|contact|playlist|album|note|inbox|tab)",
        item_rest,
        re.I,
    )
    if possessive:
        item = _clean_item(possessive.group("item"))
    if not item:
        # "chat with John / playlist for John"
        with_kind = re.search(
            r"(?:chat|conversation|thread|profile|contact|playlist|album|note|tab|channel)"
            r"\s+(?:with|for|of|from)\s+(?P<item>.+)$",
            item_rest,
            re.I,
        )
        if with_kind:
            item = _clean_item(with_kind.group("item"))
            kind = kind or _find_kind(item_rest)
    if not item:
        # "note Groceries / folder Downloads / channel general"
        kind_first = re.search(
            r"^(?:the\s+)?(?:chat|conversation|thread|profile|contact|playlist|album|note|"
            r"folder|file|channel|board|workspace|tab|place)"
            r"\s+(?:called\s+|named\s+)?(?P<item>.+)$",
            item_rest,
            re.I,
        )
        if kind_first:
            item = _clean_item(kind_first.group("item"))
    if not item:
        # "open the chat with John in Messages" already consumed; fall back to
        # the whole remainder when it is a clean name and a kind was named.
        remainder = re.sub(
            r"\b(?:chats?|conversations?|threads?|profiles?|contacts?|playlists?|albums?|"
            r"notes?|memos?|folders?|files?|channels?|workspaces?|boards?|tabs?|places?)\b",
            " ",
            item_rest,
            flags=re.I,
        )
        remainder = re.sub(r"\b(?:with|for|of|from|called|named)\b", " ", remainder, flags=re.I)
        remainder = _RANDOM_RE.sub(" ", remainder)
        remainder = re.sub(
            r"\b(?:some|a|an|song|track|video|clip)\b", " ", remainder, flags=re.I
        )
        remainder = _clean_item(remainder)
        # Named app: "open YouTube from Safari" / "play lofi in Music" do not
        # need a kind word. Play without an item still needs a name unless
        # they asked for a random track from a collection.
        if remainder and (kind or verb == "play" or app):
            item = remainder

    if verb == "play" and not kind:
        if collection and item and item.lower() != collection.lower():
            kind = "track"
        elif collection:
            kind = "playlist"
        else:
            kind = "track"
    if not item and collection and verb == "play":
        item = collection
        kind = "playlist"
    if not item:
        return None
    if app is None and not kind:
        return None
    # Without an app, only chat/contact items are ours; files/folders keep
    # their existing computer/file pipelines.
    if app is None and (kind or "") not in {"chat", "contact"}:
        return None
    return InAppIntent(
        verb=verb,
        app=app,
        item=item,
        kind=kind or "item",
        background=background,
        random=random,
        collection=collection,
    )


async def _guess_app_for_chat(item: str) -> str | None:
    """Pick an app for a chat item by asking each channel's own store."""

    from app.ev.messaging.native import resolve_native_contact

    whatsapp = await resolve_native_contact("whatsapp", item)
    if isinstance(whatsapp, dict) and whatsapp.get("status") == "unique":
        return "WhatsApp"
    return None


def _destination_url_for_item(item: str) -> str | None:
    """Homepage/URL when the item is a named site (YouTube from Safari)."""

    raw = (item or "").strip()
    if not raw:
        return None
    from app.ev.computer_strategy import named_site_in_text, navigation_url_from_text

    dest = navigation_url_from_text(raw) or named_site_in_text(raw)
    if dest:
        return dest
    other = target_for(raw)
    if other is not None and other.label == "YouTube":
        return "https://www.youtube.com/"
    return None


def _render_url(target: InAppTarget, item: str, kind: str) -> str | None:
    template = target.url_template
    if not template:
        return None
    digits = re.sub(r"\D+", "", item)
    if "{digits}" in template:
        if len(digits) < 7:
            return None
        return template.replace("{digits}", digits)
    if "{quote}" in template:
        return template.replace("{quote}", quote(item, safe=""))
    return None


def _applescript_escape(value: str) -> str:
    return value.replace("\\", "\\\\").replace('"', '\\"')


async def _drive_applescript(
    target: InAppTarget, item: str, kind: str, *, random: bool = False, playlist: str = ""
) -> dict[str, Any] | None:
    if not target.apple_script:
        return None
    import asyncio
    import shutil

    if not shutil.which("osascript"):
        return None
    safe = _applescript_escape(item)
    if target.apple_script == "music_play":
        playlist_name = _applescript_escape(playlist or (item if kind == "playlist" else ""))
        track_name = _applescript_escape(item) if kind != "playlist" else ""
        if playlist_name and random:
            script = f'''
            tell application "Music"
              try
                set theList to playlist "{playlist_name}"
                set total to count of tracks of theList
                if total is 0 then return "{{\\"ok\\": false, \\"error\\": \\"not_found\\"}}"
                set pick to random number from 1 to total
                play track pick of theList
                delay 0.4
                return "{{" & "\\"ok\\": true, \\"playing\\": " & (player state is playing) & "}}"
              on error errMsg
                return "{{\\"ok\\": false, \\"error\\": \\"not_found\\"}}"
              end try
            end tell
            '''
        elif playlist_name and not track_name:
            finder = f'playlist "{playlist_name}"'
            script = f'''
            tell application "Music"
              try
                play {finder}
                delay 0.4
                return "{{" & "\\"ok\\": true, \\"playing\\": " & (player state is playing) & "}}"
              on error errMsg
                return "{{\\"ok\\": false, \\"error\\": \\"not_found\\"}}"
              end try
            end tell
            '''
        else:
            finder = f'playlist "{safe}"' if kind == "playlist" else f'track "{safe}"'
            script = f'''
            tell application "Music"
              try
                play {finder}
                delay 0.4
                return "{{" & "\\"ok\\": true, \\"playing\\": " & (player state is playing) & "}}"
              on error errMsg
                return "{{\\"ok\\": false, \\"error\\": \\"not_found\\"}}"
              end try
            end tell
            '''
    elif target.apple_script == "notes_show":
        script = f'''
        tell application "Notes"
          try
            show note "{safe}"
            return "{{\\"ok\\": true, \\"shown\\": true}}"
          on error errMsg
            return "{{\\"ok\\": false, \\"error\\": \\"not_found\\"}}"
          end try
        end tell
        '''
    else:
        return None
    try:
        proc = await asyncio.create_subprocess_exec(
            "osascript",
            "-",
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, _ = await asyncio.wait_for(proc.communicate(script.encode("utf-8")), timeout=15.0)
    except Exception:
        return None
    raw = (stdout or b"").decode("utf-8", errors="replace").strip()
    if not raw:
        return None
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return None
    if not data.get("ok"):
        return {"ok": False, "error": "item_not_found", "driver": "applescript"}
    return {
        "ok": True,
        "matched": True,
        "driver": "applescript",
        "title": item,
        "focus_theft": 0,
    }


_TAB_ALL_RE = re.compile(r"^(?:all|every|each|both)$", re.I)
_TAB_FILLER_RE = re.compile(r"^(?:the|a|an|this|that|these|those|it|them|tabs?)$", re.I)


def _tab_close_spec(item: str) -> tuple[str, bool]:
    """Split a tab-close item into ``(query, all_tabs)``.

    "all tabs" closes every tab; filler words ("the tab") name no tab, so the
    driver falls back to the tab the owner is looking at.
    """

    value = (item or "").strip()
    if _TAB_ALL_RE.match(value):
        return "", True
    if _TAB_FILLER_RE.match(value):
        return "", False
    return value, False


async def _drive_live_app(
    session: AsyncSession,
    target: InAppTarget,
    item: str,
    kind: str,
    verb: str,
    *,
    actor: str,
    live_session_id: str | None,
    device_id,
    request_id: str | None,
    all_items: bool = False,
    random: bool = False,
    playlist: str = "",
) -> dict[str, Any] | None:
    """Universal EV.app content opener, when the desktop app is live."""

    from app.ev.computer import handle_computer_tool

    action = "open_item"
    if verb == "play":
        action = "play"
    elif verb == "close":
        action = "close_tab"
    elif kind == "tab":
        action = "navigate"
    payload: dict[str, Any] = {"app": target.label, "action": action, "query": item}
    if all_items:
        payload["all"] = True
    if random:
        payload["random"] = True
    if playlist:
        payload["playlist"] = playlist
    result = await handle_computer_tool(
        session,
        "app_action",
        payload,
        actor=actor,
        live_session_id=live_session_id,
        device_id=str(device_id) if device_id else None,
        request_id=request_id,
    )
    if not isinstance(result, dict) or not result.get("ok"):
        return None
    return {
        "ok": True,
        "matched": bool(result.get("verified") or result.get("executed")),
        "driver": "ev_app",
        "title": str(result.get("title") or result.get("query") or item),
        "focus_theft": 0 if result.get("focus_stolen") is False else 1,
        "raw": result,
    }


async def _open_url_background(url: str) -> dict[str, Any]:
    import asyncio
    import shutil

    if not shutil.which("open"):
        return {"ok": False, "error": "open_unavailable"}
    try:
        proc = await asyncio.create_subprocess_exec(
            "/usr/bin/open",
            "-g",
            url,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.PIPE,
        )
        _, stderr = await asyncio.wait_for(proc.communicate(), timeout=10.0)
    except Exception:
        return {"ok": False, "error": "open_failed"}
    if proc.returncode != 0:
        return {"ok": False, "error": "open_failed", "diagnosis": (stderr or b"").decode()[:120]}
    return {"ok": True, "matched": True, "driver": "url_scheme", "title": url, "focus_theft": 0}


def _shape_app_result(
    result: dict[str, Any], *, app_label: str, item: str, verb: str
) -> dict[str, Any]:
    out = dict(result or {})
    if app_label:
        out.setdefault("app", app_label)
    if item:
        out.setdefault("item", item)
    if out.get("needs_app"):
        return out
    ok = bool(out.get("ok"))
    out.setdefault("executed", ok)
    out.setdefault("verified", bool(out.get("matched") or out.get("verified")))
    if not out.get("spoken"):
        if verb == "close":
            out["spoken"] = (
                f"Closed {item} in {app_label}."
                if ok
                else f"I couldn't close {item} in {app_label}."
            )
        elif ok:
            out["spoken"] = (
                f"Playing {item} in {app_label}."
                if verb == "play"
                else f"Opened {item} in {app_label}."
            )
        else:
            out["spoken"] = f"I couldn't open {item} in {app_label}."
    return out


async def act_in_app(
    session: AsyncSession,
    *,
    app: str | None,
    item: str | None,
    kind: str | None = None,
    verb: str = "open",
    background: bool = False,
    random: bool = False,
    playlist: str | None = None,
    actor: str = "master",
    live_session_id: str | None = None,
    device_id=None,
    request_id: str | None = None,
) -> dict[str, Any]:
    result = await _act_in_app(
        session,
        app=app,
        item=item,
        kind=kind,
        verb=verb,
        background=background,
        random=random,
        playlist=playlist,
        actor=actor,
        live_session_id=live_session_id,
        device_id=device_id,
        request_id=request_id,
    )
    return _shape_app_result(
        result,
        app_label=str(result.get("app") or app or ""),
        item=(item or "").strip(),
        verb=verb,
    )


async def _act_in_app(
    session: AsyncSession,
    *,
    app: str | None,
    item: str | None,
    kind: str | None = None,
    verb: str = "open",
    background: bool = False,
    random: bool = False,
    playlist: str | None = None,
    actor: str = "master",
    live_session_id: str | None = None,
    device_id=None,
    request_id: str | None = None,
) -> dict[str, Any]:
    """Open/play/close a named item inside an app, best driver first."""

    who = (item or "").strip()
    if not who:
        return {
            "ok": False,
            "error": "missing_item",
            "spoken": "What should I close?" if verb == "close" else "What should I open?",
        }
    target = target_for(app)
    if target is None:
        if app:
            if verb == "close":
                return {
                    "ok": False,
                    "app": app,
                    "item": who,
                    "error": "close_not_supported",
                    "spoken": f"I don't know how to close {who} inside {app}.",
                }
            # Unknown app label: let open_app resolve/installed-lookup fail honestly.
            from app.ev.apps import open_app

            opened = await open_app(session, {"name": app}, actor=actor)
            if opened.get("ok"):
                return {
                    **opened,
                    "item_found": False,
                    "spoken": f"I opened {app}, but I don't know how to find {who} inside it.",
                }
            return opened
        guessed = await _guess_app_for_chat(who)
        if guessed is None:
            return {
                "ok": False,
                "needs_app": True,
                "error": "app_required",
                "spoken": (
                    f"Which app should I close {who} in — WhatsApp or Messages?"
                    if verb == "close"
                    else f"Which app should I open {who} in — WhatsApp or Messages?"
                ),
            }
        target = target_for(guessed)
    assert target is not None

    if verb == "close":
        # Closing an item must never quit the app: a whole-app quit destroys
        # every other tab/window. Only a tab can be closed in place; anything
        # else is refused and the owner is asked to confirm a whole-app quit
        # (the close_app tool owns that, on an unambiguous quit-the-app ask).
        if kind == "tab" or (not kind and "tab" in target.kinds):
            query, all_tabs = _tab_close_spec(who)
            live = await _drive_live_app(
                session,
                target,
                query,
                "tab",
                "close",
                actor=actor,
                live_session_id=live_session_id,
                device_id=device_id,
                request_id=request_id,
                all_items=all_tabs,
            )
            if live is not None:
                raw = live.get("raw")
                bridge_spoken = ""
                if isinstance(raw, dict):
                    bridge_spoken = str(raw.get("spoken") or "").strip()
                return {
                    **live,
                    "app": target.label,
                    "item": who,
                    # The driver knows which tab it closed; only it may name it.
                    "spoken": bridge_spoken or f"Closed the tab in {target.label}.",
                }
            return {
                "ok": False,
                "app": target.label,
                "item": who,
                "error": "close_tab_failed",
                "spoken": (
                    f"I couldn't close that tab in {target.label}. "
                    f'Say "quit {target.label}" if you want the whole app closed.'
                ),
            }
        if not kind:
            return {
                "ok": False,
                "app": target.label,
                "item": who,
                "error": "close_needs_confirmation",
                "spoken": (
                    f'I didn\'t close anything. Say "quit {target.label}" '
                    f"if you want the whole app closed."
                ),
            }
        return {
            "ok": False,
            "app": target.label,
            "item": who,
            "error": "close_not_supported",
            "spoken": f"I can quit {target.label}, but I can't close one {kind} inside it.",
        }

    if verb in {"open", "play"} and target.web:
        from app.ev.in_app_web import drive_item_in_web

        web = await drive_item_in_web(
            target.label, who, kind=kind, background=background
        )
        if web is not None:
            return {**web, "app": target.label, "item": who}

    dest_url = None
    if verb in {"open", "play"} and (
        target.label in {"Safari", "Google Chrome"} or kind == "tab"
    ):
        dest_url = _destination_url_for_item(who)
    live_item = dest_url or who
    live_kind = "tab" if dest_url else (kind or "item")
    live_verb = "open" if dest_url else verb

    live = await _drive_live_app(
        session,
        target,
        live_item,
        live_kind,
        live_verb,
        actor=actor,
        live_session_id=live_session_id,
        device_id=device_id,
        request_id=request_id,
        random=bool(random) and not dest_url,
        playlist=str(playlist or "").strip() if not dest_url else "",
    )
    if live is not None:
        return {**live, "app": target.label, "item": who}

    if dest_url:
        from app.ev.mac_host import open_url_in_app

        hosted = open_url_in_app(target.label, dest_url, background=background)
        if hosted.get("ok"):
            return {
                **hosted,
                "app": target.label,
                "item": who,
                "url": dest_url,
                "driver": "mac_open",
                "matched": True,
            }

    scripted = await _drive_applescript(
        target,
        who,
        kind or "item",
        random=bool(random),
        playlist=str(playlist or "").strip(),
    )
    if scripted is not None:
        return {**scripted, "app": target.label, "item": who}

    url = dest_url or _render_url(target, who, kind or "item")
    if url:
        opened = await _open_url_background(url)
        if opened.get("ok"):
            return {**opened, "app": target.label, "item": who}

    from app.ev.apps import open_app

    opened = await open_app(session, {"name": target.label}, actor=actor)
    if opened.get("ok"):
        return {
            **opened,
            "driver": "open_app_only",
            "item_found": False,
            "spoken": f"I opened {target.label}, but I couldn't find {who} automatically.",
        }
    return {
        **opened,
        "item_found": False,
    }
