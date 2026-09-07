"""Coding studio: long Spark/Luna jobs as goals you can talk over.

Mini remains the mouth. Jail tools remain the hands. A clothing-site-sized
ask is a persistent goal of slices, not a 240s live call that steals the mic.
"""

from __future__ import annotations

import re
import uuid
from typing import Any

from app.utils.text import utcnow

_OWNER = "owner"
_MAX_GOAL = 8000

_LONG_GOAL_RE = re.compile(
    r"\b(?:"
    r"coding goal|"
    r"make (?:me |a |an )?(?:coding |software )goal|"
    r"from scratch.{0,60}(?:site|website|ui|app|clothing)|"
    r"(?:site|website|ui|app|clothing).{0,40}from scratch|"
    r"(?:full|complete|entire) (?:site|website|web app|app|ui)|"
    r"(?:clothing|fashion|boutique|shop|store|apparel) (?:site|website|storefront|ui|web)|"
    r"(?:site|website|web app|landing page|dashboard|storefront) ui|"
    r"build (?:me )?(?:a |an )?(?:professional )?(?:site|website|web app|landing page|dashboard|storefront)|"
    r"make (?:me )?(?:a |an )?(?:professional )?(?:site|website|web app|landing page|clothing)|"
    r"professional(?:-looking)? (?:company )?(?:site|website|ui|web)"
    r")\b",
    re.IGNORECASE,
)
_BUILD_VERB_RE = re.compile(
    r"\b(?:create|make|build|design|ship|code|whip up|put together|want|need|please)\b",
    re.IGNORECASE,
)
_PRODUCT_SURFACE_RE = re.compile(
    r"\b(?:"
    r"app ui|user interface|front[- ]?end|"
    r"web(?:site| site| app| page)|"
    r"landing page|dashboard|storefront|homepage|"
    r"(?:html|css).{0,24}(?:page|site|app)|"
    r"calculator.{0,16}(?:app|ui)|(?:app|ui).{0,16}calculator|"
    r"(?:todo|notes|weather|chat|timer) (?:app|ui)|"
    r"(?:full|complete|entire|professional) (?:site|website|app|ui)|"
    r"from scratch.{0,40}(?:site|website|ui|app)|"
    r"(?:site|website|web app|landing page|dashboard) ui|"
    r"(?:clothing|fashion).{0,20}(?:site|ui|store)"
    r")\b",
    re.IGNORECASE,
)
_SHORT_CODE_RE = re.compile(
    r"\b(?:"
    r"prints? hello|hello world|hello\.py|"
    r"unit tests?|\bpytest\b|"
    r"(?:python |javascript |js |ruby |swift )?(?:script|function|module) that |"
    r"write (?:me |a |an )?(?:python |js |javascript |ruby )?(?:script|function|file)\b|"
    r"run (?:this |the )?(?:script|tests|pytest|file)|"
    r"edit \S+\.(?:py|js|ts|go|rs|rb|swift)"
    r")",
    re.IGNORECASE,
)
_DOING_NOW_RE = re.compile(
    r"\b(?:"
    r"what are you (?:working on|building|coding)|"
    r"what are you doing(?!\s+(?:later|tonight|tomorrow|this|on ))|"
    r"what(?:'s| is) running"
    r")\b",
    re.IGNORECASE,
)
_PROGRESS_RE = re.compile(
    r"\b(?:"
    r"how(?:'s| is) (?:the )?(?:site|app|goal|job|build|coding)|"
    r"what(?:'s| is) left|"
    r"how far along|"
    r"coding status|"
    r"status of (?:the )?(?:site|job|goal|build|coding)|"
    r"progress (?:on|of) (?:the )?(?:site|job|goal|build|coding)"
    r")\b",
    re.IGNORECASE,
)
_STATUS_RE = re.compile(
    rf"(?:{_DOING_NOW_RE.pattern})|(?:{_PROGRESS_RE.pattern})",
    re.IGNORECASE,
)
_PAUSE_RE = re.compile(
    r"\bpause (?:the )?(?:coding|code|site|job|goal|build)\b|"
    r"\bpause coding\b",
    re.IGNORECASE,
)
_STOP_RE = re.compile(
    r"\b(?:stop|cancel|kill) (?:(?:the|that|this|my)\s+)?(?:new |other |another |different |next )?(?:background )?(?:coding|code|site|job|goal|build|task|one)\b|"
    r"\bstop coding\b|"
    r"\bcancel coding\b",
    re.IGNORECASE,
)
_HALT_RE = re.compile(
    r"\b(?:"
    r"(?:stop|cancel|kill|abort|halt|break|revoke)\s+"
    r"(?:(?:the|this|that|my|new|other|another|different)\s+)?"
    r"(?:building|making|working on|coding|doing|running)|"
    r"(?:stop|cancel|kill|abort|halt|break|revoke)\s+"
    r"(?:(?:the|this|that|my|new|other|another|different)\s+)?"
    r"(?:clothing\s+|fashion\s+|dashboard\s+|landing\s+)?"
    r"(?:site|ui|build)|"
    r"(?:break|revoke|abort|stop)\s+"
    r"(?:(?:the|this|that|my|new|other|another|different)\s+)?"
    r"(?:running\s+|background\s+)?"
    r"(?:task|job|request|build|goal|coding)|"
    r"don'?t\s+(?:build|make|continue)"
    r")\b",
    re.IGNORECASE,
)
_CANCEL_RE = re.compile(
    r"\b(?:cancel|kill|drop) (?:the )?(?:coding|code|site|job|goal|build)\b|"
    r"\bcancel coding\b",
    re.IGNORECASE,
)
_DELETE_EXPLICIT_RE = re.compile(
    r"\b(?:delete|remove|clear|forget) (?:(?:the|that|this|my)\s+)?(?:new |other |another |different |next )?(?:background )?(?:task|job|goal|build|coding|studio|one)s?\b",
    re.IGNORECASE,
)
_RUN_RE = re.compile(
    r"\b(?:"
    r"(?:run|start|launch|begin|kick off)\s+"
    r"(?:(?:the|this|that|my|a|an)\s+)?"
    r"(?:new |other |another |different |next |queued |background )*"
    r"(?:background )?"
    r"(?:task|job|goal|build|one)"
    r")\b",
    re.IGNORECASE,
)
_LIST_RE = re.compile(
    r"\b(?:"
    r"(?:what|which)(?:'s| is| are)? (?:the )?(?:background )?(?:tasks|jobs)(?: are there| do you have)?|"
    r"(?:list|show) (?:the |my )?(?:background )?(?:tasks|jobs|queue)|"
    r"what(?:'s| is) (?:in |on )?(?:the )?(?:queue|task board|task list)"
    r")\b",
    re.IGNORECASE,
)
_BARE_NEW_TASK_RE = re.compile(
    r"\b(?:run|start|launch|make|create|add|queue|do)\s+"
    r"(?:(?:a|an|the|some)\s+)?"
    r"(?:new |another |different )?"
    r"(?:background )?(?:task|job)\b",
    re.IGNORECASE,
)
_LAST_RE = re.compile(r"\b(?:last|previous|old|earlier)\b", re.IGNORECASE)
_QUEUED_RE = re.compile(r"\bqueued\b", re.IGNORECASE)
_RUNNING_WORD_RE = re.compile(r"\b(?:running|current|active)\b", re.IGNORECASE)
_DELETE_SHORT_RE = re.compile(
    r"^(?:please\s+)?(?:delete|remove) (?:that|this|it)\s*[.!]?\s*$",
    re.IGNORECASE,
)
_AMBIGUOUS_STOP_RE = re.compile(
    r"^(?:please\s+)?(?:stop|stop that|stop this|cancel that|kill it)\s*[.!]?\s*$",
    re.IGNORECASE,
)
_SPEECH_ONLY_STOP_RE = re.compile(
    r"\bstop (?:talking|speaking)\b",
    re.IGNORECASE,
)
_CASUAL_HALT_RE = re.compile(
    r"^(?:(?:hey |ok(?:ay)? |evie |please )*)?(?:"
    r"stop(?: it| that| this| now)?"
    r"|cancel(?: it| that| this)?"
    r"|kill it"
    r"|enough(?: already)?"
    r"|that'?s enough"
    r"|never mind(?: that)?"
    r"|forget (?:it|that)"
    r"|quit(?: it)?"
    r"|halt"
    r"|abort"
    r"|cut it out"
    r"|no more"
    r")\s*[.!]?\s*$",
    re.IGNORECASE,
)
_SOFT_HALT_RE = re.compile(
    r"\b(?:"
    r"stop (?:doing|making|building|working on) (?:that|this|it)|"
    r"please stop|"
    r"can you stop|"
    r"i don'?t want (?:this|that|it)|"
    r"get rid of (?:this|that|it)"
    r")\b",
    re.IGNORECASE,
)
_RESUME_RE = re.compile(
    r"\b(?:"
    r"(?:resume|unpause) (?:the )?(?:coding|code|site|job|goal|build)|"
    r"keep (?:going|working) on (?:the )?(?:site|goal|job|build|coding)"
    r")\b",
    re.IGNORECASE,
)
_SKIP_RE = re.compile(
    r"\bskip (?:this |the )?(?:phase|slice|step)\b|"
    r"\bskip (?:the )?(foundation|catalog|product|cart|polish|sections|widgets|pages)\b",
    re.IGNORECASE,
)
_REVIEW_RE = re.compile(
    r"\b(?:"
    r"what did you (?:build|ship|write|make)|"
    r"show me (?:the )?(?:site|code|files|pages|build)|"
    r"review (?:the )?(?:site|code|goal|build)|"
    r"what files (?:are there|did you)"
    r")\b",
    re.IGNORECASE,
)
_STEER_RE = re.compile(
    r"\b(?:"
    r"make (?:the |that |it ).{0,48}"
    r"(?:hero|nav|footer|header|button|cart|catalog|darker|lighter|"
    r"minimal|serif|sans|black|white|bigger|smaller|polish)|"
    r"add (?:a |an |the ).{0,40}(?:page|section|button|footer|nav|filter|search)|"
    r"change (?:the |that ).{0,40}(?:color|font|hero|copy|layout|nav)|"
    r"darker|lighter|more polish|more minimal|"
    r"use (?:black|white|serif|sans)"
    r")\b",
    re.IGNORECASE,
)
_NEW_RE = re.compile(
    r"\b(?:new|other|another|different|next)\b",
    re.IGNORECASE,
)
_KIND_WORDS: dict[str, tuple[str, ...]] = {
    "clothing_site": ("clothing", "fashion", "boutique", "apparel", "atelier", "garment"),
    "dashboard": ("dashboard", "admin", "ops"),
    "landing": ("landing", "marketing"),
    "calculator": ("calculator", "calc"),
    "app_ui": ("todo",),
    "intern": ("intern", "overnight"),
}
_LIVE_STATUSES = frozenset({"queued", "running", "paused"})
_DEAD_STATUSES = frozenset({"done", "failed", "cancelled"})
_TITLE_STOP = frozenset({"the", "a", "an", "site", "ui", "page", "goal", "job", "task"})
_NOT_LIFE_GOAL = re.compile(
    r"\b(?:get fit|lose weight|read more|sleep|habit|exercise|gym)\b",
    re.IGNORECASE,
)

_PHASE_PACKS: dict[str, list[tuple[str, str]]] = {
    "clothing_site": [
        ("foundation", "Foundation: homepage, CSS, nav, hero"),
        ("catalog", "Catalog: product grid with real garments"),
        ("product", "Product page: one look, sizes, add to bag"),
        ("cart", "Cart and checkout shell"),
        ("polish", "About, footer, responsive polish"),
    ],
    "landing": [
        ("foundation", "Hero landing page and CSS"),
        ("sections", "Story, proof, and call-to-action sections"),
        ("polish", "Responsive polish and footer"),
    ],
    "dashboard": [
        ("foundation", "Shell, nav, and CSS"),
        ("widgets", "Status cards and a primary table"),
        ("polish", "Empty states and responsive polish"),
    ],
    "generic_site": [
        ("foundation", "Homepage, CSS, and layout"),
        ("pages", "Inner pages the request named"),
        ("polish", "Responsive polish"),
    ],
    "calculator": [
        ("foundation", "Calculator shell, display, and CSS"),
        ("keys", "Keypad and working math"),
        ("polish", "Layout polish and keyboard"),
    ],
    "app_ui": [
        ("foundation", "App shell, CSS, and primary screen"),
        ("actions", "Main actions and a second view"),
        ("polish", "Responsive polish"),
    ],
}


def looks_like_short_code_job(text: str | None) -> bool:
    """One-file script/fix work. Stays on the live jail, not the studio."""

    raw = (text or "").strip()
    if not raw:
        return False
    if looks_like_product_code_goal(raw):
        return False
    return bool(_SHORT_CODE_RE.search(raw))


def looks_like_product_code_goal(text: str | None) -> bool:
    """A multi-file UI/app/site — background it even if they never said background."""

    raw = (text or "").strip()
    if not raw:
        return False
    if not _PRODUCT_SURFACE_RE.search(raw):
        return False
    if _LONG_GOAL_RE.search(raw):
        return True
    return bool(_BUILD_VERB_RE.search(raw))


def looks_like_long_code_goal(text: str | None) -> bool:
    raw = (text or "").strip()
    if not raw or _NOT_LIFE_GOAL.search(raw):
        return False
    if _halt_intent(raw):
        return False
    if re.search(r"\bcreate a goal\b", raw, re.IGNORECASE) and not (
        _LONG_GOAL_RE.search(raw) or looks_like_product_code_goal(raw)
    ):
        return False
    if looks_like_short_code_job(raw):
        return False
    if _LONG_GOAL_RE.search(raw):
        return True
    return looks_like_product_code_goal(raw)


def looks_like_code_status(text: str | None) -> bool:
    raw = (text or "").strip()
    if not raw:
        return False
    if re.search(r"\bhow are you\b", raw, re.IGNORECASE) and not (
        _DOING_NOW_RE.search(raw) or _PROGRESS_RE.search(raw)
    ):
        return False
    return bool(_DOING_NOW_RE.search(raw) or _PROGRESS_RE.search(raw))


def _halt_intent(raw: str) -> bool:
    if _SPEECH_ONLY_STOP_RE.search(raw):
        return False
    return bool(
        _HALT_RE.search(raw)
        or _STOP_RE.search(raw)
        or _CANCEL_RE.search(raw)
        or _DELETE_EXPLICIT_RE.search(raw)
        or _AMBIGUOUS_STOP_RE.search(raw)
        or _DELETE_SHORT_RE.search(raw)
        or _CASUAL_HALT_RE.search(raw)
        or _SOFT_HALT_RE.search(raw)
    )


def job_in_play() -> bool:
    return bool(load_studio() or _intern_busy() or _waiting_jobs())


def looks_like_code_control(text: str | None) -> bool:
    raw = (text or "").strip()
    if not raw:
        return False
    if _SPEECH_ONLY_STOP_RE.search(raw):
        return False
    action = infer_background_task_action(raw)
    if action in {"delete", "run", "pause", "resume", "skip", "stop"}:
        return True
    if job_in_play() and (_CASUAL_HALT_RE.search(raw) or _SOFT_HALT_RE.search(raw)):
        return True
    if _AMBIGUOUS_STOP_RE.search(raw) or _DELETE_SHORT_RE.search(raw):
        return bool(load_studio() or _intern_busy() or _waiting_jobs())
    return False


def looks_like_background_task_ops(text: str | None) -> bool:
    """Any owner ask about the background-task board, not leftover coding history."""

    raw = (text or "").strip()
    if not raw:
        return False
    if looks_like_code_control(raw) or looks_like_code_status(raw) or _LIST_RE.search(raw):
        return True
    return infer_background_task_action(raw) is not None


def infer_background_task_action(text: str | None) -> str | None:
    """Map free-form board talk to an action. Finished clothing never gets a default."""

    raw = (text or "").strip()
    if not raw:
        return None
    if looks_like_long_code_goal(raw):
        return None
    if _SPEECH_ONLY_STOP_RE.search(raw):
        return None
    if _LIST_RE.search(raw):
        return "list"
    if _DELETE_EXPLICIT_RE.search(raw) or _DELETE_SHORT_RE.search(raw):
        return "delete"
    if _RUN_RE.search(raw):
        return "run"
    if _PAUSE_RE.search(raw):
        return "pause"
    if _RESUME_RE.search(raw):
        return "resume"
    if _SKIP_RE.search(raw):
        return "skip"
    if _STOP_RE.search(raw) or _CANCEL_RE.search(raw) or _HALT_RE.search(raw):
        return "stop"
    if job_in_play() and (_CASUAL_HALT_RE.search(raw) or _SOFT_HALT_RE.search(raw)):
        return "stop"
    taskish = bool(
        re.search(
            r"\b(?:background\s+)?(?:task|job)s?\b|"
            r"\b(?:the|that|this|my)\s+(?:new|other|another|different|next)\s+one\b",
            raw,
            re.IGNORECASE,
        )
    )
    named_kind = any(
        re.search(rf"\b{re.escape(word)}\b", raw, re.IGNORECASE)
        for words in _KIND_WORDS.values()
        for word in words
    )
    if re.search(
        r"\b(?:delet(?:e|ed|ing)|remov(?:e|ed|ing)|clear(?:ed|ing)?|forget|forgot|get rid of)\b",
        raw,
        re.IGNORECASE,
    ) and (taskish or named_kind):
        return "delete"
    if re.search(
        r"\b(?:run|running|start(?:ed|ing)?|launch(?:ed|ing)?|kick\s+off|begin)\b",
        raw,
        re.IGNORECASE,
    ) and (taskish or named_kind):
        return "run"
    if re.search(
        r"\b(?:stopp(?:ed|ing)?|cancel(?:led|ed|ing)?|kill(?:ed|ing)?|abort(?:ed|ing)?|"
        r"halt(?:ed|ing)?|revok(?:e|ed|ing))\b",
        raw,
        re.IGNORECASE,
    ) and (taskish or named_kind):
        return "stop"
    return None


def looks_like_code_review(text: str | None) -> bool:
    raw = (text or "").strip()
    if not raw:
        return False
    return bool(_REVIEW_RE.search(raw))


def studio_is_active() -> bool:
    studio = load_studio()
    return bool(studio and str(studio.get("status") or "") in {"queued", "running"})


def looks_like_code_steer(text: str | None) -> bool:
    raw = (text or "").strip()
    if not raw or looks_like_long_code_goal(raw) or looks_like_code_control(raw):
        return False
    studio = load_studio()
    if not studio or str(studio.get("status") or "") not in {"running", "queued", "paused"}:
        return False
    return bool(_STEER_RE.search(raw))


def maybe_handle_code_ops(text: str, *, session_key: str = "owner") -> str | None:
    """Status, control, steer, or start a long goal. None means use the short code path."""

    raw = (text or "").strip()
    if not raw:
        return None
    if looks_like_code_status(raw):
        return _status_if_relevant(raw)
    action = infer_background_task_action(raw)
    if action == "list":
        return spoken_task_board()
    if looks_like_code_review(raw) and (resolve_task(raw, action="review") or last_job()):
        return spoken_studio_review(raw)
    if action in {"delete", "run", "pause", "resume", "skip", "stop"} or looks_like_code_control(raw):
        return apply_code_control(raw)
    if looks_like_code_steer(raw):
        return queue_steer(raw)
    if looks_like_long_code_goal(raw):
        return start_coding_goal(raw, session_key=session_key)
    if _BARE_NEW_TASK_RE.search(raw):
        return run_background_task(raw)
    return None


def _intern_busy() -> bool:
    from app.ev.luna_code import intern_in_flight

    return intern_in_flight()


def _status_if_relevant(raw: str) -> str | None:
    job = resolve_task(raw, action="status")
    busy = bool(job and str(job.get("status") or "") in _LIVE_STATUSES)
    if _DOING_NOW_RE.search(raw):
        if busy:
            return spoken_studio_busy(job)
        if _waiting_jobs():
            return spoken_task_board()
        return None
    if _PROGRESS_RE.search(raw):
        if job:
            return spoken_studio_progress(job)
        return None
    return None


def spoken_studio_busy(job: dict[str, Any] | None = None) -> str:
    studio = job if job is not None else resolve_task("", action="status") or _live_goal()
    if studio and str(studio.get("status") or "") == "paused":
        return f"{studio.get('title') or 'The coding goal'} is paused."
    if studio and str(studio.get("status") or "") == "queued" and not _is_claimed(studio):
        return f"{studio.get('title') or 'That'} is queued in the background."
    if studio and str(studio.get("status") or "") in _LIVE_STATUSES:
        return f"I'm running {studio.get('title') or 'this'} in the background."
    from app.ev.luna_code import intern_in_flight

    if intern_in_flight():
        intern = resolve_task("overnight intern", action="status")
        if intern and str(intern.get("kind") or "") == "intern":
            return f"I'm running {intern.get('title') or 'that'} in the background."
        return "I'm running that in the background."
    return "I'm not running a background task."


def spoken_studio_progress(job: dict[str, Any] | None = None) -> str:
    studio = job if job is not None else _live_goal()
    if not studio:
        return spoken_studio_status()
    title = str(studio.get("title") or "the coding goal")
    status = str(studio.get("status") or "queued")
    if str(studio.get("kind") or "") == "intern":
        if status in _LIVE_STATUSES:
            return f"I'm running {title} in the background."
        if status == "done":
            return str(studio.get("last_spoken") or f"{title} is done.")[:400]
        return f"{title} is {status}."
    phases = list(studio.get("phases") or [])
    done = [item for item in phases if str(item.get("status") or "") == "done"]
    doing = next((item for item in phases if str(item.get("status") or "") == "doing"), None)
    left = [item for item in phases if str(item.get("status") or "") in {"pending", "doing"}]
    files = [str(item) for item in (studio.get("files") or []) if item]
    names = ", ".join(_leaf(item) for item in files[-6:]) if files else "no files yet"
    if status == "paused":
        nxt = str((left[0] if left else {}).get("title") or "the next slice")
        return f"{title} is paused. {len(done)} of {len(phases)} done. Next is {nxt}."
    if status == "cancelled":
        return f"I stopped {title}."
    if status == "done":
        return spoken_completion_summary(studio, ok=True)
    if status == "failed":
        return spoken_completion_summary(studio, ok=False)
    now = str((doing or (left[0] if left else {})).get("title") or "the current slice")
    return f"{title}: {len(done)} of {len(phases)} done, working on {now}. Files: {names}."[:400]


def spoken_studio_status() -> str:
    studio = _live_goal()
    if not studio:
        from app.ev.luna_code import intern_in_flight

        if intern_in_flight():
            return spoken_studio_busy()
        return "I'm not running a background task."
    status = str(studio.get("status") or "queued")
    if status in _LIVE_STATUSES:
        return spoken_studio_busy(studio)
    return spoken_studio_progress(studio)


def spoken_completion_summary(studio: dict[str, Any], *, ok: bool = True) -> str:
    title = str(studio.get("title") or "the coding goal")
    files = [str(item) for item in (studio.get("files") or []) if item]
    names = ", ".join(_leaf(item) for item in files[-12:]) if files else "no files"
    folder = str(studio.get("folder") or "the project")
    phases = list(studio.get("phases") or [])
    shipped = [
        str(item.get("title") or "").split(":")[0].strip()
        for item in phases
        if str(item.get("status") or "") in {"done", "skipped"}
    ]
    work = ", ".join(part for part in shipped if part) or "the planned slices"
    if not ok:
        note = str(studio.get("last_spoken") or "the last slice didn't verify")
        return f"{title} hit a snag after {work}. {note}"[:700]
    return (
        f"Quick brief: {title} is done. I shipped {work}. "
        f"It's all under {folder}/ — {names}. Say if you want a change."
    )[:700]


def spoken_task_board() -> str:
    jobs = list((load_board().get("jobs") or []))
    live = _live_jobs(jobs)
    if not live:
        return "I'm not running a background task."
    active = _live_goal()
    parts: list[str] = []
    for item in live:
        title = str(item.get("title") or "a task")
        if active and str(item.get("id") or "") == str(active.get("id") or ""):
            mark = "running" if str(item.get("status") or "") == "running" else str(item.get("status") or "queued")
        else:
            mark = str(item.get("status") or "queued")
        parts.append(f"{title} ({mark})")
    return ("Background tasks: " + "; ".join(parts) + ".")[:400]


def spoken_studio_review(text: str | None = None) -> str:
    studio = resolve_task(text or "", action="review") or last_job()
    if not studio:
        return "I don't have a coding goal to review yet."
    title = str(studio.get("title") or "the coding goal")
    phases = list(studio.get("phases") or [])
    files = [str(item) for item in (studio.get("files") or []) if item]
    names = ", ".join(_leaf(item) for item in files[-10:]) if files else "no files yet"
    rows = []
    for item in phases:
        state = str(item.get("status") or "pending")
        rows.append(f"{item.get('title')}: {state}")
    body = "; ".join(rows)
    return f"{title} — {str(studio.get('status') or 'queued')}. {body}. Files: {names}."[:400]


def apply_code_control(text: str) -> str:
    studio = resolve_task(text, action="control") or load_studio()
    action = infer_background_task_action(text)
    if action == "delete" or _DELETE_EXPLICIT_RE.search(text) or _DELETE_SHORT_RE.search(text):
        return delete_background_task(text)
    if action == "run" or _RUN_RE.search(text):
        return run_background_task(text)
    if action == "stop" or _STOP_RE.search(text) or _CANCEL_RE.search(text) or _AMBIGUOUS_STOP_RE.search(text) or _HALT_RE.search(text):
        return stop_background_task(text)
    if action == "pause" or _PAUSE_RE.search(text):
        if not studio or str(studio.get("status") or "") not in _LIVE_STATUSES:
            return "There's nothing running to pause."
        if _is_draining(studio):
            from app.ev.luna_code import abort_background_code

            abort_background_code()
        studio["status"] = "paused"
        studio["updated_at"] = utcnow().isoformat()
        save_studio(studio)
        return f"Paused {studio.get('title') or 'the coding goal'}."
    if action == "skip" or _SKIP_RE.search(text):
        return skip_current_slice(text)
    if action == "resume" or _RESUME_RE.search(text):
        job = resolve_task(text, action="resume") or last_job()
        if not job:
            return "There's no coding goal to resume."
        if str(job.get("status") or "") == "done":
            return f"{job.get('title')} is already done."
        if str(job.get("status") or "") == "cancelled":
            return "That goal was stopped. Ask me to start a new one."
        job["status"] = "queued"
        job["updated_at"] = utcnow().isoformat()
        save_studio(job, claim=True)
        _enqueue_slice(job)
        _spawn()
        return f"I'm running {job.get('title')} in the background."
    return "I didn't catch whether to pause, stop, or remove that."


def run_background_task(text: str = "") -> str:
    """Start the job the owner meant — never a finished leftover."""

    job = resolve_task(text, action="run")
    if not job:
        return "There's no new background task to run. Tell me what to build."
    status = str(job.get("status") or "")
    title = str(job.get("title") or "that")
    if status in _DEAD_STATUSES:
        named = _named_job(text, list((load_board().get("jobs") or [])))
        if named is not None:
            return f"{title} already finished."
        return "There's no new background task to run. Tell me what to build."
    if status == "running" and _is_claimed(job):
        return f"I'm already running {title} in the background."
    active = _live_goal()
    if active and str(active.get("id") or "") != str(job.get("id") or ""):
        if _is_draining(active):
            from app.ev.luna_code import abort_background_code

            abort_background_code()
        if str(active.get("status") or "") in {"queued", "running"}:
            active["status"] = "paused"
            active["updated_at"] = utcnow().isoformat()
            save_studio(active)
    job["status"] = "queued"
    job["updated_at"] = utcnow().isoformat()
    save_studio(job, claim=True)
    _enqueue_slice(job)
    _spawn()
    return f"I'm running {title} in the background."


def stop_background_task(text: str = "") -> str:
    job = resolve_task(text, action="stop")
    if not job:
        return "There's nothing running."
    status = str(job.get("status") or "")
    if status in _DEAD_STATUSES:
        if _named_job(text, list((load_board().get("jobs") or []))):
            return f"{job.get('title')} already finished."
        return "There's nothing running."
    if _is_draining(job):
        from app.ev.luna_code import abort_background_code

        abort_background_code()
    job["status"] = "cancelled"
    job["queue"] = []
    job["updated_at"] = utcnow().isoformat()
    save_studio(job)
    return f"Stopped {job.get('title') or 'the background task'}."


def delete_background_task(text: str = "") -> str:
    job = resolve_task(text, action="delete")
    if not job:
        return "There's no background task to remove."
    named = _named_job(text, list((load_board().get("jobs") or [])))
    status = str(job.get("status") or "")
    if status in _DEAD_STATUSES and named is None and not _NEW_RE.search(text or ""):
        return "There's no background task to remove."
    title = str(job.get("title") or "")
    if _is_draining(job):
        from app.ev.luna_code import abort_background_code

        abort_background_code()
    _remove_job(str(job.get("id") or ""))
    try:
        _ready_path().unlink(missing_ok=True)
    except OSError:
        pass
    if title:
        return f"Removed {title}."
    return "There's no background task to remove."


def _remove_job(job_id: str) -> None:
    if not job_id:
        return
    board = load_board()
    jobs = [item for item in (board.get("jobs") or []) if str(item.get("id") or "") != job_id]
    prev = str(board.get("active_id") or "")
    board["jobs"] = jobs
    if prev == job_id:
        board["active_id"] = None
    elif prev and _job_by_id(board, prev) is None:
        board["active_id"] = None
    _write_board(board)
    live = _live_goal_from(board)
    from app.memory.paths import atomic_write_json

    if live:
        atomic_write_json(_studio_path(), live)
    else:
        _studio_path().unlink(missing_ok=True)


def skip_current_slice(text: str) -> str:
    studio = load_studio()
    if not studio:
        return "There's no coding goal to skip."
    if str(studio.get("status") or "") in {"done", "cancelled"}:
        return f"{studio.get('title')} isn't running, so there's nothing to skip."
    phases = list(studio.get("phases") or [])
    named = None
    hit = _SKIP_RE.search(text or "")
    token = (hit.group(1) if hit and hit.lastindex else "") or ""
    token = token.lower().strip()
    if token and token not in {"phase", "slice", "step"}:
        named = next(
            (
                item
                for item in phases
                if token in str(item.get("id") or "").lower()
                or token in str(item.get("title") or "").lower()
            ),
            None,
        )
    current = named or next(
        (item for item in phases if str(item.get("status") or "") == "pending"),
        None,
    )
    if current is None:
        return "Nothing left to skip — this slice is already in flight. Pause if you want me to stop."
    current["status"] = "skipped"
    current["note"] = "skipped by owner"
    studio["phases"] = phases
    studio["updated_at"] = utcnow().isoformat()
    left = [item for item in phases if str(item.get("status") or "") == "pending"]
    if left:
        studio["status"] = "queued"
        save_studio(studio)
        _enqueue_slice(studio)
        _spawn()
        return f"Skipped {current.get('title')}. Next is {left[0].get('title')}."
    studio["status"] = "done"
    save_studio(studio)
    _clear_pending()
    return f"Skipped {current.get('title')}. {studio.get('title')} is done."


def queue_steer(text: str) -> str:
    studio = load_studio()
    if not studio:
        return "I'm not on a coding goal to steer."
    notes = list(studio.get("steering") or [])
    note = " ".join(text.split())[:240]
    if note:
        notes.append(note)
        studio["steering"] = notes[-8:]
        studio["updated_at"] = utcnow().isoformat()
        save_studio(studio)
    return f"I'll fold that into the next slice of {studio.get('title')}."


def start_coding_goal(
    text: str,
    *,
    session_key: str = "owner",
    extra_queue: list[dict[str, Any]] | None = None,
) -> str:
    if _halt_intent(text or ""):
        return stop_background_task(text)
    existing = load_studio()
    status = str((existing or {}).get("status") or "")
    kind = classify_goal_kind(text)
    folder = folder_for_kind(kind)
    phases = [
        {"id": key, "title": title, "status": "pending", "files": [], "note": ""}
        for key, title in _PHASE_PACKS.get(kind, _PHASE_PACKS["generic_site"])
    ]
    studio = {
        "id": f"goal-{uuid.uuid4().hex[:10]}",
        "title": title_for_kind(kind, text),
        "request": (text or "").strip()[:_MAX_GOAL],
        "kind": kind,
        "folder": folder,
        "mode": "goal",
        "status": "queued",
        "phases": phases,
        "files": [],
        "steering": [],
        "queue": [],
        "session_key": (session_key or _OWNER).strip() or _OWNER,
        "created_at": utcnow().isoformat(),
        "updated_at": utcnow().isoformat(),
        "last_spoken": "",
    }
    extras = list(extra_queue or [])
    if existing and status in _LIVE_STATUSES:
        save_studio(studio, claim=False)
        for item in extras:
            extra_kind = str(item.get("kind") or classify_goal_kind(str(item.get("request") or "")))
            extra = {
                **studio,
                "id": f"goal-{uuid.uuid4().hex[:10]}",
                "title": str(item.get("title") or title_for_kind(extra_kind, str(item.get("request") or ""))),
                "request": str(item.get("request") or "")[:_MAX_GOAL],
                "kind": extra_kind,
                "folder": folder_for_kind(extra_kind),
                "status": "queued",
                "phases": [
                    {"id": key, "title": title, "status": "pending", "files": [], "note": ""}
                    for key, title in _PHASE_PACKS.get(extra_kind, _PHASE_PACKS["generic_site"])
                ],
                "created_at": utcnow().isoformat(),
                "updated_at": utcnow().isoformat(),
            }
            save_studio(extra, claim=False)
        return f"Queued {studio['title']} after {existing.get('title')}."
    if _intern_busy():
        save_studio(studio, claim=False)
        for item in extras:
            extra_kind = str(item.get("kind") or classify_goal_kind(str(item.get("request") or "")))
            extra = {
                **studio,
                "id": f"goal-{uuid.uuid4().hex[:10]}",
                "title": str(item.get("title") or title_for_kind(extra_kind, str(item.get("request") or ""))),
                "request": str(item.get("request") or "")[:_MAX_GOAL],
                "kind": extra_kind,
                "folder": folder_for_kind(extra_kind),
                "status": "queued",
                "created_at": utcnow().isoformat(),
                "updated_at": utcnow().isoformat(),
            }
            save_studio(extra, claim=False)
        return f"Queued {studio['title']} after the overnight job."
    try:
        _ready_path().unlink(missing_ok=True)
    except OSError:
        pass
    save_studio(studio, claim=True)
    for item in extras:
        extra_kind = str(item.get("kind") or classify_goal_kind(str(item.get("request") or "")))
        extra = {
            **studio,
            "id": f"goal-{uuid.uuid4().hex[:10]}",
            "title": str(item.get("title") or title_for_kind(extra_kind, str(item.get("request") or ""))),
            "request": str(item.get("request") or "")[:_MAX_GOAL],
            "kind": extra_kind,
            "folder": folder_for_kind(extra_kind),
            "status": "queued",
            "created_at": utcnow().isoformat(),
            "updated_at": utcnow().isoformat(),
        }
        save_studio(extra, claim=False)
    _enqueue_slice(studio)
    _spawn()
    return f"I'm running {studio['title']} in the background."


def classify_goal_kind(text: str) -> str:
    lowered = (text or "").lower()
    if re.search(r"\b(?:clothing|fashion|boutique|apparel|garment|shop|storefront)\b", lowered):
        return "clothing_site"
    if re.search(r"\bcalculator\b", lowered):
        return "calculator"
    if re.search(r"\b(?:dashboard|admin|ops center)\b", lowered):
        return "dashboard"
    if re.search(r"\b(?:landing page|marketing site|hero)\b", lowered):
        return "landing"
    if re.search(r"\b(?:todo|notes|weather|chat|timer) (?:app|ui)\b", lowered):
        return "app_ui"
    if re.search(r"\b(?:site|website|web app|ui|app)\b", lowered):
        return "generic_site"
    return "generic_site"


def title_for_kind(kind: str, text: str) -> str:
    if kind == "clothing_site":
        return "clothing site UI"
    if kind == "dashboard":
        return "dashboard UI"
    if kind == "landing":
        return "landing page"
    if kind == "calculator":
        return "calculator UI"
    if kind == "app_ui":
        lowered = (text or "").lower()
        hit = re.search(r"\b((?:todo|notes|weather|chat|timer) (?:app|ui))\b", lowered)
        return hit.group(1) if hit else "app UI"
    blob = " ".join((text or "").split())[:48].rstrip(" .")
    return blob or "coding goal"


def folder_for_kind(kind: str) -> str:
    return {
        "clothing_site": "atelier",
        "landing": "landing",
        "dashboard": "board",
        "generic_site": "site",
        "calculator": "calc",
        "app_ui": "app",
    }.get(kind, "site")


def slice_prompt(studio: dict[str, Any]) -> str:
    fresh = get_job(str((studio or {}).get("id") or "")) or studio
    if str(fresh.get("status") or "") in {"paused", "cancelled"}:
        return ""
    studio = fresh
    phases = list(studio.get("phases") or [])
    index = next(
        (i for i, item in enumerate(phases) if str(item.get("status") or "") in {"pending", "doing"}),
        0,
    )
    phase = phases[index] if phases else {"id": "slice", "title": "Next slice"}
    phase["status"] = "doing"
    studio["status"] = "running"
    studio["phases"] = phases
    save_studio(studio)
    done = [str(item.get("title") or "") for item in phases[:index]]
    left = [str(item.get("title") or "") for item in phases[index + 1 :]]
    files = ", ".join(str(item) for item in (studio.get("files") or [])[-12:]) or "(none yet)"
    steering = "; ".join(str(item) for item in (studio.get("steering") or [])[-4:]) or "(none)"
    folder = str(studio.get("folder") or "site")
    return (
        f"CODING GOAL SLICE {index + 1}/{max(1, len(phases))}\n"
        f"Kind: {studio.get('kind')}\n"
        f"Folder: {folder}/\n"
        f"Phase: {phase.get('title')}\n"
        f"Owner request: {studio.get('request')}\n"
        f"Already done: {'; '.join(done) or '(none)'}\n"
        f"Later (do not do now): {'; '.join(left) or '(none)'}\n"
        f"Files so far: {files}\n"
        f"Steering from the owner: {steering}\n\n"
        "Ship this phase only, as a professional multi-file result inside that folder. "
        "HTML+CSS (and a little JS if the phase needs it). No npm, no placeholders like lorem if you can write real copy. "
        "Search what already exists, then write or patch. Run the cheapest check you can (open isn't a check; python3/node on a tiny verify script is). "
        "When the phase is actually in the files, stop. Leave later phases for the next slice."
    )


def apply_slice_result(studio: dict[str, Any], result: dict[str, Any]) -> dict[str, Any]:
    from app.memory.paths import atomic_write_json

    fresh = get_job(str((studio or {}).get("id") or ""))
    if fresh is None:
        return studio
    if str(fresh.get("status") or "") in {"paused", "cancelled"}:
        return fresh
    studio = fresh
    phases = list(studio.get("phases") or [])
    doing = next((item for item in phases if str(item.get("status") or "") == "doing"), None)
    files = [str(item) for item in (result.get("files_changed") or result.get("files") or []) if item]
    merged = list(studio.get("files") or [])
    for item in files:
        if item not in merged:
            merged.append(item)
    studio["files"] = merged
    studio["last_spoken"] = str(result.get("spoken") or "")[:400]
    studio["updated_at"] = utcnow().isoformat()
    studio["steering"] = []
    ok = bool(result.get("ok"))
    if doing is not None:
        doing["files"] = files
        doing["note"] = str(result.get("spoken") or "")[:200]
        doing["status"] = "done" if ok else "failed"
    left = [item for item in phases if str(item.get("status") or "") == "pending"]
    failed = doing is not None and not ok
    if failed:
        studio["status"] = "failed"
        summary = spoken_completion_summary(studio, ok=False)
        save_studio({key: value for key, value in studio.items() if key != "_notify"})
        studio["_notify"] = summary
        atomic_write_json(
            _ready_path(),
            {
                "ok": False,
                "spoken": summary,
                "at": utcnow().isoformat(),
            },
        )
        return studio
    if left:
        studio["status"] = "queued"
        save_studio(studio)
        _enqueue_slice(studio)
        return studio
    studio["status"] = "done"
    studio["queue"] = []
    summary = spoken_completion_summary(studio, ok=True)
    save_studio({key: value for key, value in studio.items() if key != "_notify"})
    studio["_notify"] = summary
    atomic_write_json(
        _ready_path(),
        {
            "ok": True,
            "spoken": summary,
            "at": utcnow().isoformat(),
        },
    )
    return studio


def try_heuristic_slice(goal: str) -> dict[str, Any] | None:
    """Offline / test path: actually write the phase files for known kinds."""

    if "CODING GOAL SLICE" not in (goal or ""):
        return None
    kind_match = re.search(r"Kind:\s*(\w+)", goal)
    folder_match = re.search(r"Folder:\s*([A-Za-z0-9_-]+)", goal)
    phase_match = re.search(r"Phase:\s*(.+)", goal)
    kind = (kind_match.group(1) if kind_match else "generic_site").strip()
    folder = (folder_match.group(1) if folder_match else folder_for_kind(kind)).strip("/")
    phase_title = (phase_match.group(1) if phase_match else "").split("\n")[0].strip().lower()
    from app.ev.code_runtime import write_file

    written = _write_kind_phase(kind, folder, phase_title)
    if not written:
        return None
    names = ", ".join(written)
    return {
        "ok": True,
        "spoken": f"Shipped {phase_title or 'this slice'} as {names}.",
        "files_changed": written,
        "runs": [],
        "brain": "heuristic",
    }


def load_studio() -> dict[str, Any] | None:
    """The live coding goal only. Finished jobs stay on the board, not here."""

    return _live_goal()


def last_job() -> dict[str, Any] | None:
    jobs = list((load_board().get("jobs") or []))
    return jobs[-1] if jobs else None


def get_job(job_id: str | None) -> dict[str, Any] | None:
    return _job_by_id(load_board(), str(job_id or ""))


def save_studio(studio: dict[str, Any], *, claim: bool | None = None) -> None:
    from app.memory.paths import atomic_write_json

    job = {key: value for key, value in dict(studio or {}).items() if key != "_notify"}
    if not job.get("id"):
        return
    board = load_board()
    jobs = list(board.get("jobs") or [])
    replaced = False
    for index, item in enumerate(jobs):
        if str(item.get("id") or "") == str(job.get("id") or ""):
            jobs[index] = job
            replaced = True
            break
    if not replaced:
        jobs.append(job)
    jobs = jobs[-24:]
    prev = str(board.get("active_id") or "") or None
    job_id = str(job.get("id") or "")
    kind = str(job.get("kind") or "")
    status = str(job.get("status") or "")
    active = prev
    if kind != "intern":
        if claim is True and status in _LIVE_STATUSES:
            active = job_id
        elif job_id == str(prev or ""):
            if status in _DEAD_STATUSES:
                active = None
        elif claim is False:
            pass
        elif not prev and status in _LIVE_STATUSES:
            active = job_id
    board["jobs"] = jobs
    board["active_id"] = active
    _write_board(board)
    live = _live_goal_from(board)
    if live:
        atomic_write_json(_studio_path(), live)
    else:
        _studio_path().unlink(missing_ok=True)
    if kind != "intern" and status in _DEAD_STATUSES:
        _drop_job_markers(job_id)
    if status == "done" and kind != "intern" and not board.get("active_id"):
        nxt = next(
            (
                item
                for item in jobs
                if str(item.get("kind") or "") != "intern"
                and str(item.get("status") or "") == "queued"
            ),
            None,
        )
        if nxt:
            board["active_id"] = str(nxt.get("id") or "") or None
            _write_board(board)
            atomic_write_json(_studio_path(), nxt)
            _enqueue_slice(nxt)
            _spawn()


def _recompute_active_id(jobs: list[dict[str, Any]]) -> str | None:
    for want in ("running", "paused", "queued"):
        for item in jobs:
            if str(item.get("kind") or "") == "intern":
                continue
            if str(item.get("status") or "") == want:
                return str(item.get("id") or "") or None
    return None


def load_board() -> dict[str, Any]:
    from app.memory.paths import read_json

    data = read_json(_board_path())
    if isinstance(data, dict) and isinstance(data.get("jobs"), list):
        return data
    return _migrate_legacy_studio()


def register_intern_job(goal: str, *, session_key: str = "owner") -> str:
    job_id = f"intern-{uuid.uuid4().hex[:10]}"
    title = intern_title(goal)
    job = {
        "id": job_id,
        "title": title,
        "request": (goal or "").strip()[:_MAX_GOAL],
        "kind": "intern",
        "folder": "",
        "mode": "intern",
        "status": "queued",
        "phases": [],
        "files": [],
        "steering": [],
        "queue": [],
        "session_key": (session_key or _OWNER).strip() or _OWNER,
        "created_at": utcnow().isoformat(),
        "updated_at": utcnow().isoformat(),
        "last_spoken": "",
    }
    board = load_board()
    jobs = list(board.get("jobs") or [])
    jobs.append(job)
    board["jobs"] = jobs[-24:]
    _write_board(board)
    return job_id


def finish_intern_job(goal_id: str | None, *, ok: bool, spoken: str) -> None:
    board = load_board()
    job = _job_by_id(board, str(goal_id or ""))
    if job is None:
        live = [
            item
            for item in (board.get("jobs") or [])
            if str(item.get("kind") or "") == "intern"
            and str(item.get("status") or "") in _LIVE_STATUSES
        ]
        job = live[-1] if live else None
    if job is None:
        return
    job["status"] = "done" if ok else "failed"
    job["last_spoken"] = (spoken or "")[:400]
    job["updated_at"] = utcnow().isoformat()
    save_studio(job)


def intern_title(goal: str) -> str:
    blob = " ".join((goal or "").split())[:48].rstrip(" .")
    return blob or "overnight job"


def resolve_task(text: str, *, action: str = "control") -> dict[str, Any] | None:
    """Pick the job the owner meant. Finished work never wins an unnamed new/generic ask."""

    raw = (text or "").strip()
    board = load_board()
    jobs = list(board.get("jobs") or [])
    live = _live_jobs(jobs)
    named = _named_job(raw, jobs)
    active = _live_goal_from(board)
    intern_live = [
        item
        for item in live
        if str(item.get("kind") or "") == "intern"
    ]
    if named is not None:
        if _NEW_RE.search(raw) and str(named.get("status") or "") in _DEAD_STATUSES:
            others = [item for item in live if str(item.get("id") or "") != str(named.get("id") or "")]
            if others:
                return others[-1]
            if action in {"run", "delete", "stop"}:
                return None
        return named
    if _NEW_RE.search(raw):
        if action == "run":
            waiting = [
                item
                for item in live
                if not active or str(item.get("id") or "") != str(active.get("id") or "")
            ]
            if waiting:
                return waiting[-1]
            return None
        return live[-1] if live else None
    if _QUEUED_RE.search(raw):
        queued = [item for item in live if str(item.get("status") or "") == "queued"]
        return queued[-1] if queued else None
    if _RUNNING_WORD_RE.search(raw) and action != "status":
        running = [item for item in live if str(item.get("status") or "") == "running"]
        return running[-1] if running else active
    if _LAST_RE.search(raw) and action in {"review", "delete", "status"}:
        return jobs[-1] if jobs else None
    if action == "review":
        return active or (jobs[-1] if jobs else None)
    if action == "run":
        waiting = [
            item
            for item in live
            if not active or str(item.get("id") or "") != str(active.get("id") or "")
        ]
        if waiting:
            return waiting[-1]
        return None
    if action == "status":
        if active:
            return active
        if intern_live:
            return intern_live[-1]
        return None
    if action == "stop":
        if active:
            return active
        if intern_live:
            return intern_live[-1]
        return None
    if action == "delete":
        if active:
            return active
        if intern_live:
            return intern_live[-1]
        if live:
            return live[-1]
        return None
    if active:
        return active
    if intern_live:
        return intern_live[-1]
    if live:
        return live[-1]
    return None


def _named_job(raw: str, jobs: list[dict[str, Any]]) -> dict[str, Any] | None:
    lowered = (raw or "").lower()
    kind = None
    for key, words in _KIND_WORDS.items():
        if any(re.search(rf"\b{re.escape(word)}\b", lowered) for word in words):
            kind = key
            break
    if kind:
        hits = [item for item in jobs if str(item.get("kind") or "") == kind]
        live_hits = [item for item in hits if str(item.get("status") or "") in _LIVE_STATUSES]
        if live_hits:
            return live_hits[-1]
        return hits[-1] if hits else None
    for item in reversed(jobs):
        title = str(item.get("title") or "").lower().strip()
        if title and title in lowered:
            return item
        tokens = [
            token
            for token in re.findall(r"[a-z0-9]+", title)
            if token not in _TITLE_STOP and len(token) > 2
        ]
        if tokens and all(token in lowered for token in tokens[:2]):
            return item
    return None


def _live_goal() -> dict[str, Any] | None:
    return _live_goal_from(load_board())


def _live_goal_from(board: dict[str, Any]) -> dict[str, Any] | None:
    jobs = list(board.get("jobs") or [])
    aid = str(board.get("active_id") or "")
    if not aid:
        return None
    hit = _job_by_id(board, aid)
    if (
        hit
        and str(hit.get("status") or "") in _LIVE_STATUSES
        and str(hit.get("kind") or "") != "intern"
    ):
        return hit
    return None


def _live_jobs(jobs: list[dict[str, Any]] | None = None) -> list[dict[str, Any]]:
    rows = jobs if jobs is not None else list((load_board().get("jobs") or []))
    return [item for item in rows if str(item.get("status") or "") in _LIVE_STATUSES]


def _waiting_jobs() -> list[dict[str, Any]]:
    active = _live_goal()
    aid = str((active or {}).get("id") or "")
    return [
        item
        for item in _live_jobs()
        if str(item.get("id") or "") != aid
    ]


def _is_claimed(job: dict[str, Any] | None) -> bool:
    if not job:
        return False
    active = _live_goal()
    return bool(active and str(active.get("id") or "") == str(job.get("id") or ""))


def _job_by_id(board: dict[str, Any], job_id: str) -> dict[str, Any] | None:
    if not job_id:
        return None
    for item in board.get("jobs") or []:
        if str(item.get("id") or "") == job_id:
            return item
    return None


def _next_live_goal_id(jobs: list[dict[str, Any]], *, exclude: str = "") -> str | None:
    for item in jobs:
        if str(item.get("id") or "") == exclude:
            continue
        if str(item.get("status") or "") in _LIVE_STATUSES and str(item.get("kind") or "") != "intern":
            return str(item.get("id") or "") or None
    return None


def _write_board(board: dict[str, Any]) -> None:
    from app.memory.paths import atomic_write_json

    atomic_write_json(_board_path(), board)


def _migrate_legacy_studio() -> dict[str, Any]:
    from app.memory.paths import read_json

    old = read_json(_studio_path())
    if not old or not old.get("id"):
        board = {"active_id": None, "jobs": []}
        _write_board(board)
        return board
    jobs = [old]
    session_key = str(old.get("session_key") or _OWNER)
    for item in list(old.get("queue") or []):
        kind = str(item.get("kind") or classify_goal_kind(str(item.get("request") or "")))
        jobs.append(
            {
                "id": f"goal-{uuid.uuid4().hex[:10]}",
                "title": str(item.get("title") or title_for_kind(kind, str(item.get("request") or ""))),
                "request": str(item.get("request") or "")[:_MAX_GOAL],
                "kind": kind,
                "folder": folder_for_kind(kind),
                "mode": "goal",
                "status": "queued",
                "phases": [
                    {"id": key, "title": title, "status": "pending", "files": [], "note": ""}
                    for key, title in _PHASE_PACKS.get(kind, _PHASE_PACKS["generic_site"])
                ],
                "files": [],
                "steering": [],
                "queue": [],
                "session_key": session_key,
                "created_at": utcnow().isoformat(),
                "updated_at": utcnow().isoformat(),
                "last_spoken": "",
            }
        )
    old["queue"] = []
    live_id = None
    if str(old.get("status") or "") in _LIVE_STATUSES:
        live_id = str(old.get("id") or "")
    else:
        live_id = _next_live_goal_id(jobs, exclude="")
    board = {"active_id": live_id, "jobs": jobs[-24:]}
    _write_board(board)
    return board


def _board_path():
    from app.ev.luna_code import _code_jobs_dir

    return _code_jobs_dir() / "board.json"


def _is_draining(job: dict[str, Any] | None) -> bool:
    if not job:
        return False
    from app.memory.paths import read_json

    from app.ev.luna_code import _pending_code_path, _running_code_path

    job_id = str(job.get("id") or "")
    for path in (_running_code_path(), _pending_code_path()):
        data = read_json(path) or {}
        if not data:
            continue
        if job_id and str(data.get("goal_id") or "") == job_id:
            return True
        if str(job.get("kind") or "") == "intern" and str(data.get("kind") or "") == "intern":
            return True
    return False


def _enqueue_slice(studio: dict[str, Any]) -> None:
    from app.memory.paths import atomic_write_json, read_json

    from app.ev.luna_code import _pending_code_path

    pending = read_json(_pending_code_path())
    if pending and str(pending.get("kind") or "") == "intern":
        # Overnight intern already owns the jail; drain will pick the studio after.
        return
    atomic_write_json(
        _pending_code_path(),
        {
            "kind": "goal_slice",
            "goal_id": studio.get("id"),
            "goal": studio.get("request") or "",
            "session_key": studio.get("session_key") or _OWNER,
            "enqueued_at": utcnow().isoformat(),
        },
    )


def _spawn() -> None:
    import asyncio

    try:
        asyncio.get_running_loop()
    except RuntimeError:
        # No event loop yet; the daemon tick will pick the pending slice up.
        return
    from app.ev.luna_code import spawn_pending_code_intern

    spawn_pending_code_intern()


def _drop_job_markers(job_id: str) -> None:
    if not job_id:
        return
    from app.memory.paths import read_json

    from app.ev.luna_code import _pending_code_path, _running_code_path

    for path in (_pending_code_path(), _running_code_path()):
        data = read_json(path) or {}
        if str(data.get("goal_id") or "") == job_id:
            path.unlink(missing_ok=True)


def _clear_pending() -> None:
    from app.ev.luna_code import _pending_code_path

    _pending_code_path().unlink(missing_ok=True)


def _studio_path():
    from app.ev.luna_code import _code_jobs_dir

    return _code_jobs_dir() / "studio.json"


def _ready_path():
    from app.ev.luna_code import _ready_code_path

    return _ready_code_path()


def _leaf(path: str) -> str:
    return str(path).rstrip("/").rsplit("/", 1)[-1]


def _write_kind_phase(kind: str, folder: str, phase_title: str) -> list[str]:
    from app.ev.code_runtime import write_file

    title = phase_title or ""
    if kind == "clothing_site":
        if "foundation" in title or "homepage" in title or "hero" in title:
            write_file(f"{folder}/styles.css", _ATELIER_CSS)
            write_file(f"{folder}/index.html", _ATELIER_INDEX)
            return [f"{folder}/styles.css", f"{folder}/index.html"]
        if "catalog" in title or "grid" in title:
            write_file(f"{folder}/catalog.html", _ATELIER_CATALOG)
            return [f"{folder}/catalog.html"]
        if "product" in title:
            write_file(f"{folder}/product.html", _ATELIER_PRODUCT)
            return [f"{folder}/product.html"]
        if "cart" in title or "checkout" in title:
            write_file(f"{folder}/cart.html", _ATELIER_CART)
            return [f"{folder}/cart.html"]
        write_file(f"{folder}/about.html", _ATELIER_ABOUT)
        return [f"{folder}/about.html"]
    if kind == "calculator":
        if "foundation" in title or "shell" in title or "display" in title:
            write_file(f"{folder}/styles.css", _CALC_CSS)
            write_file(f"{folder}/index.html", _CALC_INDEX)
            return [f"{folder}/styles.css", f"{folder}/index.html"]
        if "key" in title or "math" in title:
            write_file(f"{folder}/app.js", _CALC_JS)
            write_file(f"{folder}/index.html", _CALC_INDEX)
            return [f"{folder}/app.js", f"{folder}/index.html"]
        write_file(f"{folder}/about.html", _CALC_ABOUT)
        return [f"{folder}/about.html"]
    if "foundation" in title or "hero" in title or "homepage" in title:
        write_file(f"{folder}/index.html", _GENERIC_INDEX)
        write_file(f"{folder}/styles.css", _GENERIC_CSS)
        return [f"{folder}/index.html", f"{folder}/styles.css"]
    write_file(f"{folder}/next.html", _GENERIC_NEXT)
    return [f"{folder}/next.html"]


_ATELIER_CSS = """:root{--ink:#14110e;--paper:#f4efe6;--rule:#c9bba8;--accent:#7a1f2b;--mute:#6b6358}
*{box-sizing:border-box}html,body{margin:0;background:var(--paper);color:var(--ink);
font-family:Palatino,Georgia,serif}a{color:inherit;text-decoration:none}
header,footer{display:flex;justify-content:space-between;align-items:center;
padding:1.25rem 8vw;border-bottom:1px solid var(--rule)}
footer{border:0;border-top:1px solid var(--rule);font-size:.85rem;color:var(--mute)}
nav{display:flex;gap:1.5rem;letter-spacing:.12em;text-transform:uppercase;font-size:.72rem}
.hero{padding:12vh 8vw 8vh;max-width:40rem}
.hero p{color:var(--mute);line-height:1.55}
.cta{display:inline-block;margin-top:1.5rem;padding:.7rem 1.2rem;background:var(--ink);color:var(--paper);
letter-spacing:.14em;text-transform:uppercase;font-size:.72rem}
.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(220px,1fr));gap:1.5rem;padding:8vh 8vw}
.card{border:1px solid var(--rule);padding:1.25rem;min-height:16rem;display:flex;flex-direction:column;justify-content:flex-end}
.card strong{font-size:1.1rem}.price{color:var(--accent);margin-top:.4rem}
.product{display:grid;grid-template-columns:1fr 1fr;gap:3rem;padding:8vh 8vw}
.swatch{height:22rem;background:linear-gradient(160deg,#2b211c,#7a1f2b 55%,#c9bba8)}
.bag{margin-top:1rem;padding:.8rem 1.2rem;background:var(--accent);color:var(--paper);border:0;letter-spacing:.12em;text-transform:uppercase}
@media(max-width:800px){.product{grid-template-columns:1fr}header,nav{flex-wrap:wrap}}
"""

_ATELIER_INDEX = """<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8"/>
  <meta name="viewport" content="width=device-width, initial-scale=1"/>
  <title>Atelier Noir</title>
  <link rel="stylesheet" href="styles.css"/>
</head>
<body>
  <header>
    <a href="index.html"><strong>Atelier Noir</strong></a>
    <nav>
      <a href="catalog.html">Collection</a>
      <a href="about.html">Maison</a>
      <a href="cart.html">Bag</a>
    </nav>
  </header>
  <section class="hero">
    <p>Autumn 2026</p>
    <h1>Cloth with a pulse. Made to be worn hard and kept.</h1>
    <p>A small house. Wool, silk, and hardware that lasts. No seasonal junk.</p>
    <a class="cta" href="catalog.html">See the collection</a>
  </section>
  <footer>
    <span>Atelier Noir — private rooms, public cloth</span>
    <span>Made for Evie&rsquo;s desk</span>
  </footer>
</body>
</html>
"""

_ATELIER_CATALOG = """<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8"/>
  <meta name="viewport" content="width=device-width, initial-scale=1"/>
  <title>Collection — Atelier Noir</title>
  <link rel="stylesheet" href="styles.css"/>
</head>
<body>
  <header>
    <a href="index.html"><strong>Atelier Noir</strong></a>
    <nav>
      <a href="catalog.html">Collection</a>
      <a href="about.html">Maison</a>
      <a href="cart.html">Bag</a>
    </nav>
  </header>
  <section class="grid">
    <a class="card" href="product.html"><span>01</span><strong>Noir wool coat</strong><div class="price">INR 42,000</div></a>
    <a class="card" href="product.html"><span>02</span><strong>Ink silk shirt</strong><div class="price">INR 14,800</div></a>
    <a class="card" href="product.html"><span>03</span><strong>Studio trouser</strong><div class="price">INR 18,200</div></a>
    <a class="card" href="product.html"><span>04</span><strong>Hardware belt</strong><div class="price">INR 6,400</div></a>
  </section>
</body>
</html>
"""

_ATELIER_PRODUCT = """<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8"/>
  <meta name="viewport" content="width=device-width, initial-scale=1"/>
  <title>Noir wool coat — Atelier Noir</title>
  <link rel="stylesheet" href="styles.css"/>
</head>
<body>
  <header>
    <a href="index.html"><strong>Atelier Noir</strong></a>
    <nav>
      <a href="catalog.html">Collection</a>
      <a href="cart.html">Bag</a>
    </nav>
  </header>
  <section class="product">
    <div class="swatch" role="img" aria-label="Noir wool coat"></div>
    <div>
      <p>01 — Outer</p>
      <h1>Noir wool coat</h1>
      <p>Italian wool, unlined through the back so it moves. Horn buttons. Made to be the only coat you pack.</p>
      <p class="price">INR 42,000</p>
      <label>Size
        <select><option>S</option><option selected>M</option><option>L</option></select>
      </label>
      <p><a class="cta" href="cart.html">Add to bag</a></p>
    </div>
  </section>
</body>
</html>
"""

_ATELIER_CART = """<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8"/>
  <meta name="viewport" content="width=device-width, initial-scale=1"/>
  <title>Bag — Atelier Noir</title>
  <link rel="stylesheet" href="styles.css"/>
</head>
<body>
  <header>
    <a href="index.html"><strong>Atelier Noir</strong></a>
    <nav><a href="catalog.html">Collection</a></nav>
  </header>
  <section class="hero">
    <h1>Your bag</h1>
    <p>Noir wool coat — M — INR 42,000</p>
    <p>Shipping calculated at the atelier. Checkout is a shell until you ask for payments.</p>
    <a class="cta" href="catalog.html">Keep looking</a>
  </section>
</body>
</html>
"""

_ATELIER_ABOUT = """<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8"/>
  <meta name="viewport" content="width=device-width, initial-scale=1"/>
  <title>Maison — Atelier Noir</title>
  <link rel="stylesheet" href="styles.css"/>
</head>
<body>
  <header>
    <a href="index.html"><strong>Atelier Noir</strong></a>
    <nav>
      <a href="catalog.html">Collection</a>
      <a href="cart.html">Bag</a>
    </nav>
  </header>
  <section class="hero">
    <h1>A house, not a drop.</h1>
    <p>We cut small. We keep the last season in stock. If it is on the rack, it is meant to be worn for years.</p>
  </section>
  <footer><span>Atelier Noir</span><span>Quiet hours honored</span></footer>
</body>
</html>
"""

_GENERIC_CSS = """body{margin:0;font-family:Georgia,serif;background:#f7f4ee;color:#171717}
main{padding:10vh 8vw;max-width:40rem}a{color:#171717}
"""
_GENERIC_INDEX = """<!doctype html><html lang="en"><head><meta charset="utf-8"/><meta name="viewport" content="width=device-width,initial-scale=1"/><title>Studio</title><link rel="stylesheet" href="styles.css"/></head><body><main><h1>Studio</h1><p>Built in slices, in the background.</p><a href="next.html">Continue</a></main></body></html>
"""
_GENERIC_NEXT = """<!doctype html><html lang="en"><head><meta charset="utf-8"/><title>Next</title><link rel="stylesheet" href="styles.css"/></head><body><main><h1>Next</h1><p>Inner page for this slice.</p></main></body></html>
"""

_CALC_CSS = """:root{--ink:#101114;--paper:#f6f3ee;--key:#ece7df;--op:#2c4a6e;--eq:#7a1f2b}
*{box-sizing:border-box}html,body{margin:0;background:var(--paper);color:var(--ink);font-family:ui-sans-serif,system-ui,sans-serif}
main{max-width:22rem;margin:8vh auto;padding:1.25rem;border:1px solid #d9d1c7;border-radius:1.1rem}
.display{min-height:4.2rem;margin-bottom:1rem;padding:.8rem 1rem;background:#fff;border-radius:.7rem;text-align:right;font-size:2rem}
.keys{display:grid;grid-template-columns:repeat(4,1fr);gap:.55rem}button{border:0;border-radius:.7rem;padding:1rem 0;background:var(--key);font-size:1.1rem}
button.op{background:var(--op);color:#fff}button.eq{background:var(--eq);color:#fff;grid-column:span 2}
"""
_CALC_INDEX = """<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8"/>
  <meta name="viewport" content="width=device-width, initial-scale=1"/>
  <title>Desk calc</title>
  <link rel="stylesheet" href="styles.css"/>
</head>
<body>
  <main>
    <p>Desk calc</p>
    <div class="display" id="display" aria-live="polite">0</div>
    <div class="keys">
      <button data-k="C">C</button><button data-k="(">(</button><button data-k=")">)</button><button class="op" data-k="/">÷</button>
      <button data-k="7">7</button><button data-k="8">8</button><button data-k="9">9</button><button class="op" data-k="*">×</button>
      <button data-k="4">4</button><button data-k="5">5</button><button data-k="6">6</button><button class="op" data-k="-">−</button>
      <button data-k="1">1</button><button data-k="2">2</button><button data-k="3">3</button><button class="op" data-k="+">+</button>
      <button data-k="0">0</button><button data-k=".">.</button><button class="eq" data-k="=">=</button>
    </div>
  </main>
  <script src="app.js"></script>
</body>
</html>
"""
_CALC_JS = """const display=document.getElementById("display");
let buf="0";
const show=()=>{display.textContent=buf;};
const push=k=>{
  if(k==="C"){buf="0";show();return;}
  if(k==="="){
    try{const n=Function('"use strict";return ('+buf+')')();buf=String(n);}catch(e){buf="Error";}
    show();return;
  }
  buf=buf==="0"||buf==="Error"?k:buf+k;show();
};
document.querySelectorAll("button[data-k]").forEach(b=>b.addEventListener("click",()=>push(b.getAttribute("data-k"))));
document.addEventListener("keydown",e=>{
  const k=e.key;
  if(/^[0-9.+\\-*/()]$/.test(k)) push(k);
  else if(k==="Enter") push("=");
  else if(k==="Escape"||k==="c"||k==="C") push("C");
});
show();
"""
_CALC_ABOUT = """<!doctype html><html lang="en"><head><meta charset="utf-8"/><title>Desk calc</title><link rel="stylesheet" href="styles.css"/></head><body><main><p><a href="index.html">Back</a></p><h1>A quiet calculator.</h1><p>Click or type. Escape clears.</p></main></body></html>
"""
