"""Deterministic detection of work the local Mac agent must do itself.

The conversational kernel is the right mind for talk, memory, and knowledge.
It is the wrong executor for "edit this file", "open Ghostty and resume those
sessions", or "read what is on my screen": those turns need the agent loop with
real tools, and the brain's four-round conversational budget cannot carry them.

This module answers one question before any model call: *is this turn about
operating the owner's machine?* It is deliberately conservative — a wrong
"yes" sends a normal question to the agent, which costs a few seconds; a wrong
"no" reproduces the dead end where Evie narrates work instead of doing it.
"""

from __future__ import annotations

import re

# Things that only exist on the owner's machine. A turn naming one of these
# and asking for an action, or asking about its current state, is local work.
_LOCAL_NOUNS = re.compile(
    r"\b(?:"
    r"files?|folders?|director(?:y|ies)|desktop|downloads?|documents?|pictures?|"
    r"screenshots?|images?|photos?|pdfs?|spreadsheets?|csvs?|invoices?|receipts?|"
    r"terminal|ghostty|ghosty|ghost\s+terminal|iterm|tmux|shell|command\s+line|cli|pane|tab|window|"
    r"finder|spotlight|clipboard|trash|disk|disk\s+space|storage|space|drive|backups?|archives?|zips?|"
    r"scripts?|logs?|repos?|repositor(?:y|ies)|git|branches?|commits?|"
    r"apps?|applications?|process(?:es)?|settings|preferences|"
    r"opencode|claude\s+code|muse\s+code|cursor|vs\s?code|"
    r"mac|macbook|laptop|computer"
    r")\b",
    re.IGNORECASE,
)

_ACTION_VERBS = re.compile(
    r"\b(?:"
    r"create|make|write|save|edit|update|alter|append|insert|replace|rename|"
    r"move|copy|delete|remove|trash|clean(?:\s*up)?|organi[sz]e|sort|archive|"
    r"compress|unzip|extract|convert|resize|merge|split|install|uninstall|"
    r"run|execute|launch|open|close|quit|kill|start|stop|restart|resume|"
    r"read|show|list|find|search|grep|scan|look|see|inspect|observe|compare|"
    r"capture|take|describe|"
    r"dedupe|download|upload|export|import|back\s?up|sync|"
    r"build|compile|fix|repair|refactor|test|deploy|"
    r"check|examine|analyse|analyze|diagnose|investigate|audit|free|clear\s+up|"
    r"type|click|press|drag|scroll|paste|select|focus|switch|"
    r"arrange|split|tile|maximize|minimize|restore|resize|move\s+window|"
    r"send|schedule|set\s?up|prepare|collect|gather"
    r")\b",
    re.IGNORECASE,
)

# Possessive/deictic phrases that anchor a question to this machine. They let a
# question ("what files are on my desktop") count as local without an action
# verb in the imperative.
_LOCAL_ANCHOR = re.compile(
    r"\b(?:"
    r"my\s+(?:mac|macbook|laptop|computer|desktop|files?|folders?|downloads?|"
    r"documents?|pictures?|screen|terminal|apps?|process(?:es)?|clipboard|"
    r"sessions?|tabs?|windows?|disk|storage|drive)"
    r"|on\s+(?:my|the|this)\s+(?:mac|macbook|laptop|computer|desktop|screen|terminal)"
    r"|this\s+(?:mac|macbook|laptop|computer)"
    r"|in\s+my\s+(?:files?|folders?|desktop|downloads?|documents?|pictures?)"
    r"|this\s+(?:screenshot|screen|image|file|folder|terminal|window|tab)"
    r")\b",
    re.IGNORECASE,
)

_MULTI_STEP = re.compile(
    r"\b(?:then|and then|after that|finally|also|followed by)\b|;\s*|,\s*and\b",
    re.IGNORECASE,
)

_QUESTION_START = re.compile(
    r"^\s*(?:what|who|when|where|why|how|which|is|are|was|were|do|does|did|"
    r"can you|could you|would you|explain|tell me about)\b",
    re.IGNORECASE,
)

# A terminal/vision turn is local even without a generic action verb.
_TERMINAL_SIGNAL = re.compile(
    r"\b(?:ghostty|ghosty|ghost\s+terminal|iterm|tmux|terminal|shell|command\s+line|"
    r"opencode|claude\s+code|muse\s+code|resume\s+(?:my\s+)?(?:session|sessions|work))\b",
    re.IGNORECASE,
)

_VISION_SIGNAL = re.compile(
    r"\b(?:screenshot|screen\s?shot|screen|clipboard|what\s+do\s+you\s+see|"
    r"read\s+(?:this|that|the)\s+(?:image|screenshot|screen))\b",
    re.IGNORECASE,
)

# Capability fast paths own these when they are a single, known ask. Compound
# versions still reach the agent because _MULTI_STEP wins below.
_SIMPLE_CAP = re.compile(
    r"\b(?:"
    r"weather|forecast|temperature|remind|reminder|timer|alarm|"
    r"message|text|whatsapp|email|mail|call|dial|play|pause|skip|volume|"
    r"remember that|recall|who is|what is|what's the (?:time|date|weather)"
    r")\b",
    re.IGNORECASE,
)

# Software artifacts belong to the coding lane (`code.act` / studio), not to
# the generic computer agent; the code lane owns scaffolding and its own runner.
_CODE_ARTIFACT = re.compile(
    r"\b(?:write|build|create|make|implement|code|develop)\b.{0,50}"
    r"\b(?:script|program|app|application|website|web\s?site|web\s?app|api|"
    r"function|class|module|game|dashboard|bot|library|package|cli|service)\b",
    re.IGNORECASE,
)

# A single create/write of one file or folder is a fast-path file ask, not a
# multi-step computer job. Compound versions still reach the agent below.
_SIMPLE_CREATE = re.compile(
    r"^\s*(?:please\s+)?(?:make|create|write|save)\b", re.IGNORECASE
)

_STUDIO = re.compile(
    r"\b(?:overnight|while i (?:sleep|am away)|coding job|code studio|"
    r"continue (?:the )?(?:coding )?(?:job|studio|goal)|steer (?:the )?(?:job|studio)|"
    r"background (?:coding )?(?:job|task))\b",
    re.IGNORECASE,
)

# Pure knowledge/explanation asks never belong to the executor, even when they
# mention a local noun ("what is Ghostty", "how does tmux work").
_KNOWLEDGE = re.compile(
    r"^\s*(?:what\s+(?:is|are|was|were)\s+(?:a|an|the)?\s*|"
    r"how\s+(?:do|does|did|can|should)\b|"
    r"why\s+(?:is|are|do|does|did)\b|"
    r"explain\b|tell\s+me\s+about\b|define\b|compare\b|"
    r"what\s+does\b)",
    re.IGNORECASE,
)


def looks_like_local_work(text: str) -> bool:
    """True when this turn needs the local agent's hands, not the chat mind."""
    raw = (text or "").strip()
    if not raw or len(raw) > 4000:
        return False
    if _STUDIO.search(raw):
        return False
    if _CODE_ARTIFACT.search(raw):
        return False
    anchor = bool(_LOCAL_ANCHOR.search(raw))
    noun = bool(_LOCAL_NOUNS.search(raw))
    verb = bool(_ACTION_VERBS.search(raw))
    multi = bool(_MULTI_STEP.search(raw))
    terminal = bool(_TERMINAL_SIGNAL.search(raw))
    vision = bool(_VISION_SIGNAL.search(raw))
    question = bool(_QUESTION_START.search(raw))

    # "What is Ghostty?" is knowledge, not control. An explicit anchor
    # ("what is on my screen") overrides this because it names this machine.
    if question and _KNOWLEDGE.search(raw) and not anchor:
        return False

    # Single known capability with no compound clause keeps its fast path.
    if _SIMPLE_CAP.search(raw) and not multi and not anchor:
        return False

    # A single create/write of one file or folder keeps the file fast path;
    # only compound or terminal/vision work belongs to the computer agent.
    # "create two files ... a and b" is compound even without "then".
    if _SIMPLE_CREATE.match(raw) and not multi and not terminal and not vision:
        plural = re.search(
            r"\b(?:two|three|four|five|\d+)\s+(?:files?|folders?)\b", raw, re.I
        )
        if not plural and " and " not in raw:
            return False

    if anchor and (verb or terminal or vision or multi):
        return True
    # "What files are on my desktop" is a state question about this machine,
    # not a knowledge question; the agent answers it by looking.
    if question and anchor and (noun or terminal or vision):
        return True
    if noun and (verb or multi):
        return True
    if terminal and (verb or multi or re.search(r"\bsessions?\b", raw, re.I)):
        return True
    if vision and (verb or question or multi):
        return True
    # "run those commands from the screenshot" has neither a generic noun nor
    # an action verb the first clause alone would catch.
    return bool(re.search(r"\b(?:those|these)\s+(?:commands?|sessions?|tabs?)\b", raw, re.I))


def describe_route(text: str) -> dict[str, object]:
    """Small diagnostic used by tests and the status surface."""
    return {
        "local": looks_like_local_work(text),
        "full_agent": needs_full_agent(text),
        "anchor": bool(_LOCAL_ANCHOR.search(text or "")),
        "noun": bool(_LOCAL_NOUNS.search(text or "")),
        "verb": bool(_ACTION_VERBS.search(text or "")),
        "multi": bool(_MULTI_STEP.search(text or "")),
        "terminal": bool(_TERMINAL_SIGNAL.search(text or "")),
        "vision": bool(_VISION_SIGNAL.search(text or "")),
    }


_QUESTION_OPENERS = re.compile(
    r"^\s*(?:in one sentence|briefly|in short|who|what|when|where|why|how|"
    r"which|is|are|was|were|do|does|did|can you tell|could you tell|"
    r"explain|tell me about|summari[sz]e|define)\b",
    re.IGNORECASE,
)

_EXPLICIT_FILE = re.compile(
    r"[\w./~-]+\.(?:txt|md|markdown|pdf|json|csv|tsv|py|js|ts|swift|sql|log|"
    r"png|jpe?g|heic|gif|webp|docx?|xlsx?|pptx?|zip|tar|gz|html?)\b",
    re.IGNORECASE,
)


def looks_like_pure_question(text: str) -> bool:
    """True for a knowledge question that must never hit the file parser.

    The desk parser is stateful: a stale "last file" turned "in one sentence,
    who is Ada Lovelace" into an append to an unrelated JSON file. A question
    with no local anchor, no explicit filename, and no personal reference
    belongs to the main brain and needs no owner-memory lookup.
    """
    raw = (text or "").strip()
    if not raw or not _QUESTION_OPENERS.search(raw):
        return False
    if _LOCAL_ANCHOR.search(raw) or _EXPLICIT_FILE.search(raw):
        return False
    return not (_TERMINAL_SIGNAL.search(raw) or _VISION_SIGNAL.search(raw))


def needs_full_agent(text: str) -> bool:
    """True for compound machine work that a single capability cannot finish.

    The legacy ``computer`` tool is good at one thing at a time (open an app,
    look at the screen). A request with two panes, a resume command, a
    screenshot to read first, or several steps needs the agent loop with real
    terminal and vision tools. This gate keeps single-step asks on the fast
    path and sends compounds to the agent.
    """
    raw = (text or "").strip()
    if not raw or not looks_like_local_work(raw):
        return False
    if _MULTI_STEP.search(raw):
        return True
    verbs = {match.group(0).lower() for match in _ACTION_VERBS.finditer(raw)}
    if len(verbs) >= 2 and (_TERMINAL_SIGNAL.search(raw) or _LOCAL_NOUNS.search(raw)):
        # "open a Ghostty window and run echo hello" — two real actions in one
        # request, even without the word "then".
        return True
    patterns = (
        r"\b(?:two|three|four|five|\d+)\s+(?:panes?|tabs?|windows?|sessions?)\b",
        r"\b(?:one|a)\s+pane\s+(?:per|for)\b",
        r"\b(?:resume|attach|reconnect)\b[^.]{0,80}\b(?:session|sessions|work|tmux)\b",
        r"\brun\b[^.]{0,120}\b(?:in|inside|on)\b[^.]{0,40}\b(?:terminal|pane|window|session)\b",
        r"\b(?:read|look at|see|inspect)\b[^.]{0,40}\b(?:screenshot|screen|image)\b",
        r"\b(?:take|capture)\b[^.]{0,40}\b(?:screenshot|screen|image)\b",
        r"\b(?:screenshot|screen|image)\b[^.]{0,60}\b(?:run|command|commands|session|sessions)\b",
    )
    return any(re.search(pattern, raw, re.IGNORECASE) for pattern in patterns)



__all__ = [
    "describe_route",
    "looks_like_local_work",
    "looks_like_pure_question",
    "needs_full_agent",
]
