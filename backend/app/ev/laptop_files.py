"""Owner laptop files — read, write, edit, list, and open.

The realtime voice model is not the editor. It states a file goal; this
module (and MacControlService.file_op) touches the disk. Intelligent edits
use Muse Spark when it is the configured brain. OpenAI Luna then DeepSeek
remain legacy-only. The sandbox jail is a different path and never
substitutes for the owner's Desktop/Documents/Downloads.
"""

from __future__ import annotations

import json
import logging
import os
import re
import subprocess
import time
from pathlib import Path
from typing import Any

from app.config import settings

logger = logging.getLogger("ev.laptop_files")

MAX_FILE_BYTES = 256 * 1024
MAX_LIST = 40
TEXT_EXTENSIONS = frozenset(
    {
        ".txt",
        ".md",
        ".markdown",
        ".json",
        ".csv",
        ".py",
        ".swift",
        ".js",
        ".ts",
        ".tsx",
        ".jsx",
        ".html",
        ".css",
        ".xml",
        ".yml",
        ".yaml",
        ".toml",
        ".ini",
        ".log",
        ".sh",
        ".rs",
        ".go",
        ".rb",
        ".java",
        ".c",
        ".h",
        ".cpp",
        ".hpp",
        ".rtf",
        ".text",
    }
)
FOLDER_ALIASES = {
    "desktop": "Desktop",
    "desk": "Desktop",
    "documents": "Documents",
    "docs": "Documents",
    "downloads": "Downloads",
    "download": "Downloads",
    "movies": "Movies",
    "pictures": "Pictures",
    "photos": "Pictures",
    "music": "Music",
    "code": "Code",
    "icloud": "Library/Mobile Documents/com~apple~CloudDocs",
    "icloud drive": "Library/Mobile Documents/com~apple~CloudDocs",
}
DENY_PARTS = frozenset(
    {
        ".ssh",
        ".aws",
        ".gnupg",
        ".kube",
        ".gnupg2",
        "keychains",
        "keychain",
    }
)
DENY_NAMES = frozenset(
    {
        ".env",
        ".env.local",
        ".env.production",
        "id_rsa",
        "id_ed25519",
        "id_ecdsa",
        "credentials.json",
        "credentials.csv",
        "master.key",
        "api.env",
    }
)
DENY_SUFFIXES = (".pem", ".p12", ".pfx", ".key", ".kdbx")
DENY_SUBSTRINGS = (
    "/library/application support/ev",
    "/.ev/secrets",
    "/.git/",
)
APP_STEAL = re.compile(
    r"\b(?:textedit|safari|chrome|spotify|slack|notes|music|finder|calculator)\b",
    re.I,
)
FILE_CUE = re.compile(
    r"(?:"
    r"\b(?:desktop|downloads?)\b|"
    r"\bdocuments?\s+folder\b|"
    r"\b(?:in|into|to|on)\s+(?:my\s+|the\s+)?documents\b|"
    r"on\s+(?:my\s+|the\s+)?(?:desk(?:top)?|desktop)|"
    r"files? (?:on|in|from) (?:my )?(?:the )?(?:desk(?:top)?|desktop|documents|downloads|icloud)|"
    r"\bicloud drive\b|"
    r"\blocal files?\b|"
    r"~/|"
    r"\.(?:txt|md|markdown|json|csv|py|swift|js|ts|html|htm|css|yml|yaml|log|sh|pdf|png|jpg|jpeg)\b"
    r")",
    re.I,
)
CALLED_RE = re.compile(
    r"\b(?:called|named|titled)\s+(?:as\s+)?[\"']?([A-Za-z0-9][\w.+\-]{0,80}?\.[A-Za-z0-9]{1,8})[\"']?",
    re.I,
)
BARE_NAME_RE = re.compile(
    r"\b(?:file|document)\s+[\"']?([A-Za-z0-9][\w .+\-]{0,80}?\.[A-Za-z0-9]{1,8})[\"']?",
    re.I,
)
EXT_NAME_RE = re.compile(
    r"\b([A-Za-z0-9][\w.+\-]{0,80}?\.(?:txt|md|markdown|json|csv|py|swift|js|ts|html|htm|css|yml|yaml|log|sh|pdf|png|jpg|jpeg))\b",
    re.I,
)
SAYS_RE = re.compile(
    r"(?:that\s+says|saying|that\s+reads|with(?:\s+the)?(?:\s+text|\s+words|\s+contents?)?|containing|that\s+contains)\s+(.+)$",
    re.I,
)
NOTE_COLON_RE = re.compile(
    r"\b(?:note|file|text|reminder)\b[^:]{0,48}:\s*(.+)$",
    re.I,
)
REPLACE_RE = re.compile(
    r"\b(?:change|replace|swap)\s+[\"']?(.+?)[\"']?\s+(?:to|with|into)\s+[\"']?(.+?)[\"']?\s*$",
    re.I,
)
FOLDER_RE = re.compile(
    r"\b(?:on|in|into|to|inside)\s+(?:my\s+|the\s+)?(icloud(?:\s+drive)?|desk(?:top)?|desktop|documents|docs|downloads?|movies|pictures|photos|code)\b",
    re.I,
)
DEST_FOLDER_RE = re.compile(
    r"\b(?:to|into)\s+(?:my\s+|the\s+)?(icloud(?:\s+drive)?|desk(?:top)?|desktop|documents|docs|downloads?|movies|pictures|photos|code)\b",
    re.I,
)
RENAME_TO_RE = re.compile(
    r"\brename\b.{0,80}?\b(?:to|as)\s+[\"']?([A-Za-z0-9][\w.+\-]{0,80})[\"']?",
    re.I,
)
FILE_FOLLOWUP_RE = re.compile(
    r"(?:(?:can|could)\s+you\s+|please\s+)?"
    r"\b(?:"
    r"read\s+(?:it|that|this)|"
    r"open\s+(?:it|that|this)|"
    r"what(?:'s|s| is) (?:in|inside) (?:it|that|this)|"
    r"what does (?:it|that|this) say|"
    r"show\s+(?:it|that|this)|"
    r"edit\s+(?:it|that|this)|"
    r"(?:change|update|modify)\s+(?:it|that|this)|"
    r"add(?:\s+\S+){0,8}\s+to\s+(?:it|that|this)|"
    r"append\s+(?:to\s+)?(?:it|that|this)|"
    r"rename\s+(?:it|that|this)|"
    r"(?:copy|duplicate|move)\s+(?:it|that|this)|"
    r"(?:run|execute)\s+(?:it|that|this)"
    r")\b",
    re.I,
)
ADD_TO_DEIXIS_RE = re.compile(
    r"\b(?:add|append)\s+(?:the (?:text|line|words?)\s+)?\S.+\s+to\s+(?:it|that|this)\b",
    re.I,
)
KEEP_ONLY_RE = re.compile(
    r"(?:"
    r"(?:delete|remove|clear|erase|wipe)\s+(?:everything|all|the rest).{0,80}?"
    r"(?:keep|leave|keeping|leaving)\s+(?:just\s+|only\s+)?(.+)$"
    r"|"
    r"(?:delete|remove|clear|erase|wipe)\s+(?:everything|all)\s+except\s+(.+)$"
    r"|"
    r"(?:keep|leave|keeping|leaving)\s+(?:just\s+|only\s+)(.+)$"
    r"|"
    r"just\s+(?:keep|leave|keeping|leaving)\s+(.+)$"
    r")",
    re.I,
)
FILE_DELETE_RE = re.compile(
    r"\b(?:delete|remove|trash|bin)\s+(?:the\s+)?(?:file|note|document)(?:\s+itself)?\b|"
    r"\b(?:delete|remove|trash)\s+(?:it|that|this)\s*$|"
    r"\bthrow\s+(?:it|that|this)\s+away\b|"
    r"\b(?:put|move)\s+(?:it|that|this)\s+in\s+(?:the\s+)?trash\b",
    re.I,
)
CLEAR_RE = re.compile(
    r"\b(?:clear|empty|wipe|blank)\s+(?:it|that|this|out|the file|the note|everything)\b|"
    r"\b(?:delete|remove)\s+(?:everything|all)(?:\s+from\s+(?:it|that|this|the file))?\s*$",
    re.I,
)
DROP_ITEM_RE = re.compile(
    r"\b(?:delete|remove|erase|cut|take out|strike)\s+(?:the\s+|a\s+|an\s+)?(.+)$",
    re.I,
)
REWRITE_TO_RE = re.compile(
    r"\b(?:rewrite it|replace everything|make it say|change it to|replace it with|"
    r"make it\s+(?:just|only)\s+say)\s+(.+)$",
    re.I,
)
RUN_FILE_RE = re.compile(
    r"\b(?:run|execute)\s+(?:it|that|this|the file|the script)\b",
    re.I,
)
CONTENT_MUTATE_RE = re.compile(
    r"\b(?:"
    r"delete everything|remove everything|"
    r"keep (?:just |only )|just keep (?:the |only )|"
    r"keep(?:ing)? (?:just |only )|leave(?:ing)? (?:just |only )|"
    r"clear (?:it|that|this|the file)|wipe (?:it|that|this)|"
    r"empty (?:it|the file|the note)|rewrite (?:it|that|this)|replace everything"
    r")\b",
    re.I,
)
DROP_NOT_FILE_RE = re.compile(
    r"\b(?:from my|from the|calendar|reminder|email|message|inbox|alarm|event)\b",
    re.I,
)
JUST_KEEP_CHAT_RE = re.compile(
    r"\bjust keep (?:going|doing|trying|talking|it up)\b",
    re.I,
)
RUNNABLE_SUFFIXES = {
    ".py": ("python3",),
    ".sh": ("bash",),
    ".js": ("node",),
    ".rb": ("ruby",),
}
NOTE_CREATE_RE = re.compile(
    r"\b(?:drop|leave|create|write|jot|make|put|add)\s+(?:down\s+)?(?:a\s+|the\s+|new\s+)?note\b",
    re.I,
)
KIND_RE = re.compile(
    r"\b(pdfs?|screenshots?|images?|photos?|html)\b",
    re.I,
)
DOC_NOUN_RE = re.compile(
    r"\b(?:resumes?|cvs?|pdfs?|notes?|documents?|html|screenshots?|receipts?|invoices?|letters?)\b",
    re.I,
)
FINDABLE_DOC_RE = re.compile(
    r"\b(?:resumes?|cvs?|curriculum vitae|pdfs?|receipts?|invoices?|letters?)\b",
    re.I,
)
WEB_STEAL_RE = re.compile(
    r"\b(?:on the web|online|google|the internet|youtube|safari|chrome)\b",
    re.I,
)
FIND_AND_OPEN_RE = re.compile(
    r"\b(?:find|search(?:\s+for)?|locate|look for|where'?s|where is)\s+"
    r"(?:the\s+|my\s+|a\s+)?(.+?)\s+and\s+open(?:\s+it|\s+that|\s+this)?\b",
    re.I,
)
CONFIRMATION_RE = re.compile(
    r"(?:^\s*\(system confirmation\b)"
    r"|(?:^\s*\(life record\b)"
    r"|(?:speak this to the owner now)"
    r"|(?:answer the owner from this now)"
    r"|(?:^(?:wrote|opened|updated|added that)\b)"
    r"|(?:^on desktop:)"
    r"|(?:^the file says:)",
    re.I,
)
FILE_URL_SUFFIXES = frozenset(
    {
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
    }
)
PLAY_MEDIA_RE = re.compile(r"\.(?:mov|mp4|m4v|mp3|aac|wav)\b", re.I)


def laptop_files_allowed() -> bool:
    override = str(getattr(settings, "laptop_files_root", None) or "").strip()
    if override:
        return True
    env = str(getattr(settings, "environment", "") or "").strip().lower()
    if env == "test":
        return True
    return bool(getattr(settings, "laptop_files", False))


def allowed_roots() -> list[Path]:
    override = str(getattr(settings, "laptop_files_root", None) or "").strip()
    if override:
        root = Path(override).expanduser().resolve()
        root.mkdir(parents=True, exist_ok=True)
        return [root]
    home = Path.home()
    roots: list[Path] = []
    for name in ("Desktop", "Documents", "Downloads", "Movies", "Music", "Pictures", "Code"):
        path = (home / name).resolve()
        if path.exists() and path.is_dir():
            roots.append(path)
        elif name in {"Desktop", "Documents", "Downloads"}:
            path.mkdir(parents=True, exist_ok=True)
            roots.append(path.resolve())
    icloud = (home / "Library/Mobile Documents/com~apple~CloudDocs").resolve()
    if icloud.exists() and icloud.is_dir():
        roots.append(icloud)
    return roots


DEFAULT_WRITE_NAME = "evie-note.txt"
_SKIP_DIR_NAMES = frozenset(
    {
        ".git",
        "node_modules",
        "__pycache__",
        ".venv",
        "venv",
        "Library",
        ".build",
        "build",
        "dist",
        "DerivedData",
        ".swiftpm",
        "Pods",
        ".next",
        "target",
        ".cache",
        ".tox",
        "Carthage",
        ".gradle",
    }
)
_SEARCH_PENALTY = (
    "flagship",
    "draft",
    "old",
    "copy",
    "backup",
    "template",
    "sample",
    "placeholder",
)
FILE_VERBS = re.compile(
    r"\b(?:"
    r"read|write|edit|create|save|make|put|jot|open|list|append|update|"
    r"change|modify|what's in|whats in|what is in|what's on|whats on|what is on|"
    r"show(?:\s+me)?|all the files|the files|"
    r"look at (?:the |my )files|"
    r"drop|leave|dump|find|search|locate|where's|where is|look for|"
    r"pull up|peek|check|grab|rename|copy|duplicate|move|"
    r"delete|remove|trash|clear|erase|wipe|run|execute|"
    r"recent|latest|newest"
    r")\b",
    re.I,
)
CALLED_BARE_RE = re.compile(
    r"\b(?:called|named|titled)\s+(?:as\s+)?[\"']?([A-Za-z][\w.+\-]{0,60}?)[\"']?"
    r"(?=\s+(?:on|in|inside|that|saying|with|to|spelled)|[,.]|$)",
    re.I,
)
NAME_FILE_RE = re.compile(
    r"\ba\s+(?!new\b|text\b|local\b|empty\b|small\b)([A-Za-z][\w.+\-]{0,40})\s+files?\b",
    re.I,
)
WRITE_ON_RE = re.compile(
    r"\b(?:write|create|save|make|put|jot|edit|read|open)\s+"
    r"(?!a\b|an\b|the\b|my\b|new\b|file\b|files\b)"
    r"[\"']?([A-Za-z][\w.+\-]{0,40})[\"']?\s+(?:on|in)\b",
    re.I,
)


def normalize_file_utterance(text: str) -> str:
    raw = (text or "").strip()
    raw = re.sub(r"\bdesk\s+top\b", "desktop", raw, flags=re.I)
    raw = re.sub(
        r"\bon\s+(?:my\s+|the\s+)?desk\b(?!\s*top)",
        "on desktop",
        raw,
        flags=re.I,
    )
    raw = re.sub(r"\bdownload folder\b", "downloads", raw, flags=re.I)
    raw = re.sub(r"\bdot\s+(txt|md|json|csv|py|html|pdf)\b", r".\1", raw, flags=re.I)
    return raw


def is_system_confirmation(text: str) -> bool:
    raw = (text or "").strip()
    if not raw:
        return False
    return bool(CONFIRMATION_RE.search(raw))


def _has_file_referent(last_path: str | None = None) -> bool:
    if str(last_path or "").strip():
        return True
    from app.ev.desk_scene import referent_file_path, scene_is_live

    return scene_is_live() or referent_file_path() is not None


_APPEND_ADD_RE = re.compile(
    r"\b(?:add|append|include|put|jot(?:\s+down)?|stick|throw|toss|plus)\b",
    re.I,
)
_APPEND_ALSO_RE = re.compile(r"^(?:and\s+)?(?:also|plus)\b", re.I)
_APPEND_QUANT_RE = re.compile(
    r"\b(?:\d+|two|three|four|five|six|seven|eight|nine|ten|"
    r"a couple(?: of)?|a few|some)\s+(?:more\s+)?(?:things?|items?|lines?|entries|bits)\b",
    re.I,
)
_APPEND_POLITE_RE = re.compile(
    r"^(?:please\s+|can you\s+|could you\s+|would you\s+)?",
    re.I,
)
_APPEND_DEST_TAIL_RE = re.compile(
    r"(?:"
    r"\s+to\s+(?:it|that|this|the (?:file|note|list))"
    r"|\s+on\s+(?:there|it|(?:the |my )?(?:list|note|file))"
    r"|\s+as well|\s+too|\s+underneath|\s+below|\s+under (?:it|that)"
    r")+$",
    re.I,
)
_APPEND_FRAME_RE = re.compile(
    r"^(?:add|append|include|put|jot(?:\s+down)?|stick|throw|toss|plus|also(?:\s+add)?)\s+"
    r"(?:(?:\d+|two|three|four|five|six|seven|eight|nine|ten|a couple(?: of)?|a few|some)\s+)?"
    r"(?:more\s+)?"
    r"(?:things?|items?|lines?|entries|bits)?"
    r"[,:]?\s*"
    r"(?:the (?:text|line|words?)\s+)?"
    r"(.+)$",
    re.I,
)
_APPEND_SKIP_ITEMS = frozenset(
    {
        "things",
        "thing",
        "items",
        "item",
        "more",
        "it",
        "that",
        "this",
        "them",
        "stuff",
        "some",
        "few",
        "couple",
        "lines",
        "line",
        "entries",
        "entry",
        "bits",
        "bit",
        "note",
        "file",
        "list",
    }
)
_APPEND_FOREIGN_DEST_RE = re.compile(
    r"\b(?:calendar|reminder|email|message|inbox|alarm|event|timer)\b",
    re.I,
)


def _is_new_file_write(raw: str) -> bool:
    if NOTE_CREATE_RE.search(raw) and not re.search(
        r"\bin\s+(?:the\s+)?notes?\s+app\b", raw, re.I
    ):
        return True
    if FILE_CUE.search(raw) and re.search(
        r"\b(?:write|create|save|make|drop|leave|dump)\b", raw, re.I
    ):
        return True
    return False


def _split_list_items(payload: str) -> list[str]:
    blob = _spoken_file_body(payload)
    if not blob:
        return []
    blob = re.sub(r"\s+(?:and|then|&)\s+", ",", blob, flags=re.I)
    out: list[str] = []
    for part in blob.split(","):
        item = part.strip(" .,'\"")
        if not item or len(item) > 80:
            continue
        if item.lower() in _APPEND_SKIP_ITEMS:
            continue
        if re.search(r"\b(?:i|i'm|im|we|you|because|should)\b", item, re.I):
            continue
        out.append(item)
    return out


def extract_append_items(text: str) -> list[str]:
    """Pull addable items from a follow-up. Meaning, not a canned phrase."""

    raw = (text or "").strip()
    if not raw:
        return []
    if _is_new_file_write(raw) or _APPEND_FOREIGN_DEST_RE.search(raw):
        return []
    from app.ev.desk_acts import UNDO_RE

    if UNDO_RE.search(raw):
        return []
    if re.match(r"^\s*(?:i|i'm|im|we|you)\b", raw, re.I):
        return []
    from app.ev.desk_scene import BELONGS_RE, LAND_NAME, OTHER_RE, PACKET_PUT_RE

    if BELONGS_RE.search(raw) or OTHER_RE.search(raw):
        return []
    if PACKET_PUT_RE.search(raw) and (
        "packet" in raw.lower() or "visa" in raw.lower() or LAND_NAME.search(raw)
    ):
        return []
    has_intent = bool(
        _APPEND_ADD_RE.search(raw) or _APPEND_ALSO_RE.search(raw) or _APPEND_QUANT_RE.search(raw)
    )
    if not has_intent:
        from app.ev.desk_presence import extract_bare_list_items

        return extract_bare_list_items(raw)
    body = _APPEND_POLITE_RE.sub("", raw).strip()
    body = _APPEND_DEST_TAIL_RE.sub("", body).strip()
    match = _APPEND_FRAME_RE.search(body)
    payload = match.group(1).strip() if match else ""
    if not payload and _APPEND_ALSO_RE.search(body):
        payload = re.sub(r"^(?:and\s+)?(?:also|plus)(?:\s+add)?\s+", "", body, flags=re.I)
    if not payload and _APPEND_QUANT_RE.search(body):
        payload = _APPEND_QUANT_RE.sub("", body, count=1).strip(" :,")
    items = _split_list_items(payload)
    return items


def parse_referent_append(
    text: str, last_path: str | None = None
) -> dict[str, Any] | None:
    """Append extracted items to the live note/file, if this turn means that."""

    raw = normalize_file_utterance(text)
    if not raw or is_system_confirmation(raw):
        return None
    items = extract_append_items(raw)
    if not items:
        return None
    from app.ev.desk_acts import maybe_ambiguous_append

    asked = maybe_ambiguous_append(raw, items)
    if asked is not None:
        return asked
    from app.ev.desk_scene import last_note_object, referent_file_path

    found = referent_file_path(last_path)
    note = last_note_object()
    note_path = None
    if note is not None:
        from app.ev.desk_scene import _live_path

        note_path = _live_path(note)
    if found is not None and found.suffix.lower() not in TEXT_EXTENSIONS and note_path is not None:
        found = note_path
    elif found is None:
        found = note_path
    if found is None:
        return None
    return {
        "action": "append",
        "path": str(found),
        "query": found.name,
        "content": "\n".join(items),
        "items": items,
        "receipt": "append",
        "goal": raw,
    }


def _drop_line_followup(raw: str) -> bool:
    if DROP_NOT_FILE_RE.search(raw):
        return False
    match = DROP_ITEM_RE.search(raw)
    if not match:
        return False
    from app.ev.desk_meaning import strip_reason_clause

    body = strip_reason_clause(re.sub(r"[.!?]+$", "", (match.group(1) or "").strip(" \"'")))
    if not body or len(body) > 80:
        return False
    words = body.split()
    if not words or len(words) > 8:
        return False
    if words[0].lower() in {"i", "i'm", "im", "we", "you", "he", "she", "they"}:
        return False
    return bool(re.match(r"^[A-Za-z0-9][\w'+\-]*(?:\s+[A-Za-z0-9][\w'+\-]*){0,7}$", body))


def looks_like_file_followup(text: str, last_path: str | None = None) -> bool:
    raw = normalize_file_utterance(text)
    if not raw or is_system_confirmation(raw):
        return False
    # "Add eggs to it" is a file job even before ComputerState last_path is
    # wired. Sending it to inspect_ui/click is the Talk retry loop.
    if ADD_TO_DEIXIS_RE.search(raw):
        return True
    if parse_referent_append(raw, last_path) is not None:
        return True
    from app.ev.desk_acts import looks_like_desk_file_act

    if looks_like_desk_file_act(raw, last_path):
        return True
    if not _has_file_referent(last_path):
        return False
    if FILE_FOLLOWUP_RE.search(raw) or RUN_FILE_RE.search(raw):
        return True
    if CONTENT_MUTATE_RE.search(raw) or CLEAR_RE.search(raw) or FILE_DELETE_RE.search(raw):
        return True
    if KEEP_ONLY_RE.search(raw) and not JUST_KEEP_CHAT_RE.search(raw):
        return True
    if _drop_line_followup(raw):
        return True
    if REPLACE_RE.search(raw):
        from app.ev.luna_code import looks_like_code_request

        return not looks_like_code_request(raw)
    return False


def looks_like_file_task(text: str, last_path: str | None = None) -> bool:
    raw = normalize_file_utterance(text)
    if not raw or is_system_confirmation(raw):
        return False
    if looks_like_file_followup(raw, last_path=last_path):
        return True
    from app.ev.desk_acts import looks_like_desk_file_act
    from app.ev.desk_meaning import looks_like_desk_job, spark_desk_candidate

    if (
        looks_like_desk_file_act(raw, last_path)
        or looks_like_desk_job(raw)
        or spark_desk_candidate(raw)
    ):
        return True
    from app.ev.luna_code import looks_like_code_request

    if looks_like_code_request(raw):
        return False
    if NOTE_CREATE_RE.search(raw) and not re.search(
        r"\bin\s+(?:the\s+)?notes?\s+app\b", raw, re.I
    ):
        return True
    from app.ev.desk_names import parse_bind_goal, resolve_spoken_file
    from app.ev.desk_scene import looks_like_scene_turn, resolve_spoken_object

    if looks_like_scene_turn(raw):
        return True
    if parse_bind_goal(raw, last_path):
        return True
    named = resolve_spoken_file(raw) or (
        resolve_spoken_object(raw) is not None and raw
    )
    if named and (
        re.search(
            r"\b(?:add|append|what's on|what is on|what's in|what is in|"
            r"read|open|pull up|packet)\b",
            raw,
            re.I,
        )
        or FILE_FOLLOWUP_RE.search(raw)
        or KEEP_ONLY_RE.search(raw)
        or FILE_DELETE_RE.search(raw)
        or CONTENT_MUTATE_RE.search(raw)
        or _drop_line_followup(raw)
    ):
        return True
    if WEB_STEAL_RE.search(raw) and not FILE_CUE.search(raw):
        return False
    finding = bool(
        re.search(
            r"\b(?:find|search|locate|look for|where'?s|where is)\b",
            raw,
            re.I,
        )
    )
    opening = bool(re.search(r"\b(?:open|read|pull up)\b", raw, re.I))
    if finding and (FINDABLE_DOC_RE.search(raw) or DOC_NOUN_RE.search(raw)):
        return True
    if opening and FINDABLE_DOC_RE.search(raw):
        return True
    if not FILE_CUE.search(raw):
        return False
    if PLAY_MEDIA_RE.search(raw) and re.search(r"\bplay\b", raw, re.I):
        return False
    if APP_STEAL.search(raw) and not re.search(r"\bfiles?\b", raw, re.I) and not EXT_NAME_RE.search(raw):
        if not (
            FILE_VERBS.search(raw)
            and re.search(r"\b(?:desktop|documents|downloads)\b", raw, re.I)
        ):
            return False
    return bool(FILE_VERBS.search(raw) or EXT_NAME_RE.search(raw) or KIND_RE.search(raw))


def resolve_file_computer_goal(
    text: str,
    target_app: str | None = None,
    last_path: str | None = None,
) -> tuple[str, dict[str, Any]] | None:
    parsed = parse_file_goal(text, target_app=target_app, last_path=last_path)
    if parsed is None:
        return None
    return "file_op", parsed


def parse_file_goal(
    text: str,
    target_app: str | None = None,
    last_path: str | None = None,
) -> dict[str, Any] | None:
    raw = normalize_file_utterance(text)
    if not raw or not looks_like_file_task(raw, last_path=last_path):
        return None
    if not last_path and (
        FILE_FOLLOWUP_RE.search(raw)
        or CONTENT_MUTATE_RE.search(raw)
        or KEEP_ONLY_RE.search(raw)
        or RUN_FILE_RE.search(raw)
        or FILE_DELETE_RE.search(raw)
        or _drop_line_followup(raw)
    ):
        from app.ev.desk_scene import referent_file_path

        found = referent_file_path()
        if found is not None:
            last_path = str(found)
    lowered = raw.lower()
    folder = _folder_from_text(raw)
    dest_folder = _dest_folder_from_text(raw)
    name = _filename_from_text(raw)
    kind = _kind_from_text(raw)
    recent = bool(re.search(r"\b(?:recent|latest|newest)\b", lowered))
    find_open = FIND_AND_OPEN_RE.search(raw)
    if find_open:
        needle = _search_needle(find_open.group(1)) or _search_needle(raw)
        if needle:
            from app.ev.desk_names import resolve_alias
            from app.ev.desk_scene import remember_file, resolve_alias_object

            named_path = resolve_alias(needle)
            named_obj = resolve_alias_object(needle)
            if named_path is not None:
                remember_file(named_path, source="open", query=needle)
            return {
                "action": "open",
                "path": str(named_path) if named_path else (folder or ""),
                "query": needle,
                "kind": kind,
                "goal": raw,
                "packet": (named_obj.get("aliases") or [needle])[0]
                if named_obj is not None and named_obj.get("kind") == "packet"
                else "",
            }
    from app.ev.desk_scene import parse_scene_goal

    scene_goal = parse_scene_goal(raw, last_path)
    if scene_goal is not None:
        return scene_goal
    from app.ev.desk_names import ADD_TO_NAMED_RE, parse_bind_goal, resolve_spoken_file

    bind = parse_bind_goal(raw, last_path)
    if bind:
        return bind
    named = resolve_spoken_file(raw)
    add_named = ADD_TO_NAMED_RE.search(raw)
    if named is not None and add_named:
        added = _spoken_file_body(add_named.group(1))
        return {
            "action": "append",
            "path": str(named[0]),
            "query": named[0].name,
            "content": added,
            "items": _split_list_items(added),
            "instruction": raw,
            "receipt": "append",
            "goal": raw,
        }
    if named is not None and re.search(
        r"\b(?:what's on|what is on|what's in|what is in|read|open|pull up|grab)\b",
        lowered,
    ):
        action = (
            "open"
            if re.search(r"\b(?:open|pull up|grab)\b", lowered)
            and not re.search(r"\b(?:what's|what is|read)\b", lowered)
            else "read"
        )
        return {
            "action": action,
            "path": str(named[0]),
            "query": named[1],
            "goal": raw,
        }
    from app.ev.desk_acts import parse_desk_file_goal

    desk_goal = parse_desk_file_goal(raw, last_path)
    if desk_goal is not None:
        return desk_goal
    appended = parse_referent_append(raw, last_path)
    if appended is not None and not _is_new_file_write(raw):
        return appended
    if last_path and not name and (
        FILE_FOLLOWUP_RE.search(raw)
        or CONTENT_MUTATE_RE.search(raw)
        or KEEP_ONLY_RE.search(raw)
        or RUN_FILE_RE.search(raw)
        or FILE_DELETE_RE.search(raw)
        or _drop_line_followup(raw)
    ):
        last = Path(last_path)
        if last.name and last.name != ".":
            name = last.name
        if not folder:
            folder = str(last.parent)
    content, instruction = _content_and_instruction(raw, name)
    if not name and folder and re.search(
        r"\b(?:write|create|save|make|put|jot|drop|leave|dump|add)\b", lowered
    ) and not kind and not re.search(
        r"\b(?:list|what's on|what is on|show files|files on|find|search|rename|copy|move)\b",
        lowered,
    ):
        name = DEFAULT_WRITE_NAME
    if NOTE_CREATE_RE.search(raw) and not re.search(
        r"\bin\s+(?:the\s+)?notes?\s+app\b", raw, re.I
    ):
        if not folder:
            folder = _alias_folder("desktop")
        if not name:
            name = DEFAULT_WRITE_NAME
    name = _with_text_suffix(name) if name and not kind else name
    if re.search(r"\brename\b", lowered) and (name or last_path):
        dest_name = ""
        match = RENAME_TO_RE.search(raw)
        if match:
            dest_name = _with_text_suffix(match.group(1).strip(" \"'"))
        if dest_name:
            return {
                "action": "rename",
                "path": _join_hint(folder, name),
                "query": name or "",
                "dest": str(Path(folder) / dest_name) if folder else dest_name,
                "goal": raw,
            }
    if re.search(r"\b(?:copy|duplicate)\b", lowered) and (name or last_path):
        target_folder = dest_folder or folder
        if not target_folder and last_path:
            target_folder = str(Path(last_path).parent)
        return {
            "action": "copy",
            "path": _join_hint(folder, name) if not last_path or folder else last_path,
            "query": name or "",
            "dest": target_folder,
            "goal": raw,
        }
    if re.search(r"\bmove\b", lowered) and (name or last_path):
        return {
            "action": "move",
            "path": _join_hint(folder, name) if not last_path or folder else last_path,
            "query": name or "",
            "dest": dest_folder,
            "goal": raw,
        }
    if FILE_DELETE_RE.search(raw) and not KEEP_ONLY_RE.search(raw) and (name or folder or last_path):
        return {
            "action": "delete",
            "path": _join_hint(folder, name) if name or folder else (last_path or ""),
            "query": name or "",
            "goal": raw,
        }
    if RUN_FILE_RE.search(raw) and (name or last_path):
        return {
            "action": "run",
            "path": _join_hint(folder, name) if name or folder else (last_path or ""),
            "query": name or "",
            "goal": raw,
        }
    wants_list = bool(
        re.search(
            r"\b(?:list|what's on|what is on|show files|files on|files in|all the files|the files|"
            r"anything on|check(?:\s+what's)?)\b",
            lowered,
        )
        or (recent and folder and not name and not re.search(r"\b(?:write|read|open|edit)\b", lowered))
        or (
            bool(re.search(r"\bshow(?:\s+me)?\b", lowered))
            and (kind or re.search(r"\bfiles?\b", lowered) or (folder and not name))
        )
        or bool(re.search(r"\b(?:pdfs|screenshots|images|photos|text files|txt files)\b", lowered))
        and folder
        and not re.search(r"\b(?:open|read|write|edit|find)\b", lowered)
    )
    if wants_list:
        if re.search(r"\b(?:find|search|look for|where's|where is|locate)\b", lowered) and (
            name or (kind and not re.search(r"\bfiles?\b", lowered))
        ):
            pass
        else:
            return {
                "action": "list",
                "path": folder or name or "",
                "query": kind or "",
                "recent": recent,
                "kind": kind,
                "goal": raw,
            }
    if re.search(r"\b(?:find|search|locate|look for|where's|where is)\b", lowered):
        needle = name or kind or _search_needle(raw)
        if needle or folder:
            return {
                "action": "search",
                "path": folder or "",
                "query": needle or kind or "",
                "kind": kind,
                "goal": raw,
            }
    wants_edit = bool(
        re.search(r"\b(?:edit|update|modify|rewrite|change|append|keep|clear|wipe|erase)\b", lowered)
        or KEEP_ONLY_RE.search(raw)
        or CLEAR_RE.search(raw)
        or DROP_ITEM_RE.search(raw)
        or REWRITE_TO_RE.search(raw)
        or (
            re.search(r"\badd\b", lowered)
            and re.search(r"\bto (?:it|that|this|the file)\b", lowered)
        )
        or (
            re.search(r"\b(?:delete|remove)\b", lowered)
            and not FILE_DELETE_RE.search(raw)
        )
    )
    if wants_edit and (name or folder or last_path):
        action = "append" if re.search(r"\b(?:append|add to)\b", lowered) else "edit"
        if re.search(r"\badd\b", lowered) and re.search(r"\bto (?:it|that|this)\b", lowered):
            action = "append"
        payload = {
            "action": action,
            "path": _join_hint(folder, name) if name or folder else (last_path or ""),
            "query": name or "",
            "instruction": raw,
            "goal": raw,
        }
        if action == "append" and content:
            payload["content"] = content
        elif action == "append":
            added = _append_body(raw)
            if added:
                payload["content"] = added
        return payload
    if re.search(r"\b(?:write|create|save|make|put|jot|drop|leave|dump|add)\b", lowered) and (
        name or folder
    ) and not (
        looks_like_file_followup(raw, last_path=last_path) and not _is_new_file_write(raw)
    ):
        payload = {
            "action": "write",
            "path": _join_hint(folder, name),
            "query": name or "",
            "content": content,
            "goal": raw,
        }
        if not content and _has_substance(instruction) and not _is_naming_only(raw):
            payload["instruction"] = instruction
        return payload
    if re.search(
        r"\b(?:read|what's in|what is in|contents? of|show me|peek|what does)\b",
        lowered,
    ):
        return {
            "action": "read",
            "path": _join_hint(folder, name) if name or folder else (last_path or ""),
            "query": name or kind or "",
            "kind": kind,
            "goal": raw,
        }
    findable = FINDABLE_DOC_RE.search(raw)
    if re.search(r"\b(?:open|pull up|grab)\b", lowered) and (
        name or folder or kind or last_path or findable
    ):
        if findable and not FILE_FOLLOWUP_RE.search(raw):
            needle = _search_needle(findable.group(0)) or _search_needle(raw)
            return {
                "action": "open",
                "path": folder or "",
                "query": needle or findable.group(0),
                "kind": kind,
                "goal": raw,
            }
        return {
            "action": "open",
            "path": _join_hint(folder, name) if name or folder else (last_path or ""),
            "query": name or kind or _search_needle(raw) or "",
            "kind": kind,
            "goal": raw,
        }
    if (name or kind) and folder:
        return {
            "action": "open",
            "path": _join_hint(folder, name) if name else folder,
            "query": name or kind or "",
            "kind": kind,
            "goal": raw,
        }
    del target_app
    return None


def _folder_from_text(text: str) -> str:
    override = str(getattr(settings, "laptop_files_root", None) or "").strip()
    source = re.search(
        r"\b(?:on|in|inside)\s+(?:my\s+|the\s+)?(icloud(?:\s+drive)?|desk(?:top)?|desktop|documents|docs|downloads?|movies|pictures|photos|code)\b",
        text,
        re.I,
    )
    alias = source.group(1).lower() if source else ""
    if not alias and not re.search(r"\b(?:copy|move|rename)\b", text, re.I):
        dest = DEST_FOLDER_RE.search(text)
        alias = dest.group(1).lower() if dest else ""
    if override:
        return override if alias or "desktop" in text.lower() or "folder" in text.lower() else ""
    return _alias_folder(alias)


def _dest_folder_from_text(text: str) -> str:
    match = DEST_FOLDER_RE.search(text)
    if not match:
        return ""
    return _alias_folder(match.group(1).lower())


def _alias_folder(alias: str) -> str:
    name = FOLDER_ALIASES.get((alias or "").lower(), "")
    if not name:
        if (alias or "").lower() in {"desk", "desktop"}:
            name = "Desktop"
    if not name:
        return ""
    override = str(getattr(settings, "laptop_files_root", None) or "").strip()
    if override:
        return override
    return str(Path.home() / name)


def _kind_from_text(text: str) -> str:
    match = KIND_RE.search(text or "")
    if not match:
        return ""
    token = match.group(1).lower()
    if token.startswith("pdf"):
        return "pdf"
    if token.startswith("screenshot"):
        return "screenshot"
    if token.startswith("image") or token.startswith("photo"):
        return "image"
    if token.startswith("html"):
        return "html"
    if "text" in token or "txt" in token:
        return "text"
    return ""


def _search_needle(text: str) -> str:
    raw = re.sub(
        r"\b(?:please|can you|could you|find|search(?:\s+for)?|locate|look for|"
        r"where'?s|where is|open(?:\s+it)?|and open(?:\s+it)?|"
        r"the|a|an|my|on|in|inside|desktop|documents|downloads|files?|"
        r"and|that|this|it|for me)\b",
        " ",
        text or "",
        flags=re.I,
    )
    return re.sub(r"\s+", " ", raw).strip(" .,'\"")[:80]


def _append_body(text: str) -> str:
    match = re.search(
        r"\b(?:append|add)\s+(?:the (?:text|line|words?)\s+)?[\"']?(.+?)[\"']?\s+to\s+(?:it|that|this|the file)\b",
        text or "",
        re.I,
    )
    if match:
        return _spoken_file_body(match.group(1))
    return ""


def _filename_from_text(text: str) -> str:
    ext = EXT_NAME_RE.search(text)
    if ext:
        return ext.group(1).strip(" \"'")
    for pattern in (CALLED_RE, BARE_NAME_RE, CALLED_BARE_RE, NAME_FILE_RE, WRITE_ON_RE):
        match = pattern.search(text)
        if match:
            name = match.group(1).strip(" \"'")
            name = re.sub(r"^(?:as|a)\s+", "", name, flags=re.I)
            return name
    return ""


def _with_text_suffix(name: str) -> str:
    raw = str(name or "").strip()
    if not raw:
        return ""
    path = Path(raw)
    if path.suffix:
        return raw
    return raw + ".txt"


def _strip_folder_tail(body: str) -> str:
    return re.sub(
        r"\s+(?:on|in|to|into)\s+(?:my\s+|the\s+)?(?:desktop|documents|downloads)\s*$",
        "",
        body,
        flags=re.I,
    )


def _spoken_file_body(text: str) -> str:
    body = _strip_folder_tail((text or "").strip(" \"'"))
    return re.sub(r"[.!?]+$", "", body).strip()


def _content_and_instruction(text: str, name: str) -> tuple[str, str]:
    says = SAYS_RE.search(text)
    if says:
        body = _spoken_file_body(says.group(1))
        return body[:MAX_FILE_BYTES], body[:MAX_FILE_BYTES]
    jot = re.search(r"\bjot(?:\s+down)?\s+(.+)$", text, re.I)
    if jot:
        body = _spoken_file_body(jot.group(1))
        return body[:MAX_FILE_BYTES], body[:MAX_FILE_BYTES]
    colon = NOTE_COLON_RE.search(text)
    if colon and not re.search(r"\b(?:named|called|titled|spelled)\b", text, re.I):
        body = _spoken_file_body(colon.group(1))
        if body:
            return body[:MAX_FILE_BYTES], body[:MAX_FILE_BYTES]
    put = re.search(
        r"\b(?:put|drop|leave)\s+(.+?)\s+on\s+(?:my\s+|the\s+)?desktop\b",
        text,
        re.I,
    )
    if put and " " in put.group(1).strip():
        body = _spoken_file_body(put.group(1))
        if body and not re.search(r"\b(?:file|note|text)\b", body, re.I):
            return body[:MAX_FILE_BYTES], body[:MAX_FILE_BYTES]
    stripped = text
    if name:
        stripped = re.sub(re.escape(name), " ", stripped, flags=re.I)
    stripped = re.sub(
        r"^(?:please\s+)?(?:write|create|save|make|put|jot|edit|update|modify|rewrite|change|read|open|list)\s+",
        "",
        stripped,
        flags=re.I,
    )
    stripped = re.sub(
        r"\b(?:a\s+)?(?:new\s+)?files?\b|\bcalled\b|\bnamed\b|"
        r"\bon (?:my |the )?desktop\b|\bin (?:my |the )?documents\b|"
        r"\bto (?:my |the )?(?:desktop|documents|downloads)\b",
        " ",
        stripped,
        flags=re.I,
    )
    stripped = re.sub(r"\s+", " ", stripped).strip(" .,'\"")
    return "", stripped[:MAX_FILE_BYTES]


def _has_substance(text: str) -> bool:
    leftover = re.sub(
        r"\b(?:please|write|create|save|make|put|jot|a|an|the|my|new|file|files|"
        r"on|in|to|into|desktop|documents|downloads|called|named|titled)\b",
        " ",
        text or "",
        flags=re.I,
    )
    leftover = re.sub(r"[^A-Za-z0-9]+", " ", leftover)
    return len(leftover.strip()) >= 8


def _is_naming_only(text: str) -> bool:
    raw = text or ""
    if SAYS_RE.search(raw):
        return False
    return bool(
        re.search(
            r"\b(?:named|called|titled|spelled|hyphen|full stop|dot txt)\b",
            raw,
            re.I,
        )
    )


def _join_hint(folder: str, name: str) -> str:
    if folder and name:
        return str(Path(folder) / name)
    return folder or name or ""


def path_denied(path: Path) -> str | None:
    try:
        resolved = path.expanduser().resolve()
    except OSError:
        return "invalid_path"
    lowered = str(resolved).replace("\\", "/").lower()
    for part in resolved.parts:
        if part.lower() in DENY_PARTS or part.lower() in DENY_NAMES:
            return "path_denied"
    if resolved.name.lower() in DENY_NAMES:
        return "path_denied"
    if any(lowered.endswith(suffix) for suffix in DENY_SUFFIXES):
        return "path_denied"
    if any(token in lowered for token in DENY_SUBSTRINGS):
        return "path_denied"
    roots = allowed_roots()
    if not any(_is_inside(resolved, root) for root in roots):
        return "path_outside_allowed"
    return None


def _is_inside(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


def _needle_aliases(needle: str) -> tuple[str, ...]:
    token = re.sub(r"\s+", " ", (needle or "").strip().lower())
    if not token:
        return ()
    if token in {"resume", "resumes", "cv", "cvs", "curriculum vitae"}:
        return ("resume", "cv")
    if token.endswith("s") and len(token) > 3:
        return (token, token[:-1])
    return (token,)


def _name_matches_needle(path: Path, needle: str) -> bool:
    name = path.name.lower()
    hay = _search_hay(path.stem)
    for token in _needle_aliases(needle):
        if token == "cv":
            if path.suffix.lower() == ".pdf" and re.search(
                r"(?:^|[\s_\-])cv(?:$|[\s_\-]|\.)",
                name,
            ):
                return True
            continue
        if token and (token in name or token in hay.split() or token in hay):
            return True
    return False


def _search_depth(root: Path) -> int:
    name = root.name.lower()
    if name in {"code", "movies", "music"}:
        return 2
    return 5


def _walk_matching_files(
    root: Path,
    needle: str,
    *,
    kind: str = "",
    max_depth: int = 5,
    limit: int = 80,
) -> list[Path]:
    found: list[Path] = []
    try:
        resolved = root.expanduser().resolve()
    except OSError:
        return found
    if not resolved.is_dir():
        return found
    prefix_len = len(resolved.parts)
    try:
        for dirpath, dirnames, filenames in os.walk(resolved):
            current = Path(dirpath)
            depth = len(current.parts) - prefix_len
            dirnames[:] = [
                name
                for name in dirnames
                if name not in _SKIP_DIR_NAMES and not name.startswith(".")
            ]
            if depth >= max_depth:
                dirnames.clear()
            for name in filenames:
                if name.startswith(".") or len(found) >= limit:
                    continue
                child = current / name
                if path_denied(child) is not None:
                    continue
                if kind and not _matches_kind(child, kind):
                    continue
                if needle and not _name_matches_needle(child, needle):
                    continue
                try:
                    found.append(child.resolve())
                except OSError:
                    continue
            if len(found) >= limit:
                break
    except OSError:
        return found
    return found


def _spotlight_name_hits(needle: str, roots: list[Path]) -> list[Path]:
    if str(getattr(settings, "laptop_files_root", None) or "").strip():
        return []
    aliases = [
        token
        for token in _needle_aliases(needle)
        if token != "cv" and re.fullmatch(r"[A-Za-z0-9._ -]{2,40}", token)
    ]
    if not aliases or os.name != "posix":
        return []
    cmd: list[str] = ["mdfind"]
    for root in roots:
        cmd.extend(["-onlyin", str(root)])
    cmd.append(" || ".join(f'kMDItemFSName == "*{token}*"cd' for token in aliases))
    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=8,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return []
    hits: list[Path] = []
    for line in (proc.stdout or "").splitlines():
        raw = line.strip()
        if not raw:
            continue
        path = Path(raw)
        try:
            if path.is_file() and path_denied(path) is None and _name_matches_needle(path, needle):
                hits.append(path.resolve())
        except OSError:
            continue
    return hits


def _collect_file_hits(
    needle: str,
    *,
    roots: list[Path] | None = None,
    kind: str = "",
) -> list[Path]:
    token = (needle or "").strip()
    if not token:
        return []
    search_roots = roots or allowed_roots()
    seen: set[Path] = set()
    hits: list[Path] = []

    def add(path: Path) -> None:
        try:
            resolved = path.resolve()
        except OSError:
            return
        if resolved in seen or not resolved.is_file():
            return
        if path_denied(resolved) is not None:
            return
        if kind and not _matches_kind(resolved, kind):
            return
        if not _name_matches_needle(resolved, token):
            return
        seen.add(resolved)
        hits.append(resolved)

    for path in _spotlight_name_hits(token, search_roots):
        add(path)
    per_root = 80
    for root in search_roots:
        depth = _search_depth(root) if roots is None else 6
        for child in _walk_matching_files(
            root,
            token,
            kind=kind,
            max_depth=depth,
            limit=per_root,
        ):
            add(child)
            if len(hits) >= MAX_LIST * 3:
                return hits
    return hits


def resolve_existing(path_hint: str, query: str = "") -> tuple[Path | None, list[Path], str | None]:
    hint = str(path_hint or "").strip()
    needle = str(query or "").strip()
    if not needle and hint:
        hinted = Path(hint).expanduser()
        if not hinted.exists() or hinted.is_file():
            needle = hinted.name
    scoped: list[Path] | None = None
    if hint:
        candidate = Path(hint).expanduser()
        if not candidate.is_absolute():
            found_rel: Path | None = None
            for root in allowed_roots():
                trial = root / hint
                if path_denied(trial):
                    continue
                if trial.exists():
                    found_rel = trial.resolve()
                    break
            candidate = found_rel if found_rel is not None else candidate
        else:
            denied = path_denied(candidate)
            if denied:
                return None, [], denied
        try:
            exists = candidate.exists()
        except OSError:
            exists = False
        if exists and candidate.is_file():
            return candidate.resolve(), [], None
        elif exists and candidate.is_dir():
            if not needle:
                return candidate.resolve(), [], None
            scoped = [candidate.resolve()]
        elif (
            not exists
            and candidate.suffix
            and candidate.parent.exists()
            and path_denied(candidate) is None
        ):
            if not needle or candidate.name.lower() == needle.lower() or needle.lower() in candidate.name.lower():
                return candidate.expanduser(), [], None
    if not needle:
        return None, [], "not_found"
    matches = _collect_file_hits(needle, roots=scoped)
    if not matches:
        return None, [], "not_found"
    if len(matches) == 1:
        from app.ev.desk_scene import note_search_hits

        note_search_hits(matches)
        return matches[0], [], None
    best = _best_search_hit(matches, needle)
    if best is not None:
        ranked = sorted(matches, key=lambda item: _search_score(item, needle), reverse=True)
        from app.ev.desk_scene import note_search_hits

        note_search_hits(ranked[:4])
        return best, [], None
    return None, matches[:MAX_LIST], "ambiguous"


def perform_local(arguments: dict[str, Any]) -> dict[str, Any]:
    if not laptop_files_allowed():
        return {
            "ok": False,
            "executed": False,
            "verified": False,
            "error": "laptop_files_disabled",
            "spoken": "Local file access is not enabled on this API.",
        }
    args = prepare_file_arguments(arguments)
    action = str(args.get("action") or "").strip().lower()
    path_hint = str(args.get("path") or "").strip()
    query = str(args.get("query") or "").strip()
    content = str(args.get("content") or "")
    if action == "list":
        return _list_files(
            path_hint,
            query,
            kind=str(args.get("kind") or query or ""),
            recent=bool(args.get("recent")),
        )
    if action == "search":
        return _search_files(path_hint, query, kind=str(args.get("kind") or ""))
    if action == "write":
        dest = Path(path_hint).expanduser() if path_hint else _write_destination(path_hint, query)
        if dest is None:
            return _fail("not_found", "I need a file name and a folder like Desktop or Documents.")
        if dest.exists() and dest.is_dir():
            dest = dest / (query or DEFAULT_WRITE_NAME)
        if dest.suffix == "":
            dest = dest.with_name(_with_text_suffix(dest.name or DEFAULT_WRITE_NAME))
        denied = path_denied(dest)
        if denied:
            return _fail(denied, "I won't touch that path.")
        return _write_file(dest, content)
    target, matches, error = resolve_existing(path_hint, query)
    if error == "ambiguous":
        names = ", ".join(item.name for item in matches[:8])
        return {
            "ok": False,
            "executed": False,
            "verified": False,
            "error": "ambiguous",
            "matches": [str(item) for item in matches],
            "spoken": f"Which file do you mean? I found {names}.",
        }
    if target is None:
        spoken = "I couldn't find that file." if action != "write" else "I need a file name."
        return _fail(error or "not_found", spoken)
    if target.exists() and target.is_dir() and action in {
        "read",
        "open",
        "edit",
        "append",
        "rename",
        "copy",
        "move",
        "delete",
        "run",
    }:
        kind = str(args.get("kind") or "")
        newest = _newest_matching(target, kind) if kind else _newest_text_file(target)
        if newest is None:
            return _fail("not_found", f"I don't see a file in {target.name} yet.")
        target = newest
    if action == "read":
        return _read_file(target)
    if action in {"edit", "append"}:
        current = _read_file(target)
        if not current.get("ok"):
            return current
        if action == "append":
            joined = str(current.get("content") or "")
            if joined and not joined.endswith("\n"):
                joined += "\n"
            joined += content
            return _write_file(target, joined, spoken=f"Added that to {target.name}.")
        return _write_file(target, content, spoken=f"Updated {target.name}.")
    if action == "open":
        return _open_file(target)
    if action == "delete":
        if target.exists() and target.is_dir():
            return _fail("not_a_file", "I won't delete a folder.")
        return _delete_file(target)
    if action == "run":
        return _run_file(target)
    if action == "rename":
        return _rename_file(target, str(args.get("dest") or ""))
    if action == "copy":
        return _copy_file(target, str(args.get("dest") or ""))
    if action == "move":
        return _move_file(target, str(args.get("dest") or ""))
    return _fail(
        "unknown_action",
        "I can read, write, edit, delete, list, open, find, rename, copy, move, or run local files.",
    )


def prepare_file_arguments(arguments: dict[str, Any]) -> dict[str, Any]:
    """Resolve folder-only hints to a real file before MacControl sees them."""

    args = dict(arguments or {})
    action = str(args.get("action") or "").strip().lower()
    path_hint = str(args.get("path") or "").strip()
    query = str(args.get("query") or "").strip()
    if action in {
        "list",
        "search",
        "bind",
        "packet_add",
        "packet_list",
        "confirm_land",
        "scene_other",
        "scene_turns",
        "ask_which",
        "undo",
    } or not action:
        return args
    if action == "write":
        dest = _write_destination(
            path_hint,
            query,
            unique_default=not bool(args.get("overwrite")),
        )
        if dest is not None and args.get("unique_name") and dest.exists() and not args.get("overwrite"):
            dest = _unique_path(dest)
        if dest is not None:
            args["path"] = str(dest)
            args["query"] = dest.name
        return args
    target, _matches, _error = resolve_existing(path_hint, query)
    if target is not None and target.exists() and target.is_dir() and action in {
        "read",
        "open",
        "edit",
        "append",
        "rename",
        "copy",
        "move",
    }:
        kind = str(args.get("kind") or "")
        newest = _newest_matching(target, kind) if kind else _newest_text_file(target)
        if newest is not None:
            target = newest
    if target is not None:
        args["path"] = str(target)
        args["query"] = target.name
    dest_hint = str(args.get("dest") or "").strip()
    if dest_hint:
        dest_path = Path(dest_hint).expanduser()
        if dest_path.suffix == "" and action in {"copy", "move"}:
            args["dest"] = str(dest_path)
        elif dest_path.suffix == "" and action == "rename" and target is not None:
            args["dest"] = str(target.parent / _with_text_suffix(dest_path.name))
        else:
            args["dest"] = str(dest_path)
    return args


def _write_destination(
    path_hint: str, query: str, *, unique_default: bool = True
) -> Path | None:
    target, _matches, _error = resolve_existing(path_hint, query)
    dest = target
    if dest is not None and dest.exists() and dest.is_dir():
        dest = dest / (query or DEFAULT_WRITE_NAME)
    if dest is None:
        dest = _new_path(path_hint, query)
    if dest is None:
        return None
    if dest.exists() and dest.is_dir():
        dest = dest / (query or DEFAULT_WRITE_NAME)
    if dest.suffix == "":
        dest = dest.with_name(_with_text_suffix(dest.name or DEFAULT_WRITE_NAME))
    if dest.name == DEFAULT_WRITE_NAME and unique_default:
        dest = _unique_path(dest)
    return dest


def _unique_path(dest: Path) -> Path:
    if not dest.exists():
        return dest
    stem = dest.stem
    suffix = dest.suffix or ".txt"
    folder = dest.parent
    for index in range(2, 80):
        candidate = folder / f"{stem}-{index}{suffix}"
        if not candidate.exists():
            return candidate
    return folder / f"{stem}-{int(time.time())}{suffix}"


def _new_path(path_hint: str, query: str) -> Path | None:
    hint = str(path_hint or "").strip()
    name = _with_text_suffix(Path(hint).name if hint else str(query or "").strip()) or DEFAULT_WRITE_NAME
    if "/" in hint or hint.startswith("~") or (hint and Path(hint).expanduser().is_absolute()):
        candidate = Path(hint).expanduser()
        if candidate.exists() and candidate.is_dir():
            return candidate / (query or DEFAULT_WRITE_NAME)
        if candidate.suffix == "" and query:
            return candidate / _with_text_suffix(query)
        if candidate.suffix == "":
            return candidate / DEFAULT_WRITE_NAME
        return candidate
    roots = allowed_roots()
    folder = Path(hint).expanduser() if hint and Path(hint).expanduser().is_dir() else roots[0]
    if folder.is_file():
        folder = folder.parent
    return folder / name


def _newest_text_file(folder: Path) -> Path | None:
    return _newest_matching(folder, "text")


def _matches_kind(path: Path, kind: str) -> bool:
    token = (kind or "").strip().lower().lstrip(".")
    if not token:
        return True
    name = path.name.lower()
    suffix = path.suffix.lower()
    if token in {"pdf"}:
        return suffix == ".pdf"
    if token in {"html", "htm"}:
        return suffix in {".html", ".htm"}
    if token in {"screenshot"}:
        return "screenshot" in name
    if token in {"image", "photo", "picture"}:
        return suffix in {".png", ".jpg", ".jpeg", ".gif", ".webp", ".heic"}
    if token in {"text", "txt"}:
        return suffix in TEXT_EXTENSIONS or suffix == ""
    if token.startswith("."):
        return suffix == token
    if len(token) <= 8 and token.isalpha():
        return suffix == f".{token}" or token in name
    return token in name


def _newest_matching(folder: Path, kind: str) -> Path | None:
    try:
        children = [
            child
            for child in folder.iterdir()
            if child.is_file()
            and not child.name.startswith(".")
            and path_denied(child) is None
            and _matches_kind(child, kind)
        ]
    except OSError:
        return None
    if not children:
        return None
    children.sort(key=lambda item: item.stat().st_mtime, reverse=True)
    return children[0]


def _list_files(
    path_hint: str,
    query: str,
    *,
    kind: str = "",
    recent: bool = False,
) -> dict[str, Any]:
    folder: Path | None = None
    if path_hint:
        candidate = Path(path_hint).expanduser()
        if candidate.exists() and candidate.is_dir() and path_denied(candidate) is None:
            folder = candidate.resolve()
        elif candidate.exists() and candidate.is_file():
            folder = candidate.parent
    if folder is None:
        folder = allowed_roots()[0]
    denied = path_denied(folder)
    if denied:
        return _fail(denied, "I won't list that folder.")
    needle = (query or "").lower()
    if needle in {"pdf", "html", "screenshot", "image", "text", "txt"}:
        kind = kind or needle
        needle = ""
    names: list[str] = []
    try:
        children = [child for child in folder.iterdir() if not child.name.startswith(".")]
    except OSError as exc:
        return _fail("list_failed", f"I couldn't list that folder. {type(exc).__name__}")
    matched: list[Path] = []
    for child in children:
        if path_denied(child) is not None:
            continue
        if kind and not _matches_kind(child, kind):
            continue
        if needle and needle not in child.name.lower():
            continue
        matched.append(child)
    if recent:
        matched.sort(key=lambda item: item.stat().st_mtime if item.exists() else 0, reverse=True)
        matched = matched[:8]
    else:
        matched.sort(key=lambda item: item.name.lower())
    names = [child.name for child in matched[:MAX_LIST]]
    if names:
        prefix = f"Recent on {folder.name}" if recent else f"On {folder.name}"
        spoken = f"{prefix}: {', '.join(names)}."
    else:
        spoken = f"{folder.name} looks empty."
    return {
        "ok": True,
        "executed": True,
        "verified": True,
        "action": "list",
        "path": str(folder),
        "files": names,
        "count": len(names),
        "spoken": spoken,
        "source": "laptop_files",
    }


def _search_hay(name: str) -> str:
    return re.sub(r"[_\-]+", " ", (name or "").lower())


def _search_score(path: Path, needle: str) -> int:
    aliases = _needle_aliases(needle)
    if not aliases:
        return 0
    name = path.name.lower()
    hay = _search_hay(path.stem)
    words = hay.split()
    score = 0
    for token in aliases:
        if token == "cv" and not re.search(r"(?:^|[\s_\-])cv(?:$|[\s_\-]|\.)", name):
            continue
        current = 0
        if path.stem.lower() == token or name == token or name == f"{token}{path.suffix.lower()}":
            current = 100
        elif hay == token or hay.endswith(" " + token):
            current = 85
        elif token in words:
            current = 70
        elif token in hay or token in name:
            current = 50
        if current > score:
            score = current
    if score <= 0:
        return 0
    score -= min(len(path.stem), 10)
    if path.suffix.lower() == ".pdf" and any(token in {"resume", "cv", "pdf"} for token in aliases):
        score += 14
    parent = path.parent.name.lower()
    if parent == "desktop":
        score += 18
        if any(token in {"resume", "cv"} for token in aliases):
            score += 8
    elif parent in {"documents", "docs"}:
        score += 10
    elif parent == "downloads":
        score += 2
    if len(words) >= 3:
        score += 4
    lowered_hay = f"{hay} {name}"
    for token in _SEARCH_PENALTY:
        if token in lowered_hay and token not in aliases:
            score -= 12 if token == "flagship" else 6
    return score


def _best_search_hit(matches: list[Path], needle: str) -> Path | None:
    if not matches:
        return None
    ranked = sorted(
        matches,
        key=lambda item: (_search_score(item, needle), -len(item.stem)),
        reverse=True,
    )
    top = ranked[0]
    if _search_score(top, needle) <= 0:
        return None
    return top


def _search_files(path_hint: str, query: str, *, kind: str = "") -> dict[str, Any]:
    needle = (query or kind or "").strip().lower()
    if not needle:
        return _fail("not_found", "What should I look for?")
    roots: list[Path] | None = None
    if path_hint:
        candidate = Path(path_hint).expanduser()
        if candidate.exists() and candidate.is_dir() and path_denied(candidate) is None:
            roots = [candidate.resolve()]
        elif candidate.exists() and candidate.is_file():
            roots = [candidate.parent]
    hits = _collect_file_hits(needle, roots=roots, kind=kind)
    if not hits:
        return _fail("not_found", f"I couldn't find {query or kind} there.")
    best = _best_search_hit(hits, needle) or hits[0]
    ranked = sorted(hits, key=lambda item: _search_score(item, needle), reverse=True)
    from app.ev.desk_scene import note_search_hits

    note_search_hits(ranked[:4])
    names = [item.name for item in hits[:MAX_LIST]]
    spoken = f"I found {best.name} on {best.parent.name}."
    if len(hits) > 1:
        spoken = f"I found {best.name} on {best.parent.name}. {len(hits)} matches."
    return {
        "ok": True,
        "executed": True,
        "verified": True,
        "action": "search",
        "path": str(best),
        "files": names,
        "count": len(hits),
        "spoken": spoken,
        "source": "laptop_files",
    }


def _rename_file(target: Path, dest_hint: str) -> dict[str, Any]:
    denied = path_denied(target)
    if denied:
        return _fail(denied, "I won't touch that path.")
    dest = Path(dest_hint).expanduser() if dest_hint else None
    if dest is None or str(dest) == "":
        return _fail("not_found", "What should I rename it to?")
    if dest.suffix == "":
        dest = target.parent / _with_text_suffix(dest.name)
    elif not dest.is_absolute():
        dest = target.parent / dest.name
    denied = path_denied(dest)
    if denied:
        return _fail(denied, "I won't put it there.")
    if dest.exists():
        return _fail("exists", f"{dest.name} already exists.")
    try:
        target.rename(dest)
    except OSError as exc:
        return _fail("rename_failed", f"I couldn't rename {target.name}. {type(exc).__name__}")
    return {
        "ok": True,
        "executed": True,
        "verified": dest.exists(),
        "action": "rename",
        "path": str(dest),
        "spoken": f"Renamed it to {dest.name}.",
        "source": "laptop_files",
    }


def _copy_file(target: Path, dest_hint: str) -> dict[str, Any]:
    denied = path_denied(target)
    if denied:
        return _fail(denied, "I won't touch that path.")
    dest = _dest_file(target, dest_hint or str(target.parent))
    if dest is None:
        return _fail("not_found", "Where should I copy it?")
    denied = path_denied(dest)
    if denied:
        return _fail(denied, "I won't put it there.")
    dest = _unique_path(dest) if dest.exists() else dest
    try:
        dest.write_bytes(target.read_bytes())
    except OSError as exc:
        return _fail("copy_failed", f"I couldn't copy {target.name}. {type(exc).__name__}")
    return {
        "ok": True,
        "executed": True,
        "verified": dest.exists(),
        "action": "copy",
        "path": str(dest),
        "spoken": f"Copied {target.name} to {dest.parent.name}.",
        "source": "laptop_files",
    }


def _move_file(target: Path, dest_hint: str) -> dict[str, Any]:
    denied = path_denied(target)
    if denied:
        return _fail(denied, "I won't touch that path.")
    dest = _dest_file(target, dest_hint)
    if dest is None:
        return _fail("not_found", "Where should I move it?")
    denied = path_denied(dest)
    if denied:
        return _fail(denied, "I won't put it there.")
    dest = _unique_path(dest) if dest.exists() else dest
    try:
        target.rename(dest)
    except OSError as exc:
        return _fail("move_failed", f"I couldn't move {target.name}. {type(exc).__name__}")
    return {
        "ok": True,
        "executed": True,
        "verified": dest.exists(),
        "action": "move",
        "path": str(dest),
        "spoken": f"Moved {dest.name} to {dest.parent.name}.",
        "source": "laptop_files",
    }


def _delete_file(target: Path) -> dict[str, Any]:
    denied = path_denied(target)
    if denied:
        return _fail(denied, "I won't touch that path.")
    if not target.is_file():
        return _fail("not_a_file", f"{target.name} is not a file.")
    name = target.name
    parent = target.parent
    try:
        target.unlink()
    except OSError as exc:
        return _fail("delete_failed", f"I couldn't delete {name}. {type(exc).__name__}")
    from app.ev.desk_scene import forget_file

    forget_file(target)
    return {
        "ok": True,
        "executed": True,
        "verified": not target.exists(),
        "action": "delete",
        "path": str(parent / name),
        "spoken": f"Deleted {name}.",
        "source": "laptop_files",
    }


def _run_file(target: Path) -> dict[str, Any]:
    denied = path_denied(target)
    if denied:
        return _fail(denied, "I won't run that path.")
    if not target.is_file():
        return _fail("not_a_file", f"{target.name} is not a file.")
    runner = RUNNABLE_SUFFIXES.get(target.suffix.lower())
    if not runner:
        return _fail("not_runnable", f"{target.name} isn't a script I will run.")
    try:
        completed = subprocess.run(
            [*runner, str(target)],
            cwd=str(target.parent),
            capture_output=True,
            text=True,
            timeout=12,
            check=False,
        )
    except subprocess.TimeoutExpired:
        return _fail("run_timeout", f"{target.name} ran too long, so I stopped it.")
    except OSError as exc:
        return _fail("run_failed", f"I couldn't run {target.name}. {type(exc).__name__}")
    output = ((completed.stdout or "") + (completed.stderr or "")).strip()
    if len(output) > 400:
        output = output[:397] + "..."
    ok = completed.returncode == 0
    spoken = output or (f"Ran {target.name}." if ok else f"{target.name} exited {completed.returncode}.")
    return {
        "ok": ok,
        "executed": True,
        "verified": ok,
        "action": "run",
        "path": str(target),
        "exit_code": completed.returncode,
        "output": (completed.stdout or "")[:MAX_FILE_BYTES],
        "spoken": spoken,
        "source": "laptop_files",
    }


def _dest_file(target: Path, dest_hint: str) -> Path | None:
    raw = str(dest_hint or "").strip()
    if not raw:
        return None
    dest = Path(raw).expanduser()
    if dest.suffix == "" or dest.exists() and dest.is_dir():
        folder = dest if dest.suffix == "" or dest.is_dir() else dest.parent
        dest = folder / target.name
    denied = path_denied(dest)
    if denied:
        return None
    return dest


def _read_file(target: Path) -> dict[str, Any]:
    denied = path_denied(target)
    if denied:
        return _fail(denied, "I won't read that path.")
    if not target.is_file():
        return _fail("not_a_file", f"{target.name} is not a file.")
    if target.suffix.lower() not in TEXT_EXTENSIONS and target.suffix:
        return {
            "ok": True,
            "executed": True,
            "verified": True,
            "action": "read",
            "path": str(target),
            "binary": True,
            "size_bytes": target.stat().st_size,
            "spoken": f"{target.name} is not a text file I can read aloud.",
            "source": "laptop_files",
        }
    data = target.read_bytes()
    if len(data) > MAX_FILE_BYTES:
        return _fail("too_large", f"{target.name} is larger than I will read.")
    text = data.decode("utf-8", errors="replace")
    preview = text.strip()
    spoken = f"The file says: {preview}" if preview else f"{target.name} is empty."
    if len(spoken) > 420:
        spoken = spoken[:417] + "..."
    return {
        "ok": True,
        "executed": True,
        "verified": True,
        "action": "read",
        "path": str(target),
        "content": text,
        "size_bytes": len(data),
        "truncated": False,
        "spoken": spoken,
        "source": "laptop_files",
    }


def _write_file(target: Path, content: str, *, spoken: str | None = None) -> dict[str, Any]:
    denied = path_denied(target)
    if denied:
        return _fail(denied, "I won't write that path.")
    if len(content.encode("utf-8")) > MAX_FILE_BYTES:
        return _fail("too_large", "That file is larger than I will write.")
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_name(target.name + ".ev-tmp")
    tmp.write_text(content, encoding="utf-8")
    tmp.replace(target)
    check = target.read_text(encoding="utf-8")
    verified = check == content
    return {
        "ok": verified,
        "executed": True,
        "verified": verified,
        "action": "write",
        "path": str(target),
        "bytes": target.stat().st_size,
        "spoken": spoken or f"Wrote {target.name}.",
        "source": "laptop_files",
        "error": None if verified else "verify_mismatch",
    }


def _open_file(target: Path) -> dict[str, Any]:
    denied = path_denied(target)
    if denied:
        return _fail(denied, "I won't open that path.")
    try:
        subprocess.run(["open", str(target)], check=False, timeout=8)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return _fail("open_failed", f"I couldn't open {target.name}. {type(exc).__name__}")
    return {
        "ok": True,
        "executed": True,
        "verified": True,
        "action": "open",
        "path": str(target),
        "spoken": f"Opened {target.name}.",
        "source": "laptop_files",
    }


def _fail(error: str, spoken: str) -> dict[str, Any]:
    return {
        "ok": False,
        "executed": False,
        "verified": False,
        "error": error,
        "spoken": spoken,
        "source": "laptop_files",
    }


def apply_simple_edit(current: str, instruction: str) -> str | None:
    text = (instruction or "").strip()
    if not text:
        return None
    body = current.replace("\r\n", "\n")
    keep = KEEP_ONLY_RE.search(text)
    if keep:
        token = _mutation_token(next((g for g in keep.groups() if g), ""))
        if token:
            return _keep_only_lines(body, token)
    if CLEAR_RE.search(text) and not KEEP_ONLY_RE.search(text):
        return ""
    rewrite = REWRITE_TO_RE.search(text)
    if rewrite:
        token = _spoken_file_body(rewrite.group(1))
        if token:
            return token
    match = REPLACE_RE.search(text)
    if match:
        old, new = match.group(1).strip(" \"'"), match.group(2).strip(" \"'")
        if old and old in body:
            return body.replace(old, new)
        if old and old.lower() in body.lower():
            pattern = re.compile(re.escape(old), re.I)
            return pattern.sub(new, body)
    dropped = DROP_ITEM_RE.search(text)
    if dropped and not KEEP_ONLY_RE.search(text) and not FILE_DELETE_RE.search(text):
        from app.ev.desk_meaning import strip_reason_clause

        blob = strip_reason_clause(dropped.group(1) or "")
        parts = [part.strip() for part in re.split(r"\s*(?:,\s*(?:and\s+)?|\s+and\s+)\s*", blob) if part.strip()]
        tokens = [_mutation_token(part) for part in (parts or [blob])]
        edited = body
        changed = False
        for token in tokens:
            if not token or token.lower() in {"everything", "all", "the rest", "it", "that", "this"}:
                continue
            nxt = _drop_token_lines(edited, token)
            if nxt is not None:
                edited = nxt
                changed = True
        if changed:
            return edited
    if re.search(r"\b(?:append|add)\b", text, re.I):
        addition = SAYS_RE.search(text)
        extra = addition.group(1).strip(" \"'") if addition else text
        extra = re.sub(r"^(?:please\s+)?(?:append|add(?:\s+to\s+it)?)\s+", "", extra, flags=re.I).strip()
        extra = _append_body(text) or extra
        extra = _spoken_file_body(extra)
        if extra and not re.search(r"\b(?:delete|remove|keep|clear)\b", extra, re.I):
            prefix = body if body.endswith("\n") or not body else body + "\n"
            return prefix + extra
    return None


def _mutation_token(blob: str) -> str:
    raw = _spoken_file_body(blob or "")
    raw = re.sub(
        r"\b(?:please|can you|could you|just|only|alone|the|a|an|"
        r"word|words|line|lines|item|items|entry|entries|"
        r"from (?:it|that|this|the (?:file|note|list))|"
        r"in (?:it|that|this|the (?:file|note|list))|"
        r"on (?:the |my )?(?:list|note|file))\b",
        " ",
        raw,
        flags=re.I,
    )
    return re.sub(r"\s+", " ", raw).strip(" .,")


def _token_aliases(token: str) -> tuple[str, ...]:
    t = (token or "").strip().lower()
    if len(t) < 2:
        return ()
    aliases = {t}
    if t.endswith("ies") and len(t) > 4:
        aliases.add(t[:-3] + "y")
    elif t.endswith("es") and len(t) > 3:
        aliases.add(t[:-2])
        aliases.add(t[:-1])
    elif t.endswith("s") and len(t) > 3:
        aliases.add(t[:-1])
    else:
        aliases.add(t + "s")
        aliases.add(t + "es")
    return tuple(aliases)


def _line_has_token(line: str, token: str) -> bool:
    hay = (line or "").lower()
    for alias in _token_aliases(token):
        if re.search(rf"\b{re.escape(alias)}\b", hay) or alias in hay:
            return True
    return False


def _keep_only_lines(current: str, token: str) -> str:
    lines = current.split("\n")
    kept = [line for line in lines if line.strip() and _line_has_token(line, token)]
    if kept:
        return "\n".join(kept)
    return token


def _drop_token_lines(current: str, token: str) -> str | None:
    lines = current.split("\n")
    remaining = [line for line in lines if not (line.strip() and _line_has_token(line, token))]
    if remaining == lines:
        return None
    while remaining and not remaining[-1].strip():
        remaining.pop()
    return "\n".join(remaining)


async def plan_file_content(
    *,
    action: str,
    current: str,
    instruction: str,
    content: str,
    label: str = "",
    receipt: str = "",
) -> tuple[str, str]:
    """Return (new_content, intelligence_source)."""

    if action == "write":
        from app.ev.desk_meaning import is_kind_echo, resolve_write_body

        body, source, _items = await resolve_write_body(
            instruction,
            proposed=content,
            label=label,
            receipt=receipt,
        )
        if source in {"inventory", "spark"}:
            return body, source
        if source == "empty":
            from app.ev.desk_meaning import leftover_needs_model

            if leftover_needs_model(instruction, [], label=label) and _has_substance(instruction):
                drafted, intel = await _intelligent_rewrite("", instruction, create=True)
                if drafted and not is_kind_echo(drafted, label):
                    return drafted, intel
            return body, "empty"
        if body and not is_kind_echo(body, label):
            return body, source or "literal"
        if instruction and _has_substance(instruction) and not _is_naming_only(instruction):
            drafted, intel = await _intelligent_rewrite("", instruction, create=True)
            if drafted and not is_kind_echo(drafted, label):
                return drafted, intel
        if receipt in {"named_list", "dated_note"}:
            return "", "empty"
        return "Note from Evie.\n", "literal"
    if action == "append" and content:
        prefix = current if current.endswith("\n") or not current else current + "\n"
        return prefix + content, "literal"
    simple = apply_simple_edit(current, instruction or content)
    if simple is not None:
        return simple, "replace"
    drafted, source = await _intelligent_rewrite(current, instruction or content, create=False)
    return drafted, source


async def _intelligent_rewrite(current: str, instruction: str, *, create: bool) -> tuple[str, str]:
    from app.ev.desk_meaning import wants_generated_contents

    if create and wants_generated_contents(instruction, []):
        prompt = (
            "Create a new local text file for the owner. Return JSON "
            '{"content":"..."} with the full file body only. They asked you to '
            "propose the contents for a situation or kind of list and did not "
            "enumerate the lines. The situation (a flight, interview, trip) is "
            "why the list exists, not a line in it. Write 6-12 concrete real-world "
            "items, one per line — never the occasion phrase, never the list kind "
            "or title, never the filename, and never the folder "
            "(desktop/documents).\n"
            f"Instruction: {instruction[:4000]}"
        )
    elif create:
        prompt = (
            "Create a new local text file for the owner. Return JSON "
            '{"content":"..."} with the full file body only. Write the named '
            "items, tasks, or note text — never the list kind or title, never "
            "the filename, and never the folder (desktop/documents). "
            "If they named no contents, return {\"content\":\"\"}.\n"
            f"Instruction: {instruction[:4000]}"
        )
    else:
        prompt = (
            "Edit this local text file. Apply the instruction. Return JSON "
            '{"content":"..."} with the FULL new file body, not a patch.\n'
            f"Instruction: {instruction[:2000]}\n---\nCURRENT FILE:\n{current[:MAX_FILE_BYTES]}"
        )
    from app.gateway.muse import (
        MuseProviderUnavailable,
        muse_intelligence_active,
        muse_spark_key_loaded,
    )

    spark_lane = muse_intelligence_active() or muse_spark_key_loaded()
    if spark_lane:
        if not muse_spark_key_loaded():
            raise RuntimeError("file_intelligence_unavailable")
        try:
            from app.contracts import ChatMessage
            from app.gateway.muse import muse_spark_model
            from app.gateway.muse_spark import muse_spark_provider

            result = await muse_spark_provider().chat(
                [
                    ChatMessage(
                        role="system",
                        content='You edit local files for Evie. Reply with JSON {"content": "..."} only.',
                    ),
                    ChatMessage(role="user", content=prompt),
                ],
                model=muse_spark_model(),
            )
        except MuseProviderUnavailable as exc:
            raise RuntimeError("file_intelligence_unavailable") from exc
        parsed = _parse_content_json(result.text or "")
        if parsed is None:
            raise RuntimeError("file_intelligence_unavailable")
        return parsed, "spark"

    luna = await _call_chat_model(
        provider="openai",
        model=(getattr(settings, "turn_control_model", None) or "gpt-5.6-luna").strip() or "gpt-5.6-luna",
        fallback=(getattr(settings, "turn_control_fallback_model", None) or "gpt-4o-mini").strip(),
        prompt=prompt,
        api_key=(getattr(settings, "openai_api_key", None) or "").strip(),
        base_url=(getattr(settings, "openai_base_url", None) or "https://api.openai.com/v1").rstrip("/"),
    )
    if luna is not None:
        return luna, "luna"
    deepseek = await _call_chat_model(
        provider="deepseek",
        model=(getattr(settings, "deepseek_model", None) or "deepseek-v4-flash").strip(),
        fallback="",
        prompt=prompt,
        api_key=(getattr(settings, "deepseek_api_key", None) or "").strip(),
        base_url=(getattr(settings, "deepseek_base_url", None) or "https://api.deepseek.com").rstrip("/"),
    )
    if deepseek is not None:
        return deepseek, "deepseek"
    raise RuntimeError("file_intelligence_unavailable")


async def _call_chat_model(
    *,
    provider: str,
    model: str,
    fallback: str,
    prompt: str,
    api_key: str,
    base_url: str,
) -> str | None:
    if not api_key or not model:
        return None
    from app.gateway.muse import refuse_legacy_cloud_brain

    refuse_legacy_cloud_brain(provider)
    import httpx

    models = [model]
    if fallback and fallback != model:
        models.append(fallback)
    url = f"{base_url}/chat/completions"
    headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
    for attempt in models:
        payload = {
            "model": attempt,
            "temperature": 0.2,
            "messages": [
                {
                    "role": "system",
                    "content": "You edit local files for Evie. Reply with JSON {\"content\": \"...\"} only.",
                },
                {"role": "user", "content": prompt},
            ],
            "response_format": {"type": "json_object"},
        }
        try:
            async with httpx.AsyncClient(timeout=25) as client:
                resp = await client.post(url, headers=headers, json=payload)
            if resp.status_code == 404 and attempt != models[-1]:
                continue
            if resp.status_code != 200:
                logger.info("file_intelligence provider=%s model=%s status=%s", provider, attempt, resp.status_code)
                continue
            data = resp.json()
            text = (
                ((data.get("choices") or [{}])[0].get("message") or {}).get("content")
                or ""
            )
            parsed = _parse_content_json(text)
            if parsed is not None:
                return parsed
        except Exception as exc:  # noqa: BLE001 - model miss must not kill Talk
            logger.info("file_intelligence provider=%s error=%s", provider, type(exc).__name__)
            continue
    return None


def _parse_content_json(raw: str) -> str | None:
    text = (raw or "").strip()
    if not text:
        return None
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*", "", text)
        text = re.sub(r"\s*```$", "", text)
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        return text if text else None
    if isinstance(data, dict) and "content" in data:
        return str(data.get("content") or "")
    return None


def _file_spoken_receipt(
    args: dict[str, Any], result: dict[str, Any], original_action: str
) -> str | None:
    if original_action not in {"append", "edit", "write", "checkoff", "undo", "drop"}:
        return None
    from app.ev.desk_acts import spoken_receipt

    items = [str(item).strip() for item in (args.get("items") or []) if str(item).strip()]
    if not items:
        items = _receipt_items_from_bodies(
            str(args.get("_before") or ""),
            str(args.get("content") or result.get("content") or ""),
        )
    instruction = str(args.get("instruction") or args.get("goal") or "")
    receipt = str(args.get("receipt") or original_action)
    name = Path(str(result.get("path") or args.get("path") or "")).name
    if original_action == "edit":
        keep = KEEP_ONLY_RE.search(instruction)
        if keep:
            token = _mutation_token(next((group for group in keep.groups() if group), ""))
            if token:
                spoken = spoken_receipt(action="keep_only", items=[token])
                if spoken:
                    return spoken
        dropped = DROP_ITEM_RE.search(instruction)
        if (
            dropped
            and not FILE_DELETE_RE.search(instruction)
            and not KEEP_ONLY_RE.search(instruction)
        ):
            token = _mutation_token(dropped.group(1))
            if token and token.lower() not in {"everything", "all", "the rest", "it", "that", "this"}:
                spoken = spoken_receipt(action="drop", items=[token])
                if spoken:
                    return spoken
        return f"Updated {name}." if name else None
    if original_action == "drop" and items:
        spoken = spoken_receipt(action="drop", items=items)
        if spoken:
            return spoken
        return f"Updated {name}." if name else None
    if original_action == "append":
        spoken = spoken_receipt(action="append", items=items, name=name)
        return spoken if items else (f"Added that to {name}." if name else None)
    if original_action == "write" and receipt in {"named_list", "dated_note"}:
        return spoken_receipt(
            action=receipt,
            items=items,
            name=name,
            label=str(args.get("label") or ""),
            when_label=str(args.get("when_label") or ""),
            instruction=instruction,
            preview=str(args.get("content") or "")[:160],
        )
    if original_action == "write":
        spoken = spoken_receipt(
            action="write_note",
            items=items,
            name=name,
            label=str(args.get("label") or ""),
        )
        if spoken:
            return spoken
        preview = str(args.get("content") or result.get("content") or "").strip()
        first = next((line.strip() for line in preview.splitlines() if line.strip()), "")
        first = re.sub(r"^[\-\*\d\.\)\s]+", "", first).strip()
        first = re.sub(r"^\[x\]\s*", "", first, flags=re.I).strip()
        if first and not re.match(r"^note from evie\.?$", first, re.I):
            return spoken_receipt(
                action="write_note",
                items=[first[:80]],
                name=name,
                label=str(args.get("label") or ""),
            )
        return None
    return spoken_receipt(
        action=receipt,
        items=items,
        name=name,
        label=str(args.get("label") or ""),
        when_label=str(args.get("when_label") or ""),
        instruction=instruction,
        preview=str(args.get("content") or "")[:160],
    )


def _receipt_items_from_bodies(before: str, after: str) -> list[str]:
    old = {line.strip() for line in (before or "").splitlines() if line.strip()}
    out: list[str] = []
    for line in (after or "").replace("\r\n", "\n").splitlines():
        token = line.strip()
        if not token or token in old:
            continue
        if re.match(r"^note from evie\.?$", token, re.I):
            continue
        cleaned = re.sub(r"^[\-\*\d\.\)\s]+", "", token).strip()
        cleaned = re.sub(r"^\[x\]\s*", "", cleaned, flags=re.I).strip()
        if cleaned:
            out.append(cleaned)
        if len(out) >= 12:
            break
    return out


async def _run_undo(args: dict[str, Any]) -> dict[str, Any]:
    from app.ev.desk_scene import forget_file, last_mutating_entry, remember_file, record_mutation

    entry = last_mutating_entry()
    if entry is None:
        return _fail("not_found", "There's nothing to undo on the desk.")
    path = Path(str(entry.get("path") or args.get("path") or "")).expanduser()
    denied = path_denied(path)
    if denied:
        return _fail(denied, "I won't touch that path.")
    before = str(entry.get("before") or "")
    current = ""
    try:
        if path.is_file():
            current = path.read_text(encoding="utf-8")
    except OSError:
        current = ""
    previous = str(entry.get("action") or "")
    created = previous == "write" and not before.strip()
    if created and path.is_file():
        try:
            path.unlink()
        except OSError as exc:
            return _fail("undo_failed", f"I couldn't put that back. {type(exc).__name__}")
        forget_file(path)
        record_mutation(path, action="undo", before=current)
        return {
            "ok": True,
            "executed": True,
            "verified": not path.exists(),
            "action": "undo",
            "path": str(path),
            "spoken": "Put it back.",
            "source": "desk_acts",
        }
    result = _write_file(path, before, spoken="Put it back.")
    if result.get("ok"):
        remember_file(path, source="undo", before=current, content=before)
        result["action"] = "undo"
        result["spoken"] = "Put it back."
        from app.ev.desk_scene import set_last_spoken

        set_last_spoken("Put it back.")
    return result


async def run_file_goal(
    arguments: dict[str, Any],
    *,
    live=None,
    request_id: str | None = None,
) -> dict[str, Any]:
    args = prepare_file_arguments(dict(arguments or {}))
    session_id = str(args.get("session_id") or "").strip() or None
    if session_id:
        from app.ev.desk_scene import set_session_id

        set_session_id(session_id)
    original_action = str(args.get("action") or "").strip().lower()
    from app.ev.desk_scene import run_scene_action

    scene_result = await run_scene_action(args)
    if scene_result is not None:
        return scene_result
    if original_action == "ask_which":
        from app.ev.desk_scene import set_pending_choice

        set_pending_choice(
            {
                "kind": "append",
                "items": list(args.get("items") or []),
                "candidates": list(args.get("candidates") or []),
                "goal": str(args.get("goal") or ""),
            }
        )
        spoken = str(args.get("spoken") or "Which note did you mean?")
        from app.ev.desk_scene import set_last_spoken

        set_last_spoken(spoken)
        return {
            "ok": True,
            "executed": True,
            "verified": True,
            "action": "ask_which",
            "spoken": spoken,
            "source": "desk_acts",
        }
    if original_action == "undo":
        return await _run_undo(args)
    if original_action == "bind":
        from app.ev.desk_names import remember_file

        record = remember_file(
            args.get("path"),
            aliases=[str(args.get("alias") or args.get("query") or "")],
            goal=str(args.get("goal") or ""),
            source="bind",
        )
        if record is None:
            return _fail("not_found", "I need the file in front of us to name it.")
        alias = str((record.get("aliases") or [args.get("alias")])[0] or "that")
        name = Path(str(record.get("path") or "")).name
        return {
            "ok": True,
            "executed": True,
            "verified": True,
            "action": "bind",
            "path": record.get("path"),
            "spoken": f"I'll remember {name} as your {alias}.",
            "source": "laptop_files",
        }
    if original_action in {"edit", "append", "write", "checkoff", "drop"}:
        current = ""
        if original_action in {"edit", "append", "checkoff", "drop"}:
            read_args = {**args, "action": "read"}
            existing = await execute_file_op(read_args, live=live, request_id=request_id)
            if not existing.get("ok") and original_action in {"edit", "append", "checkoff", "drop"}:
                return existing
            current = str(existing.get("content") or "")
        elif original_action == "write":
            dest = Path(str(args.get("path") or "")).expanduser()
            try:
                if dest.is_file():
                    current = dest.read_text(encoding="utf-8")
            except OSError:
                current = ""
        if original_action == "checkoff":
            from app.ev.desk_acts import apply_checkoff

            tokens = [str(item) for item in (args.get("items") or []) if str(item).strip()]
            planned, hit = apply_checkoff(current, tokens)
            if not hit:
                return _fail("not_found", "I don't see that on the list.")
            args["content"] = planned
            args["items"] = hit
            args["intelligence"] = "checkoff"
            args["action"] = "write"
            args["overwrite"] = True
        elif original_action == "drop":
            from app.ev.desk_acts import apply_drop

            tokens = [str(item) for item in (args.get("items") or []) if str(item).strip()]
            planned, hit = apply_drop(current, tokens)
            if not hit:
                return _fail("not_found", "I don't see that on the list.")
            args["content"] = planned
            args["items"] = hit
            args["intelligence"] = "drop"
            args["action"] = "write"
            args["overwrite"] = True
        else:
            try:
                planned, source = await plan_file_content(
                    action=original_action,
                    current=current,
                    instruction=str(args.get("instruction") or args.get("goal") or ""),
                    content=str(args.get("content") or ""),
                    label=str(args.get("label") or ""),
                    receipt=str(args.get("receipt") or original_action),
                )
            except RuntimeError:
                return {
                    "ok": False,
                    "executed": False,
                    "verified": False,
                    "error": "file_intelligence_unavailable",
                    "spoken": "I found the file, but I couldn't plan that edit yet.",
                }
            args["content"] = planned
            args["intelligence"] = source
            if original_action == "write" and planned:
                landed = [
                    re.sub(r"^[\-\*\d\.\)\s]+", "", line).strip()
                    for line in planned.splitlines()
                    if line.strip()
                ]
                if landed:
                    args["items"] = landed
            if original_action in {"append", "edit"}:
                args["action"] = "write"
                args["overwrite"] = True
        args["_before"] = current
    if original_action == "delete":
        dest = Path(str(args.get("path") or "")).expanduser()
        try:
            if dest.is_file() and dest.suffix.lower() in TEXT_EXTENSIONS:
                args["_before"] = dest.read_text(encoding="utf-8")
        except OSError:
            args["_before"] = ""
    result = await execute_file_op(args, live=live, request_id=request_id)
    if args.get("intelligence"):
        result["intelligence"] = args["intelligence"]
    if result.get("ok"):
        spoken = _file_spoken_receipt(args, result, original_action)
        if spoken:
            result["spoken"] = spoken
        heard = str(result.get("spoken") or "").strip()
        if heard:
            from app.ev.desk_scene import set_last_spoken

            set_last_spoken(heard)
    if result.get("ok") and original_action == "delete":
        from app.ev.desk_scene import forget_file, record_mutation

        gone = result.get("path") or args.get("path")
        record_mutation(gone, action="delete", before=str(args.get("_before") or ""))
        forget_file(gone)
        return result
    if result.get("ok") and original_action in {
        "write",
        "append",
        "edit",
        "checkoff",
        "drop",
        "open",
        "read",
        "search",
        "rename",
        "copy",
        "move",
        "run",
    }:
        from app.ev.desk_names import remember_file
        from app.ev.desk_scene import clear_pending_choice

        remember_file(
            result.get("path") or args.get("path"),
            goal=str(args.get("goal") or ""),
            content=str(args.get("content") or "")
            if original_action in {"write", "append", "edit", "checkoff", "drop"}
            else "",
            query=str(args.get("query") or ""),
            aliases=[str(item) for item in (args.get("aliases") or []) if str(item).strip()],
            source=original_action,
            before=str(args.get("_before") or ""),
        )
        if original_action in {"write", "append", "edit", "checkoff", "drop"} or args.get("clear_choice"):
            clear_pending_choice()
    return result


async def execute_file_op(
    arguments: dict[str, Any],
    *,
    live=None,
    request_id: str | None = None,
) -> dict[str, Any]:
    arguments = prepare_file_arguments(dict(arguments or {}))
    payload = {
        key: arguments.get(key)
        for key in ("action", "path", "query", "content", "instruction", "dest", "kind")
        if arguments.get(key) not in (None, "")
    }
    if arguments.get("recent"):
        payload["recent"] = True
    if arguments.get("overwrite"):
        payload["overwrite"] = True
    action = str(arguments.get("action") or "").strip().lower()
    path_raw = str(arguments.get("path") or "").strip()
    path_obj = Path(path_raw).expanduser() if path_raw else None
    concrete_file = bool(path_obj is not None and path_obj.is_file())
    query = str(arguments.get("query") or "").strip().lower()
    mismatched = bool(
        concrete_file
        and query
        and query not in path_obj.name.lower()
        and Path(query).name.lower() != path_obj.name.lower()
    )
    # Packaged EV.app uniquifies evie-note.txt and only scans one folder level.
    # Append/edit overwrite in Python. Find/open uses Python ranking.
    query_lookup = action in {"open", "read", "search"} and (not concrete_file or mismatched)
    use_helper = (
        live is not None
        and not bool(arguments.get("overwrite"))
        and not query_lookup
        and action not in {"run", "delete"}
    )
    if use_helper:
        from app.ev.computer import _live_command

        result = await _live_command(
            live,
            "file_op",
            payload,
            request_id=request_id,
            timeout=20.0,
        )
        error = str(result.get("error") or "")
        retry_local = error in {"unknown_command", "unsupported", "unknown_action"}
        if error in {"not_found", "ambiguous"} and action in {"open", "read", "search"}:
            retry_local = True
        if not retry_local:
            return result
    if not laptop_files_allowed():
        logger.warning(
            "laptop_files_disabled action=%s live=%s",
            payload.get("action"),
            live is not None,
        )
        return {
            "ok": False,
            "executed": False,
            "verified": False,
            "error": "laptop_files_disabled",
            "spoken": "Local file access is not enabled on this API.",
        }
    return perform_local(arguments)
