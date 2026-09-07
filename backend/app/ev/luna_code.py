"""GPT-5.6 Luna coding brain — Evie talks, Luna edits real projects.

Realtime Mini never receives a shell. This module is the coding loop:

owner goal → select an allowed project → Luna (or an honest offline
heuristic) → jail tools → spoken evidence.

Offline CI stays sacred: with no OpenAI key the heuristic implements a few
clear script/test requests and otherwise reports degraded=true.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from pathlib import Path
from typing import Any

from app.config import settings
from app.ev.code_runtime import (
    CodeJailError,
    list_dir,
    list_projects,
    read_file,
    replace_in_file,
    reset_active_project,
    run_argv,
    search_text,
    select_project,
    set_active_project,
    use_project,
    workspace_root,
    write_file,
)
from app.utils.text import utcnow

logger = logging.getLogger("ev.luna_code")

LUNA_CODE_SYSTEM = """You are Evie's coding brain (Luna). The owner asked Evie to write, edit, or run software in a real project. Mini is only the mouth; you do the work.

Rules:
- Work only through the provided tools. Stay inside the selected project.
- Any language in this repo is in scope (Python, JS/TS, Swift, Go, Rust, Ruby, Java, PHP, …). Use the matching allowlisted runner (python3, node, swift, go, cargo, ruby, java, php). No npm, pip, or shell.
- For an existing repo: list_dir / search, read the relevant slice, then patch with replace_in_file. Do not rewrite a whole file unless it is new or tiny.
- New work may be several files. Create what you need. Prefer the project's existing layout and tests.
- If the owner named a project, it should already be selected. Otherwise list_projects / use_project before editing.
- If a previous job from this session is attached, continue those files. Do not start a new unrelated program unless they asked for one.
- After a meaningful edit, run the cheapest relevant check (pytest, python3, node, cargo test, swift test, go test).
- Take the time you need. Search before guessing. Never claim success the tools did not show.
- Never ask for a raw shell. Never touch secrets, .env files, or paths outside the project.
- When done, answer with a short spoken summary Evie can say aloud: what you wrote, whether it ran, and the folder the file lives in (two or three sentences). Never just name the file.
"""

SPARK_CODE_SYSTEM = """You are Evie's coding brain. The owner asked Evie to write, edit, or run software in a real project. Jail tools are the only actuators; existing TTS is the mouth.

Rules:
- Work only through the provided tools. Stay inside the selected project.
- Any language in this repo is in scope (Python, JS/TS, Swift, Go, Rust, Ruby, Java, PHP, …). Use the matching allowlisted runner (python3, node, swift, go, cargo, ruby, java, php). No npm, pip, or shell.
- For an existing repo: list_dir / search, read the relevant slice, then patch with replace_in_file. Do not rewrite a whole file unless it is new or tiny.
- New work may be several files. Create what you need. Prefer the project's existing layout and tests.
- If the owner named a project, it should already be selected. Otherwise list_projects / use_project before editing.
- If a previous job from this session is attached, continue those files. Do not start a new unrelated program unless they asked for one.
- After a meaningful edit, run the cheapest relevant check (pytest, python3, node, cargo test, swift test, go test).
- Take the time you need. Search before guessing. Never claim success the tools did not show.
- Never ask for a raw shell. Never touch secrets, .env files, or paths outside the project.
- When done, answer with a short spoken summary Evie can say aloud: what you wrote, whether it ran, and the folder the file lives in (two or three sentences). Never just name the file.
- Do not call yourself Luna, Mini, Grok, or DeepSeek.
- If the owner request is a CODING GOAL SLICE, finish only that phase as real multi-file work. Do not ship a hello-world stub when they asked for a professional site, app UI, or calculator.
"""

LUNA_CODE_TOOLS = [
    {
        "type": "function",
        "name": "list_projects",
        "description": "List owner-allowed project roots Evie may edit.",
        "parameters": {
            "type": "object",
            "additionalProperties": False,
            "properties": {},
            "required": [],
        },
    },
    {
        "type": "function",
        "name": "use_project",
        "description": "Switch to an allowed project by name (for example ev).",
        "parameters": {
            "type": "object",
            "additionalProperties": False,
            "properties": {"name": {"type": "string", "minLength": 1, "maxLength": 80}},
            "required": ["name"],
        },
    },
    {
        "type": "function",
        "name": "list_dir",
        "description": "List files in a project-relative directory. Skips node_modules/.git/venv.",
        "parameters": {
            "type": "object",
            "additionalProperties": False,
            "properties": {"path": {"type": "string", "maxLength": 512}},
            "required": [],
        },
    },
    {
        "type": "function",
        "name": "search",
        "description": "Regex search across project source files. Prefer this before guessing paths.",
        "parameters": {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "pattern": {"type": "string", "minLength": 1, "maxLength": 200},
                "path": {"type": "string", "maxLength": 512},
                "glob": {"type": "string", "maxLength": 80},
            },
            "required": ["pattern"],
        },
    },
    {
        "type": "function",
        "name": "read_file",
        "description": "Read a project-relative text file. Use offset/limit for large files.",
        "parameters": {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "path": {"type": "string", "minLength": 1, "maxLength": 512},
                "offset": {"type": "integer", "minimum": 1},
                "limit": {"type": "integer", "minimum": 1, "maximum": 400},
            },
            "required": ["path"],
        },
    },
    {
        "type": "function",
        "name": "replace_in_file",
        "description": "Replace exact text in an existing file. Prefer this over write_file for edits.",
        "parameters": {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "path": {"type": "string", "minLength": 1, "maxLength": 512},
                "old": {"type": "string", "minLength": 1, "maxLength": 24000},
                "new": {"type": "string", "maxLength": 24000},
                "replace_all": {"type": "boolean"},
            },
            "required": ["path", "old", "new"],
        },
    },
    {
        "type": "function",
        "name": "write_file",
        "description": "Create or replace a project-relative text file. Use for new files.",
        "parameters": {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "path": {"type": "string", "minLength": 1, "maxLength": 512},
                "content": {"type": "string", "maxLength": 200000},
            },
            "required": ["path", "content"],
        },
    },
    {
        "type": "function",
        "name": "run_command",
        "description": (
            "Run an allowlisted program in the project. argv only, no shell. "
            "python3, node, ruby, php, java, javac, go, swift, swiftc, cargo, "
            "rustc, pytest, uv run, ruff, mypy, git status/diff/log/show."
        ),
        "parameters": {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "argv": {
                    "type": "array",
                    "minItems": 1,
                    "maxItems": 16,
                    "items": {"type": "string", "maxLength": 2000},
                }
            },
            "required": ["argv"],
        },
    },
]

_PRINT_QUOTED = re.compile(
    r"print(?:s|ing)?\s+['\"]([^'\"]+)['\"]",
    re.IGNORECASE,
)
_HELLO = re.compile(r"\bhello(?:\s+world)?\b", re.IGNORECASE)
_FIB = re.compile(r"\bfibonacci\b", re.IGNORECASE)
_ADD = re.compile(r"\b(?:add(?:s|ing)? two numbers|sum of two)\b", re.IGNORECASE)
_HTML = re.compile(r"\bhtml\b.*\b(?:page|file)\b|\b(?:page|file)\b.*\bhtml\b", re.IGNORECASE)
_RUN_FILE = re.compile(
    r"\brun\b.{0,40}\b([A-Za-z0-9_./-]+\.(py|js|mjs|cjs|rb|go))\b",
    re.IGNORECASE,
)
_RUN_TESTS = re.compile(
    r"\brun (?:the |my |our )?(?:tests|test suite|pytest|unit tests)\b",
    re.IGNORECASE,
)
_JS_ASK = re.compile(r"\b(?:javascript|node(?:\.js)?|js script|\.js\b)", re.IGNORECASE)
_RUBY_ASK = re.compile(r"\b(?:ruby|\.rb\b)", re.IGNORECASE)
_RUNNERS_BY_SUFFIX = {
    "py": ["python3"],
    "js": ["node"],
    "mjs": ["node"],
    "cjs": ["node"],
    "rb": ["ruby"],
    "go": ["go", "run"],
}
_MAX_GOAL_CHARS = 8000
_LIVE_JOB_SECONDS = 240.0
_CHAT_JOB_SECONDS = 300.0
_LAST_CODE_JOBS: dict[str, dict[str, Any]] = {}
_OWNER_JOB_KEY = "owner"
_MAX_RAM_JOBS = 12
_INTERN_STALE_S = 20 * 60
_INTERN_TASKS: set[asyncio.Task] = set()
_DEFER_RE = re.compile(
    r"\b(?:"
    r"overnight|"
    r"while i (?:sleep|am asleep|'m asleep|am gone|'m gone|am out)|"
    r"keep (?:working|going)(?: on (?:this|it|that))?(?: overnight)?|"
    r"work on (?:this|that|it) (?:overnight|in the background|while i.{0,20}sleep)|"
    r"ghost intern|"
    r"finish this later|"
    r"keep coding"
    r")\b",
    re.IGNORECASE,
)
_CODE_FOLLOWUP_RE = re.compile(
    r"\b(?:"
    r"where (?:is|did you (?:save|put|write)|was) (?:it|that|the (?:file|script)|hello\.py|greet\.py)|"
    r"where(?:'s| is) (?:the )?(?:file|script)(?: saved)?|"
    r"where did you (?:put|save|write) (?:it|that)|"
    r"what did you (?:write|create|make|code|save)|"
    r"did it work|"
    r"does it work|"
    r"did you finish|"
    r"are you done|"
    r"did you keep going|"
    r"how did overnight go|"
    r"is (?:overnight|the intern) (?:done|finished)|"
    r"did the intern|"
    r"what happened overnight with (?:the )?(?:code|script|job|intern)|"
    r"what(?:'s| is) in (?:the )?(?:file|script)|"
    r"(?:show|tell) me (?:the )?(?:file|script|path|code|folder)"
    r")\b",
    re.IGNORECASE,
)
_CODE_CONTINUE_RE = re.compile(
    r"\b(?:"
    r"run (?:it|that|them|those)|"
    r"try (?:it|that)|"
    r"test (?:it|that)|"
    r"run the tests|"
    r"add (?:a |an |the )?(?:unit )?tests?|"
    r"write (?:a |an |the )?tests?|"
    r"change (?:it|that|the (?:threshold|mark|limit|number|file|script|function|code)|\d+)|"
    r"make (?:it|that|the \w+) |"
    r"make (?:the )?(?:pass mark|threshold|cutoff|limit) |"
    r"fix (?:it|that)|"
    r"update (?:it|that)|"
    r"rename (?:it|that)|"
    r"now (?:run|test|fix|change|add|make)|"
    r"also (?:add|run|test|write|make)|"
    r"and then (?:run|test|add)|"
    r"and a test|"
    r"same thing in|"
    r"do that in|"
    r"in (?:python|javascript|js|ruby|go|swift)(?: instead)?"
    r")\b",
    re.IGNORECASE,
)
_SOFT_ASK_RE = re.compile(
    r"\b(?:"
    r"can you (?:please )?(?:code|write|make|create|build|whip up)|"
    r"could you (?:please )?(?:code|write|make|create|build)|"
    r"would you (?:please )?(?:code|write|make|create|build)|"
    r"i (?:need|want) (?:a |an |some )?(?:small )?(?:python |js |javascript )?"
    r"(?:script|program|function|helper|grader|module|test)|"
    r"make (?:me )?(?:a |an )?(?:small )?(?:python |js |javascript )?"
    r"(?:script|program|function|helper|grader|module|tool)|"
    r"whip up |put together |knock out |"
    r"a small (?:python |javascript |js )?(?:script|program|function|helper)|"
    r"code (?:me |something |a |an )"
    r")\b",
    re.IGNORECASE,
)
_SOFT_TASK_RE = re.compile(
    r"\b(?:"
    r"python|javascript|typescript|ruby|swift|golang|rust|java|"
    r"script|program|function|module|class|pytest|grader|"
    r"unit tests?|codebase|repo|\.py|\.js|\.ts|\.go|\.rb|"
    r"grades?|scoring|pass(?:es)? or fail"
    r")\b",
    re.IGNORECASE,
)
_SOFT_THAT_RE = re.compile(
    r"\b(?:that|which)\b.{0,80}\b(?:return|print|check|grade|pass|fail|test|compute|count)\b",
    re.IGNORECASE,
)


def looks_like_code_request(text: str | None) -> bool:
    """High-precision owner phrasing for write/edit/run in a real project."""

    raw = (text or "").strip()
    if not raw or len(raw) > _MAX_GOAL_CHARS:
        return False
    from app.ev.code_studio import looks_like_background_task_ops

    if looks_like_background_task_ops(raw):
        return False
    lowered = raw.lower()
    if re.search(
        r"\b(?:text|message|email|mail|reminder|note to|write mom|write dad)\b",
        lowered,
    ):
        return False
    if re.search(r"\b(?:open|launch|quit|close)\s+(?:cursor|vscode|xcode|terminal)\b", lowered):
        return False
    if re.search(
        r"\b(?:on my desktop|inside my desktop|in my documents|in downloads)\b",
        lowered,
    ):
        return False
    if re.search(r"\b(?:open|launch|quit|close)\s+\S+", lowered) and re.search(
        r"\b(?:type|enter)\b", lowered
    ):
        return False
    return bool(
        re.search(
            r"\b(?:"
            r"write (?:me |us )?(?:a |an |some )?"
            r"(?:python |py |javascript |js |typescript |ts |swift |html |css |"
            r"rust |go |golang |java |ruby |php |kotlin )?"
            r"(?:script|program|function|module|class|app|page|snippet)|"
            r"write (?:me |us )?(?:a |an |some )?"
            r"(?:python |py |javascript |js |typescript |ts |swift |html |css |"
            r"rust |go |java |ruby |php )file|"
            r"create (?:me |a |an )?(?:\w+ ){0,3}"
            r"(?:script|program|function|page|module|class)|"
            r"code (?:this|that|me|a |an )|"
            r"implement |"
            r"refactor |"
            r"patch |"
            r"fix .{0,48}(?:bug|code|script|function|error|test|module)|"
            r"(?:add|write) (?:a |an |the )?(?:unit )?tests?|"
            r"edit (?:this |the |my )?(?:code|file|module|function|class|test)s?|"
            r"edit \S+\.(?:py|swift|js|ts|tsx|go|rs|rb|java|php|kt)|"
            r"update (?:this |the |my )?(?:code|function|module|class|test)s?|"
            r"run (?:this |the |my )?(?:script|tests|test suite|pytest|code|program|file)|"
            r"run \S+\.(?:py|js|ts|go|rs|rb|swift)|"
            r"make (?:me )?(?:a |an )?(?:script|program|function|cli|helper|grader|module)|"
            r"build (?:me )?(?:a |an )?(?:script|function|cli|demo|helper)|"
            r"(?:python|javascript|typescript|java|ruby|rust|golang|swift) "
            r"(?:script|program|function|module|class|file)|"
            r"in (?:the |my |our )?\w{2,32} (?:repo|project|codebase|package)|"
            r"(?:this|the|my) (?:repo|codebase)|"
            r"write .{0,48}hello world|"
            r"coding goal|"
            r"(?:clothing|fashion|boutique|shop) (?:site|website|storefront|ui)|"
            r"build (?:me )?(?:a |an )?(?:professional )?(?:site|website|web app|landing page|dashboard)|"
            r"(?:site|website|web app|landing page) ui|"
            r"from scratch.{0,48}(?:site|website|ui|app)|"
            r"create (?:me |a |an )?.{0,40}(?:app ui|calculator.{0,16}(?:app|ui)|todo app)"
            r")\b",
            lowered,
        )
        or _natural_code_ask(raw)
    )


def _natural_code_ask(raw: str) -> bool:
    """Casual phrasing that still names software, not a note or a text."""

    if not _SOFT_ASK_RE.search(raw):
        return False
    return bool(_SOFT_TASK_RE.search(raw) or _SOFT_THAT_RE.search(raw))


def looks_like_code_continue(text: str | None) -> bool:
    """Short follow-on work against the last coding job: run it, add a test, change that."""

    raw = (text or "").strip()
    if not raw or len(raw) > 240:
        return False
    from app.ev.code_studio import looks_like_background_task_ops

    if looks_like_background_task_ops(raw):
        return False
    lowered = raw.lower()
    if re.search(
        r"\b(?:text|message|email|mail|reminder|note to|write mom|write dad|"
        r"on my desktop|in my documents|in downloads)\b",
        lowered,
    ):
        return False
    if re.search(
        r"\bin (?:the |my |our )?\w{2,32} (?:repo|project|codebase|package)\b",
        lowered,
    ):
        return False
    return bool(_CODE_CONTINUE_RE.search(raw))


def looks_like_code_followup(text: str | None) -> bool:
    """Owner asking where the last coding job went, or whether it ran."""

    raw = (text or "").strip()
    if not raw or looks_like_code_request(raw) or looks_like_code_continue(raw):
        return False
    from app.ev.code_studio import looks_like_background_task_ops

    if looks_like_background_task_ops(raw):
        return False
    return bool(_CODE_FOLLOWUP_RE.search(raw))


def remember_code_job(result: dict[str, Any], *, session_key: str = "owner") -> None:
    key = (session_key or _OWNER_JOB_KEY).strip() or _OWNER_JOB_KEY
    payload = {
        "workspace": str(result.get("workspace") or ""),
        "project": str(result.get("project") or ""),
        "files": [str(item) for item in (result.get("files_changed") or result.get("files") or []) if item],
        "spoken": str(result.get("spoken") or ""),
        "ok": bool(result.get("ok")),
        "goal": str(result.get("goal") or ""),
        "runs": list(result.get("runs") or []),
        "session_key": key,
        "at": utcnow().isoformat(),
    }
    _LAST_CODE_JOBS[key] = payload
    _LAST_CODE_JOBS[_OWNER_JOB_KEY] = payload
    _persist_last_code_job(payload)
    while len(_LAST_CODE_JOBS) > _MAX_RAM_JOBS:
        victim = next((item for item in _LAST_CODE_JOBS if item != _OWNER_JOB_KEY), None)
        if victim is None:
            break
        _LAST_CODE_JOBS.pop(victim, None)


def last_code_job(session_key: str | None = None) -> dict[str, Any] | None:
    key = (session_key or _OWNER_JOB_KEY).strip() or _OWNER_JOB_KEY
    hit = _LAST_CODE_JOBS.get(key)
    if hit:
        return hit
    if key != _OWNER_JOB_KEY:
        return None
    stored = _load_last_code_job()
    if stored:
        _LAST_CODE_JOBS[_OWNER_JOB_KEY] = stored
    return stored


def shared_code_job(session_key: str | None = None) -> dict[str, Any] | None:
    """Session job, then the one-body owner/disk job. Unique keys never leak by themselves."""

    return last_code_job(session_key) or last_code_job(_OWNER_JOB_KEY)


def looks_like_deferred_code(text: str | None) -> bool:
    """Overnight / keep-going work against a real coding job, not breakfast."""

    raw = (text or "").strip()
    if not raw or not _DEFER_RE.search(raw):
        return False
    if looks_like_code_request(raw) or looks_like_code_continue(raw):
        return True
    return bool(re.search(r"\b(?:this|that|it|the (?:script|code|job|file))\b", raw, re.IGNORECASE))


def enqueue_code_intern(goal: str, *, session_key: str = "owner") -> dict[str, Any]:
    request = (goal or "").strip()[:_MAX_GOAL_CHARS]
    from app.ev.code_studio import register_intern_job

    goal_id = register_intern_job(request, session_key=session_key)
    payload = {
        "kind": "intern",
        "goal": request,
        "goal_id": goal_id,
        "session_key": (session_key or _OWNER_JOB_KEY).strip() or _OWNER_JOB_KEY,
        "enqueued_at": utcnow().isoformat(),
    }
    from app.memory.paths import atomic_write_json

    atomic_write_json(_pending_code_path(), payload)
    return payload


def intern_ack_spoken() -> str:
    return "I'm running that in the background."


def intern_busy_spoken() -> str:
    return "I'm running that in the background."


def intern_result_spoken(result: dict[str, Any]) -> str:
    body = str(result.get("spoken") or "").strip()
    files = [str(item) for item in (result.get("files_changed") or result.get("files") or []) if item]
    names = ", ".join(Path(item).name for item in files[-8:]) if files else ""
    if result.get("ok"):
        lead = "The overnight job is done."
        extra = f" {body}" if body and body.lower() not in lead.lower() else ""
        where = f" Files: {names}." if names else ""
        return f"{lead}{extra}{where}".strip()[:700]
    lead = "The overnight job didn't finish cleanly."
    extra = f" {body}" if body else ""
    return f"{lead}{extra}".strip()[:700]


def has_pending_code_intern() -> bool:
    from app.memory.paths import read_json

    pending = read_json(_pending_code_path())
    return bool(pending and str(pending.get("kind") or "") in {"intern", "goal_slice"})


def intern_in_flight() -> bool:
    """Queued or actually running — pending is unlinked while Luna works."""

    if has_pending_code_intern():
        return True
    if any(not task.done() for task in _INTERN_TASKS):
        return True
    from app.memory.paths import read_json

    running = read_json(_running_code_path())
    if running and str(running.get("kind") or "") in {"intern", "goal_slice"}:
        started = running.get("started_at")
        try:
            age = time.time() - float(started)
        except (TypeError, ValueError):
            age = 0.0
        if started is not None and age > _INTERN_STALE_S:
            _running_code_path().unlink(missing_ok=True)
        else:
            return True
    from app.ev.code_studio import load_studio

    studio = load_studio()
    return bool(studio and str(studio.get("status") or "") in {"queued", "running"})


def abort_background_code() -> None:
    """Kill the intern/studio task and drop queued/running markers. Studio JSON is the caller's."""

    for task in list(_INTERN_TASKS):
        if not task.done():
            task.cancel()
    _pending_code_path().unlink(missing_ok=True)
    _running_code_path().unlink(missing_ok=True)


def schedule_background_code_notify(spoken: str) -> None:
    """Speak the finished-job brief now. Does not wait for the owner to ask."""

    text = (spoken or "").strip()
    if not text:
        return
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return
    loop.create_task(_push_background_code_notify(text), name="ev-code-notify")


def flush_background_code_notify() -> None:
    """If a brief is waiting and a live mouth is here, say it without being asked."""

    spoken = peek_code_intern_receipt()
    if not spoken:
        return
    from app.voice.live.layer import active_lives

    if not active_lives():
        return
    schedule_background_code_notify(spoken)


async def _push_background_code_notify(spoken: str) -> None:
    from app.voice.live.layer import active_lives

    for live in active_lives():
        if getattr(live, "_closed", False):
            continue
        try:
            await live._speak_code_receipt(spoken)
            consume_code_intern_receipt()
            return
        except Exception:  # noqa: BLE001 - notify must not kill the intern task
            logger.exception("background code notify failed")


def maybe_enqueue_code_intern(text: str, *, session_key: str = "owner") -> str | None:
    """Queue overnight work and return the spoken ack, or None if this is not intern work."""

    if not looks_like_deferred_code(text):
        return None
    from app.ev.code_studio import load_studio, studio_is_active

    if studio_is_active():
        studio = load_studio() or {}
        return (
            f"I'm already on {studio.get('title') or 'a coding goal'} in the background. "
            "Pause or cancel that first if you want overnight intern on something else."
        )
    last_job = shared_code_job(session_key)
    if not looks_like_code_request(text) and not last_job:
        return None
    goal = expand_code_goal(text, last_job) if last_job else (text or "").strip()[:_MAX_GOAL_CHARS]
    enqueue_code_intern(goal, session_key=_OWNER_JOB_KEY)
    return intern_ack_spoken()


def spawn_pending_code_intern() -> bool:
    """Start overnight drain off the daemon tick. Never blocks the caller."""

    if any(not task.done() for task in _INTERN_TASKS):
        return False
    if not has_pending_code_intern():
        from app.ev.code_studio import load_studio, _enqueue_slice

        studio = load_studio()
        if studio and str(studio.get("status") or "") in {"queued", "running"}:
            _enqueue_slice(studio)
        else:
            return False
    task = asyncio.create_task(drain_pending_code_jobs(), name="ev-code-intern")
    _INTERN_TASKS.add(task)

    def _done(done: asyncio.Task) -> None:
        _INTERN_TASKS.discard(done)
        if done.cancelled():
            return
        exc = done.exception()
        if exc is not None:
            logger.exception("code intern drain failed", exc_info=exc)

    task.add_done_callback(_done)
    return True


async def drain_pending_code_jobs() -> dict[str, Any] | None:
    """Run one intern job, or keep draining studio slices until the goal is parked."""

    from app.memory.paths import atomic_write_json, read_json

    last: dict[str, Any] | None = None
    path = _pending_code_path()
    while True:
        pending = read_json(path)
        kind = str((pending or {}).get("kind") or "")
        if not pending or kind not in {"intern", "goal_slice"}:
            from app.ev.code_studio import load_studio, _enqueue_slice

            studio = load_studio()
            if studio and str(studio.get("status") or "") in {"queued", "running"}:
                _enqueue_slice(studio)
                pending = read_json(path)
                kind = str((pending or {}).get("kind") or "")
            if not pending or kind not in {"intern", "goal_slice"}:
                return last
        goal = str(pending.get("goal") or "").strip()
        if kind == "intern" and not goal:
            path.unlink(missing_ok=True)
            return last
        running = {
            "kind": kind,
            "goal": goal,
            "session_key": str(pending.get("session_key") or _OWNER_JOB_KEY),
            "started_at": time.time(),
            "enqueued_at": pending.get("enqueued_at"),
            "goal_id": pending.get("goal_id"),
        }
        atomic_write_json(_running_code_path(), running)
        path.unlink(missing_ok=True)
        try:
            if kind == "goal_slice":
                from app.ev.code_studio import apply_slice_result, get_job, load_studio, slice_prompt

                studio = get_job(str(pending.get("goal_id") or "")) or load_studio()
                if not studio or str(studio.get("status") or "") in {"paused", "cancelled"}:
                    continue
                slice_goal = slice_prompt(studio)
                if not slice_goal:
                    continue
                try:
                    result = await run_code_job(
                        slice_goal,
                        session_key=str(pending.get("session_key") or _OWNER_JOB_KEY),
                        channel="background",
                    )
                except asyncio.CancelledError:
                    return last
                out = apply_slice_result(studio, result)
                note = str((out or {}).get("_notify") or "").strip()
                if note:
                    schedule_background_code_notify(note)
                last = result
                continue
            try:
                result = await run_code_job(
                    goal,
                    session_key=str(pending.get("session_key") or _OWNER_JOB_KEY),
                    channel="background",
                )
            except asyncio.CancelledError:
                return last
            spoken = intern_result_spoken(result)
            atomic_write_json(
                _ready_code_path(),
                {
                    "ok": bool(result.get("ok")),
                    "spoken": spoken,
                    "at": utcnow().isoformat(),
                },
            )
            from app.ev.code_studio import finish_intern_job

            finish_intern_job(
                str(pending.get("goal_id") or "") or None,
                ok=bool(result.get("ok")),
                spoken=spoken,
            )
            schedule_background_code_notify(spoken)
            last = result
            continue
        finally:
            _running_code_path().unlink(missing_ok=True)


def peek_code_intern_receipt() -> str | None:
    from app.memory.paths import read_json

    data = read_json(_ready_code_path())
    if not data:
        return None
    spoken = str(data.get("spoken") or "").strip()
    return spoken or None


def consume_code_intern_receipt() -> str | None:
    spoken = peek_code_intern_receipt()
    if not spoken:
        return None
    _ready_code_path().unlink(missing_ok=True)
    return spoken


def spoken_intern_followup() -> str | None:
    """Still-going, studio status, or the overnight receipt."""

    from app.ev.code_studio import load_studio, spoken_studio_status

    studio = load_studio()
    if intern_in_flight() or (studio and str(studio.get("status") or "") in {"queued", "running", "paused"}):
        return spoken_studio_status()
    return consume_code_intern_receipt()


def _code_jobs_dir():
    from app.memory.paths import ensure_tree

    return ensure_tree() / "code-jobs"


def _last_code_path():
    return _code_jobs_dir() / "last.json"


def _pending_code_path():
    return _code_jobs_dir() / "pending.json"


def _running_code_path():
    return _code_jobs_dir() / "running.json"


def _ready_code_path():
    return _code_jobs_dir() / "ready.json"


def _persist_last_code_job(payload: dict[str, Any]) -> None:
    from app.memory.paths import atomic_write_json

    try:
        atomic_write_json(_last_code_path(), payload)
    except OSError:
        logger.warning("durable code job persist skipped", exc_info=True)


def _load_last_code_job() -> dict[str, Any] | None:
    from app.memory.paths import read_json

    try:
        stored = read_json(_last_code_path())
    except OSError:
        return None
    if not stored:
        return None
    if not stored.get("workspace") and not stored.get("files"):
        return None
    return stored


def expand_code_goal(request: str, prior: dict[str, Any] | None) -> str:
    """Attach the last job so Luna can continue without a scripted filename."""

    raw = (request or "").strip()
    if not prior or not raw:
        return raw
    files = [str(item) for item in (prior.get("files") or prior.get("files_changed") or []) if item]
    folder = str(prior.get("workspace") or "").strip()
    if not files and not folder:
        return raw
    if not looks_like_code_continue(raw):
        return raw
    prev = str(prior.get("goal") or "").strip()
    prev = re.split(r"\nPrevious coding job:", prev, maxsplit=1)[0].strip()[:400]
    names = ", ".join(files[:8]) or "(none)"
    return (
        f"{raw}\n\nPrevious coding job:\n"
        f"- project: {prior.get('project') or Path(folder).name}\n"
        f"- folder: {folder}\n"
        f"- files: {names}\n"
        f"- last request: {prev}\n"
        "Continue that work. Do not start a new unrelated program unless asked."
    )


def _prior_root(prior: dict[str, Any] | None) -> Path | None:
    if not prior:
        return None
    raw = str(prior.get("workspace") or "").strip()
    if not raw:
        return None
    path = Path(raw).expanduser()
    try:
        resolved = path.resolve()
    except OSError:
        return None
    if resolved.is_dir():
        return resolved
    return None


def _prior_hint(prior: dict[str, Any] | None) -> str:
    if not prior:
        return ""
    files = [str(item) for item in (prior.get("files") or prior.get("files_changed") or []) if item]
    names = ", ".join(files[:8]) or "(none)"
    folder = str(prior.get("workspace") or "").strip()
    return (
        f"Recent coding in this session: {names} in {folder or 'the current project'}. "
        "If this request continues that work, edit those files. "
        "If it is a new program, ignore them.\n"
    )


def spoken_code_followup(text: str, job: dict[str, Any]) -> str:
    files = [str(item) for item in (job.get("files") or job.get("files_changed") or []) if item]
    names = ", ".join(Path(item).name for item in files[:4]) or "the script"
    folder = _speakable_dir(str(job.get("workspace") or ""))
    lowered = (text or "").lower()
    if "work" in lowered:
        runs = job.get("runs") or []
        ran_ok = any(bool(item.get("ok")) or item.get("exit_code") == 0 for item in runs)
        out = _last_stdout(runs)
        if ran_ok and out:
            return f"Yes. I ran {names} and it printed {out}."
        if ran_ok:
            return f"Yes. I ran {names} and it exited cleanly."
        if job.get("ok"):
            return f"I saved {names} in {folder}, but I don't have a verified run."
        return str(job.get("spoken") or "That coding job did not finish.")
    if files and folder:
        return f"{names} is in {folder}."
    return str(job.get("spoken") or "I don't have a saved coding job from this session.")


def _speakable_dir(path: str) -> str:
    raw = (path or "").strip()
    if not raw:
        return "the EV coding folder"
    root = Path(raw).expanduser()
    try:
        resolved = root.resolve()
    except OSError:
        resolved = root
    home = Path.home()
    try:
        rel = resolved.relative_to(home)
    except ValueError:
        return resolved.name or raw
    parts = rel.parts
    if len(parts) >= 3 and parts[:3] == ("Library", "Application Support", "EV"):
        rest = "/".join(parts[3:]) or "code-workspace"
        return f"the EV coding folder {rest}"
    if parts and parts[0] == "Code":
        named = ", ".join(parts[1:3]) or "Code"
        return f"your Code folder, {named}"
    if len(parts) >= 2:
        return f"{parts[-2]}, {parts[-1]}"
    return str(rel) or resolved.name


def _last_stdout(runs: list[Any]) -> str:
    for item in reversed(runs or []):
        if not isinstance(item, dict):
            continue
        out = str(item.get("stdout") or "").strip()
        if out:
            return out.splitlines()[0][:80]
    return ""


def _one_line_goal(goal: str) -> str:
    blob = re.sub(r"\s+", " ", (goal or "").strip())
    blob = re.sub(
        r"^(?:evie,?\s+)?(?:please\s+)?(?:create|write|make)\s+(?:me\s+|a\s+|an\s+|some\s+)?(?:python\s+)?(?:script|program|code|file)\s+(?:where|that|which)\s+",
        "",
        blob,
        flags=re.IGNORECASE,
    )
    return blob[:160].rstrip(" .")


def _is_branching_script(goal: str) -> bool:
    lowered = (goal or "").lower()
    if re.search(
        r"\b(?:if|else|elif|otherwise|gender|boy|girl|female|male)\b",
        lowered,
    ):
        return True
    return len(re.findall(r"\bprints?\b", lowered)) >= 2


def shape_code_spoken(result: dict[str, Any]) -> str:
    """Owner-facing receipt: what, whether it ran, and where it lives."""

    existing = str(result.get("spoken") or "").strip()
    if result.get("ok") is False:
        return existing or "I couldn't finish that coding job."
    workspace = str(result.get("workspace") or "")
    folder = _speakable_dir(workspace)
    files = [str(item) for item in (result.get("files_changed") or []) if item]
    names = ", ".join(Path(item).name for item in files[:3])
    did = _one_line_goal(str(result.get("goal") or ""))
    runs = list(result.get("runs") or [])
    ran_ok = any(bool(item.get("ok")) or item.get("exit_code") == 0 for item in runs)
    out = _last_stdout(runs)
    parts: list[str] = []
    if names:
        parts.append(f"I saved {names} in {folder}")
    elif existing:
        parts.append(existing.rstrip("."))
    else:
        parts.append(f"I finished that in {folder}")
    if did and names and did.lower() not in names.lower():
        parts.append(did)
    if ran_ok and out:
        parts.append(f"I ran it; it printed {out}")
    elif ran_ok:
        parts.append("I ran it and it exited cleanly")
    elif names:
        parts.append("I have not verified a run yet")
    spoken = ". ".join(part.rstrip(".") for part in parts if part).strip()
    return (spoken + ".")[:700]


def _finish_code_job(
    result: dict[str, Any],
    *,
    request: str,
    workspace: str,
    session_key: str,
) -> dict[str, Any]:
    result.setdefault("workspace", workspace)
    result.setdefault("project", Path(workspace).name if workspace else "")
    result.setdefault("goal", request)
    result["spoken"] = shape_code_spoken(result)
    remember_code_job(result, session_key=session_key)
    return result


async def run_code_job(
    goal: str,
    *,
    actor: str = "master",
    channel: str | None = None,
    session_key: str | None = None,
) -> dict[str, Any]:
    """Run one coding job. Always returns spoken evidence; never invents success."""

    request = (goal or "").strip()[:_MAX_GOAL_CHARS]
    if not request:
        return _fail("empty_goal", "Tell me what to write, edit, or run.")
    if not bool(getattr(settings, "code_enabled", True)):
        return _fail("code_disabled", "Coding is turned off in settings.")
    job_key = (session_key or "owner").strip() or "owner"
    prior = last_code_job(job_key)
    continue_work = looks_like_code_continue(request)
    continue_only = continue_work and not looks_like_code_request(request)
    if continue_work and not prior:
        prior = last_code_job(_OWNER_JOB_KEY)
    if continue_only and not prior:
        return _fail("no_last_job", "I don't have a script from this session to continue.")
    luna_goal = expand_code_goal(request, prior if continue_work else None)
    selected = select_project(request)
    prior_root = _prior_root(prior) if continue_work and prior else None
    if prior_root is not None:
        selected = prior_root
    token = set_active_project(selected)
    started = time.monotonic()
    channel_l = (channel or "").lower()
    live = channel_l == "voice" or actor == "voice"
    if channel_l in {"background", "intern"}:
        live = False
        budget = float(getattr(settings, "code_chat_job_seconds", _CHAT_JOB_SECONDS) or _CHAT_JOB_SECONDS)
    elif live:
        budget = float(getattr(settings, "code_live_job_seconds", _LIVE_JOB_SECONDS) or _LIVE_JOB_SECONDS)
    else:
        budget = float(getattr(settings, "code_chat_job_seconds", _CHAT_JOB_SECONDS) or _CHAT_JOB_SECONDS)
    budget = max(30.0, min(budget, 600.0))
    try:
        workspace = str(workspace_root())
        from app.gateway.muse import (
            MUSE_SPARK_PROVIDERS,
            muse_intelligence_active,
            muse_spark_key_loaded,
            muse_spark_model,
        )

        # Code-lane switch: EV_CODE_MODEL naming a Muse Spark model routes
        # code jobs to Spark without flipping the global intelligence lane
        # (chat stays on EV_CHAT_PROVIDER).
        code_model_name = str(getattr(settings, "code_model", None) or "").strip()
        code_wants_spark = code_model_name.lower() in MUSE_SPARK_PROVIDERS or (
            bool(code_model_name) and code_model_name == muse_spark_model()
        )

        spark_on = muse_intelligence_active() or code_wants_spark
        if spark_on:
            if not muse_spark_key_loaded():
                return _finish_code_job(
                    _fail(
                        "spark_unavailable",
                        "Coding intelligence is unavailable: OPENCODE_API_KEY is missing.",
                    ),
                    request=request,
                    workspace=workspace,
                    session_key=job_key,
                )
            model = muse_spark_model()
            try:
                result = await _spark_code_loop(
                    luna_goal,
                    model=model,
                    budget_s=budget,
                    live=live,
                    prior=prior,
                )
                result.setdefault("brain", model)
                result.setdefault("actor", actor)
                result.setdefault("latency_ms", round((time.monotonic() - started) * 1000, 1))
                return _finish_code_job(
                    result, request=request, workspace=workspace, session_key=job_key
                )
            except Exception as exc:  # noqa: BLE001 - coding must fail honest
                logger.warning("luna_code.spark_failed error_type=%s", type(exc).__name__)
                return _finish_code_job(
                    _fail(
                        "spark_unavailable",
                        "Coding intelligence is unavailable.",
                    ),
                    request=request,
                    workspace=workspace,
                    session_key=job_key,
                )
        key = (getattr(settings, "openai_api_key", None) or "").strip()
        model = (
            str(getattr(settings, "code_model", None) or "").strip()
            or str(getattr(settings, "turn_control_model", None) or "").strip()
            or "gpt-5.6-luna"
        )
        if key:
            models = [model]
            fallback = str(getattr(settings, "turn_control_fallback_model", None) or "").strip()
            if fallback and fallback != model:
                models.append(fallback)
            for attempt in models:
                try:
                    result = await _luna_loop(
                        luna_goal,
                        model=attempt,
                        budget_s=budget,
                        live=live,
                        prior=prior,
                    )
                    result.setdefault("brain", attempt)
                    result.setdefault("actor", actor)
                    result.setdefault("latency_ms", round((time.monotonic() - started) * 1000, 1))
                    return _finish_code_job(
                        result, request=request, workspace=workspace, session_key=job_key
                    )
                except Exception as exc:  # noqa: BLE001 - coding must fail honest
                    logger.warning(
                        "luna_code.loop_failed model=%s error_type=%s",
                        attempt,
                        type(exc).__name__,
                    )
                    if attempt != models[-1] and "luna_http_404" in str(exc):
                        continue
        heuristic = _heuristic_job(request, prior=prior)
        heuristic.setdefault("brain", "heuristic" if not key else f"{model}+heuristic")
        heuristic.setdefault("actor", actor)
        heuristic.setdefault("latency_ms", round((time.monotonic() - started) * 1000, 1))
        if heuristic.get("ok"):
            return _finish_code_job(
                heuristic, request=request, workspace=workspace, session_key=job_key
            )
        if not key:
            return _finish_code_job(
                _fail(
                    "luna_unavailable",
                    "I can write clear scripts or run tests offline. "
                    "For a real project edit, Luna needs EV_OPENAI_API_KEY.",
                    extra=heuristic,
                ),
                request=request,
                workspace=workspace,
                session_key=job_key,
            )
        return _finish_code_job(
            _fail(
                "code_incomplete",
                str(heuristic.get("spoken") or "I couldn't finish that coding job honestly."),
                extra=heuristic,
            ),
            request=request,
            workspace=workspace,
            session_key=job_key,
        )
    finally:
        reset_active_project(token)


def execute_code_tool(name: str, arguments: dict[str, Any]) -> dict[str, Any]:
    """Execute one Luna workspace tool. Used by the API loop and tests."""

    args = arguments or {}
    try:
        if name == "list_projects":
            return {"ok": True, "projects": list_projects(), "current": str(workspace_root())}
        if name == "use_project":
            return use_project(str(args.get("name") or ""))
        if name == "list_dir":
            return list_dir(str(args.get("path") or "."))
        if name == "search":
            return search_text(
                str(args.get("pattern") or ""),
                str(args.get("path") or "."),
                glob=str(args.get("glob") or ""),
            )
        if name == "read_file":
            offset = args.get("offset")
            limit = args.get("limit")
            return read_file(
                str(args.get("path") or ""),
                offset=int(offset or 1),
                limit=int(limit) if limit is not None else None,
            )
        if name == "replace_in_file":
            return replace_in_file(
                str(args.get("path") or ""),
                str(args.get("old") or ""),
                str(args.get("new") or ""),
                replace_all=bool(args.get("replace_all")),
            )
        if name == "write_file":
            return write_file(str(args.get("path") or ""), str(args.get("content") or ""))
        if name == "run_command":
            argv = args.get("argv")
            if not isinstance(argv, list) or not argv:
                raise CodeJailError("argv is required")
            return run_argv([str(item) for item in argv])
    except CodeJailError as exc:
        return {"ok": False, "error": "code_jail", "detail": str(exc)}
    return {"ok": False, "error": "unknown_code_tool", "name": name    }


async def _spark_code_loop(
    goal: str,
    *,
    model: str,
    budget_s: float,
    live: bool,
    prior: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Muse Spark coding loop. Jail tools remain the only actuators."""

    from app.gateway.muse_spark import muse_spark_provider, responses_tools_to_chat_tools

    configured = int(getattr(settings, "code_max_steps", 24) or 24)
    max_steps = max(1, min(32, configured))
    if live:
        max_steps = min(20, max_steps)
    projects = list_projects()
    catalog = ", ".join(f"{item['name']}={item['path']}" for item in projects[:12]) or "(none)"
    messages: list[dict[str, Any]] = [
        {"role": "system", "content": SPARK_CODE_SYSTEM},
        {
            "role": "user",
            "content": (
                f"Owner request:\n{goal}\n\n"
                f"Selected project: {workspace_root()}\n"
                f"Allowed projects: {catalog}\n"
                f"{_prior_hint(prior)}"
                "Relative paths only. Search, then patch. New work may be several files. "
                "Use the language this repo already speaks. Run a check before you stop."
            ),
        },
    ]
    files_changed: list[str] = []
    runs: list[dict[str, Any]] = []
    spoken = ""
    deadline = time.monotonic() + max(20.0, budget_s)
    tools = responses_tools_to_chat_tools(LUNA_CODE_TOOLS)
    provider = muse_spark_provider()
    for _step in range(max_steps):
        if time.monotonic() >= deadline:
            break
        data = await provider.complete_raw(messages, tools=tools, model=model)
        choice = ((data.get("choices") or [{}])[0].get("message") or {})
        messages.append(choice)
        calls = choice.get("tool_calls") or []
        text = str(choice.get("content") or "").strip()
        if text:
            spoken = text
        if not calls:
            break
        for call in calls:
            fn = call.get("function") or {}
            name = str(fn.get("name") or "")
            raw_args = fn.get("arguments") or "{}"
            try:
                parsed = json.loads(raw_args) if isinstance(raw_args, str) else dict(raw_args)
            except json.JSONDecodeError:
                parsed = {}
            result = execute_code_tool(name, parsed if isinstance(parsed, dict) else {})
            if name in {"write_file", "replace_in_file"} and result.get("ok"):
                path = str(result.get("path") or "")
                if path and path not in files_changed:
                    files_changed.append(path)
            if name == "run_command":
                runs.append(
                    {
                        "argv": result.get("argv"),
                        "exit_code": result.get("exit_code"),
                        "ok": result.get("ok"),
                        "stdout": (result.get("stdout") or "")[:500],
                        "stderr": (result.get("stderr") or "")[:300],
                    }
                )
            messages.append(
                {
                    "role": "tool",
                    "tool_call_id": str(call.get("id") or ""),
                    "content": json.dumps(result, default=str)[:16_000],
                }
            )
    ok = bool(files_changed or (runs and any(item.get("ok") for item in runs)))
    if not spoken:
        last_out = ""
        for item in reversed(runs):
            last_out = str(item.get("stdout") or "").strip()
            if last_out:
                break
        if files_changed:
            spoken = f"I edited {', '.join(files_changed)} in {workspace_root().name}."
            if last_out:
                spoken = f"{spoken} Output: {last_out[:180]}"
        elif ok:
            spoken = f"I ran that in {workspace_root().name}."
            if last_out:
                spoken = f"{spoken} Output: {last_out[:180]}"
        else:
            spoken = "I couldn't finish a verified coding change."
    return {
        "ok": ok,
        "spoken": spoken[:500],
        "files_changed": files_changed,
        "runs": runs[-6:],
        "brain": model,
        "workspace": str(workspace_root()),
        "degraded": not ok,
    }


async def _luna_loop(
    goal: str,
    *,
    model: str,
    budget_s: float,
    live: bool,
    prior: dict[str, Any] | None = None,
) -> dict[str, Any]:
    from app.gateway.muse import refuse_legacy_cloud_brain

    refuse_legacy_cloud_brain("openai")
    import httpx

    key = (getattr(settings, "openai_api_key", None) or "").strip()
    configured = int(getattr(settings, "code_max_steps", 24) or 24)
    max_steps = max(1, min(32, configured))
    if live:
        max_steps = min(20, max_steps)
    projects = list_projects()
    catalog = ", ".join(f"{item['name']}={item['path']}" for item in projects[:12]) or "(none)"
    conversation: list[dict[str, Any]] = [
        {"role": "system", "content": LUNA_CODE_SYSTEM},
        {
            "role": "user",
            "content": (
                f"Owner request:\n{goal}\n\n"
                f"Selected project: {workspace_root()}\n"
                f"Allowed projects: {catalog}\n"
                f"{_prior_hint(prior)}"
                "Relative paths only. Search, then patch. New work may be several files. "
                "Use the language this repo already speaks. Run a check before you stop."
            ),
        },
    ]
    files_changed: list[str] = []
    runs: list[dict[str, Any]] = []
    spoken = ""
    deadline = time.monotonic() + max(20.0, budget_s)
    http_timeout = float(getattr(settings, "code_http_timeout_seconds", 60.0) or 60.0)
    http_timeout = max(15.0, min(http_timeout, 90.0))
    base = str(getattr(settings, "openai_base_url", None) or "https://api.openai.com/v1").rstrip("/")
    async with httpx.AsyncClient(timeout=http_timeout) as client:
        for _step in range(max_steps):
            if time.monotonic() >= deadline:
                break
            payload = {
                "model": model,
                "input": conversation,
                "tools": LUNA_CODE_TOOLS,
                "reasoning": {"effort": "medium" if live else "high"},
            }
            resp = await client.post(
                f"{base}/responses",
                headers={
                    "Authorization": f"Bearer {key}",
                    "Content-Type": "application/json",
                },
                json=payload,
            )
            if resp.status_code != 200:
                raise RuntimeError(f"luna_http_{resp.status_code}")
            data = resp.json()
            output = data.get("output") or []
            if not isinstance(output, list):
                output = []
            conversation.extend(item for item in output if isinstance(item, dict))
            calls = [
                item
                for item in output
                if isinstance(item, dict) and item.get("type") == "function_call"
            ]
            text = str(data.get("output_text") or "").strip() or _output_text(output)
            if text:
                spoken = text.strip()
            if not calls:
                break
            for call in calls:
                name = str(call.get("name") or "")
                raw_args = call.get("arguments") or "{}"
                try:
                    parsed = json.loads(raw_args) if isinstance(raw_args, str) else dict(raw_args)
                except json.JSONDecodeError:
                    parsed = {}
                result = execute_code_tool(name, parsed if isinstance(parsed, dict) else {})
                if name in {"write_file", "replace_in_file"} and result.get("ok"):
                    path = str(result.get("path") or "")
                    if path and path not in files_changed:
                        files_changed.append(path)
                if name == "run_command":
                    runs.append(
                        {
                            "argv": result.get("argv"),
                            "exit_code": result.get("exit_code"),
                            "ok": result.get("ok"),
                            "stdout": (result.get("stdout") or "")[:500],
                            "stderr": (result.get("stderr") or "")[:300],
                        }
                    )
                conversation.append(
                    {
                        "type": "function_call_output",
                        "call_id": str(call.get("call_id") or ""),
                        "output": json.dumps(result, default=str)[:16_000],
                    }
                )
    ok = bool(files_changed or (runs and any(item.get("ok") for item in runs)))
    if not spoken:
        last_out = ""
        for item in reversed(runs):
            last_out = str(item.get("stdout") or "").strip()
            if last_out:
                break
        if files_changed:
            spoken = f"I edited {', '.join(files_changed)} in {workspace_root().name}."
            if last_out:
                spoken = f"{spoken} Output: {last_out[:180]}"
        elif ok:
            spoken = f"I ran that in {workspace_root().name}."
            if last_out:
                spoken = f"{spoken} Output: {last_out[:180]}"
        else:
            spoken = "I couldn't finish a verified coding change."
    return {
        "ok": ok,
        "spoken": spoken[:500],
        "files_changed": files_changed,
        "runs": runs[-6:],
        "brain": model,
        "workspace": str(workspace_root()),
        "degraded": not ok,
    }


def _heuristic_continue(goal: str, prior: dict[str, Any] | None) -> dict[str, Any] | None:
    """Run or patch the last job when the owner says 'run it' / 'change 50 to 60'."""

    if not prior or not looks_like_code_continue(goal):
        return None
    files = [str(item) for item in (prior.get("files") or prior.get("files_changed") or []) if item]
    lowered = goal.lower()
    wants_run = bool(
        re.search(r"\b(?:run it|run that|run them|try it|try that|test it)\b", lowered)
        or (
            _RUN_TESTS.search(goal)
            and not re.search(r"\b(?:write|create|make|add)\b", lowered)
        )
    )
    if wants_run:
        tests = [item for item in files if Path(item).name.startswith("test_")]
        sources = [item for item in files if item not in tests]
        if tests or _RUN_TESTS.search(goal):
            ran = _run_tests()
            extra = (ran.get("stdout") or ran.get("stderr") or "").strip()[:180]
            return {
                "ok": bool(ran.get("ok")),
                "spoken": f"Ran tests in {workspace_root().name}. {extra}".strip(),
                "files_changed": [],
                "runs": [
                    {
                        "argv": ran.get("argv"),
                        "exit_code": ran.get("exit_code"),
                        "ok": ran.get("ok"),
                    }
                ],
                "brain": "heuristic",
                "degraded": not bool(ran.get("ok")),
            }
        path = (sources[0] if sources else None) or (files[0] if files else None)
        if not path:
            return _fail("no_last_file", "I don't have a file from this session to run.")
        suffix = Path(path).suffix.lstrip(".").lower()
        argv = [*(_RUNNERS_BY_SUFFIX.get(suffix) or ["python3"]), path]
        ran = run_argv(argv)
        extra = (ran.get("stdout") or ran.get("stderr") or "").strip()[:180]
        return {
            "ok": bool(ran.get("ok")),
            "spoken": f"Ran {Path(path).name}. {extra}".strip(),
            "files_changed": [],
            "runs": [
                {
                    "argv": ran.get("argv"),
                    "exit_code": ran.get("exit_code"),
                    "ok": ran.get("ok"),
                    "stdout": ran.get("stdout"),
                }
            ],
            "brain": "heuristic",
            "degraded": not bool(ran.get("ok")),
        }
    nums = re.findall(r"\b(\d+)\b", goal)
    if (
        len(nums) >= 2
        and files
        and re.search(r"\b(?:change|make|set|update)\b", lowered)
    ):
        old, new = nums[0], nums[1]
        if old == new:
            return None
        changed: list[str] = []
        for path in files:
            try:
                result = replace_in_file(path, old, new, replace_all=True)
            except CodeJailError:
                continue
            if result.get("ok"):
                changed.append(str(result.get("path") or path))
        if not changed:
            return None
        spoken = f"Updated {', '.join(Path(item).name for item in changed)} from {old} to {new}."
        payload: dict[str, Any] = {
            "ok": True,
            "spoken": spoken,
            "files_changed": changed,
            "runs": [],
            "brain": "heuristic",
        }
        try:
            if any(Path(item).name.startswith("test_") for item in files):
                ran = _run_tests()
            else:
                path = changed[0]
                suffix = Path(path).suffix.lstrip(".").lower()
                ran = run_argv([*(_RUNNERS_BY_SUFFIX.get(suffix) or ["python3"]), path])
        except CodeJailError:
            return payload
        payload["ok"] = bool(ran.get("ok"))
        payload["degraded"] = not bool(ran.get("ok"))
        payload["runs"] = [
            {
                "argv": ran.get("argv"),
                "exit_code": ran.get("exit_code"),
                "ok": ran.get("ok"),
                "stdout": ran.get("stdout"),
            }
        ]
        extra = (ran.get("stdout") or "").strip()[:80]
        if extra:
            payload["spoken"] = f"{spoken} Output: {extra}"
        return payload
    return None


def _heuristic_job(goal: str, prior: dict[str, Any] | None = None) -> dict[str, Any]:
    lowered = goal.lower()
    try:
        from app.ev.code_studio import try_heuristic_slice

        sliced = try_heuristic_slice(goal)
        if sliced is not None:
            return sliced
        continued = _heuristic_continue(goal, prior)
        if continued is not None:
            return continued
        if _RUN_TESTS.search(goal) and not re.search(
            r"\b(?:write|create|make|add)\b", lowered
        ):
            ran = _run_tests()
            spoken = (
                f"Ran tests in {workspace_root().name}. "
                + (ran.get("stdout") or ran.get("stderr") or "").strip()[:180]
            )
            return {
                "ok": bool(ran.get("ok")),
                "spoken": spoken.strip(),
                "files_changed": [],
                "runs": [{"argv": ran.get("argv"), "exit_code": ran.get("exit_code"), "ok": ran.get("ok")}],
                "brain": "heuristic",
                "degraded": not bool(ran.get("ok")),
            }
        run_match = _RUN_FILE.search(goal)
        if run_match and re.search(r"\brun\b", lowered):
            path = run_match.group(1)
            suffix = (run_match.group(2) or "").lower()
            argv = [*( _RUNNERS_BY_SUFFIX.get(suffix) or ["python3"] ), path]
            ran = run_argv(argv)
            spoken = (
                f"Ran {path}. "
                + (ran.get("stdout") or ran.get("stderr") or "").strip()[:180]
            )
            return {
                "ok": bool(ran.get("ok")),
                "spoken": spoken or f"Ran {path}.",
                "files_changed": [],
                "runs": [{"argv": ran.get("argv"), "exit_code": ran.get("exit_code"), "ok": ran.get("ok")}],
                "brain": "heuristic",
            }
        if _FIB.search(goal):
            path = "fibonacci.py"
            write_file(
                path,
                "def fib(n):\n"
                "    a, b = 0, 1\n"
                "    for _ in range(n):\n"
                "        a, b = b, a + b\n"
                "    return a\n\n"
                "if __name__ == '__main__':\n"
                "    print(fib(8))\n",
            )
            ran = run_argv(["python3", path])
            return _ok_write(path, ran, "Wrote fibonacci.py and ran it.")
        if _ADD.search(goal):
            path = "add.py"
            write_file(
                path,
                "def add(a, b):\n"
                "    return a + b\n\n"
                "if __name__ == '__main__':\n"
                "    print(add(2, 3))\n",
            )
            ran = run_argv(["python3", path])
            return _ok_write(path, ran, "Wrote add.py and ran it.")
        if _HTML.search(goal) or ("html" in lowered and "hello" in lowered):
            path = "index.html"
            write_file(path, "<!doctype html><title>Evie</title><p>hello world</p>\n")
            return {
                "ok": True,
                "spoken": "Wrote index.html with a hello world page.",
                "files_changed": [path],
                "runs": [],
                "brain": "heuristic",
            }
        branched = _heuristic_branching_script(goal)
        if branched is not None:
            return branched
        printed = _PRINT_QUOTED.search(goal)
        wants_print = bool(printed or _HELLO.search(goal) or "print" in lowered)
        if wants_print and not _is_branching_script(goal) and _JS_ASK.search(goal):
            payload = (printed.group(1).strip() if printed else "hello world")[:80]
            path = "hello.js"
            write_file(path, f"console.log({payload!r})\n")
            ran = run_argv(["node", path])
            return _ok_write(path, ran, f"Wrote hello.js that prints {payload!r}.")
        if wants_print and not _is_branching_script(goal) and _RUBY_ASK.search(goal):
            payload = (printed.group(1).strip() if printed else "hello world")[:80]
            path = "hello.rb"
            write_file(path, f"puts {payload!r}\n")
            ran = run_argv(["ruby", path])
            return _ok_write(path, ran, f"Wrote hello.rb that prints {payload!r}.")
        if (
            not _is_branching_script(goal)
            and (printed or _HELLO.search(goal) or ("python" in lowered and "print" in lowered))
        ):
            payload = (printed.group(1).strip() if printed else "hello world")[:80]
            path = "hello.py"
            write_file(path, f"print({payload!r})\n")
            ran = run_argv(["python3", path])
            return _ok_write(path, ran, f"Wrote hello.py that prints {payload!r}.")
    except CodeJailError as exc:
        return _fail("code_jail", str(exc))
    return _fail(
        "needs_luna",
        "That's a real project job. Luna will take it when the OpenAI key is set.",
    )


def _run_tests() -> dict[str, Any]:
    root = workspace_root()
    if (root / "pyproject.toml").exists() or (root / "uv.lock").exists():
        try:
            return run_argv(["uv", "run", "pytest", "-q"])
        except CodeJailError:
            pass
    try:
        return run_argv(["pytest", "-q"])
    except CodeJailError:
        return run_argv(["python3", "-m", "pytest", "-q"])


def _ok_write(path: str, ran: dict[str, Any], spoken: str) -> dict[str, Any]:
    extra = (ran.get("stdout") or "").strip()[:120]
    if extra:
        spoken = f"{spoken} Output: {extra}"
    return {
        "ok": bool(ran.get("ok")),
        "spoken": spoken,
        "files_changed": [path],
        "runs": [{"argv": ran.get("argv"), "exit_code": ran.get("exit_code"), "ok": ran.get("ok"), "stdout": ran.get("stdout")}],
        "brain": "heuristic",
        "degraded": not bool(ran.get("ok")),
    }


def _heuristic_branching_script(goal: str) -> dict[str, Any] | None:
    """Write a real if/else script instead of a fake one-line hello.py."""

    if not _is_branching_script(goal):
        return None
    lowered = goal.lower()
    if "python" not in lowered and "script" not in lowered and "code" not in lowered:
        return None
    boy_msg, girl_msg = _two_print_payloads(goal)
    if boy_msg is None or girl_msg is None:
        return None
    path = "greet.py"
    write_file(
        path,
        "import sys\n"
        "gender = (sys.argv[1] if len(sys.argv) > 1 else 'boy').strip().lower()\n"
        f"if gender in {{'boy', 'male', 'm'}}:\n"
        f"    print({boy_msg!r})\n"
        "else:\n"
        f"    print({girl_msg!r})\n",
    )
    ran_boy = run_argv(["python3", path, "boy"])
    ran_girl = run_argv(["python3", path, "female"])
    ok = bool(ran_boy.get("ok") and ran_girl.get("ok"))
    spoken = (
        f"Wrote {path} so boy prints {boy_msg!r} and female prints {girl_msg!r}."
    )
    extra = (ran_boy.get("stdout") or "").strip()[:80]
    if extra:
        spoken = f"{spoken} Output for boy: {extra}"
    return {
        "ok": ok,
        "spoken": spoken,
        "files_changed": [path],
        "runs": [
            {
                "argv": ran_boy.get("argv"),
                "exit_code": ran_boy.get("exit_code"),
                "ok": ran_boy.get("ok"),
                "stdout": ran_boy.get("stdout"),
            },
            {
                "argv": ran_girl.get("argv"),
                "exit_code": ran_girl.get("exit_code"),
                "ok": ran_girl.get("ok"),
                "stdout": ran_girl.get("stdout"),
            },
        ],
        "brain": "heuristic",
        "degraded": not ok,
    }


def _two_print_payloads(goal: str) -> tuple[str | None, str | None]:
    quoted = re.findall(r"['\"]([^'\"]{2,80})['\"]", goal)
    if len(quoted) >= 2:
        return quoted[0].strip(), quoted[1].strip()
    hellos = re.findall(r"hello(?:\s+miss)?(?:\s+world)?", goal, flags=re.IGNORECASE)
    cleaned = [re.sub(r"\s+", " ", item.strip().lower()) for item in hellos]
    unique: list[str] = []
    for item in cleaned:
        if item and item not in unique:
            unique.append(item)
    if len(unique) >= 2:
        return unique[0], unique[1]
    if re.search(r"\bhello miss world\b", goal, re.IGNORECASE) and re.search(
        r"\bhello world\b", goal, re.IGNORECASE
    ):
        return "hello world", "hello miss world"
    return None, None


def _output_text(output: list[Any]) -> str:
    chunks: list[str] = []
    for item in output:
        if not isinstance(item, dict):
            continue
        if item.get("type") == "message":
            for content in item.get("content") or []:
                if isinstance(content, dict) and content.get("type") == "output_text":
                    chunks.append(str(content.get("text") or ""))
        if item.get("type") == "output_text":
            chunks.append(str(item.get("text") or ""))
    return "\n".join(part for part in chunks if part).strip()


def _fail(error: str, spoken: str, extra: dict[str, Any] | None = None) -> dict[str, Any]:
    payload = {
        "ok": False,
        "degraded": True,
        "error": error,
        "spoken": spoken,
        "files_changed": [],
        "runs": [],
        "next_step": error,
    }
    if extra:
        payload["files_changed"] = list(extra.get("files_changed") or [])
        payload["runs"] = list(extra.get("runs") or [])
    return payload
