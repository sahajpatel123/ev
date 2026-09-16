"""Read-only project literacy: purpose, catalog, aliases, follow-ups.

Mini is the mouth. Jail tools are the eyes. This module never writes files,
never runs npm/pip/shell, and never answers a purpose question with a
directory listing.
"""

from __future__ import annotations

import json
import logging
import re
from pathlib import Path
from typing import Any

from app.ev.code_runtime import (
    GENERIC_PROJECT_NAMES,
    CodeJailError,
    is_sandbox_workspace,
    list_dir,
    list_projects,
    read_file,
    reset_active_project,
    run_argv,
    search_text,
    set_active_project,
    workspace_root,
)

logger = logging.getLogger("ev.code_literacy")

_FILE_DUMP_RE = re.compile(
    r"\b(?!next|node|three|vue)[A-Za-z0-9_-]{2,}\.(?:md|json|tsx?|jsx?|py|swift|toml|yml|yaml|css|html|mjs|cjs)\b",
    re.IGNORECASE,
)
_STOCK_README_RE = re.compile(
    r"bootstrapped with|create-next-app|getting started|deploy on vercel|"
    r"learn more about next\.js",
    re.IGNORECASE,
)
_SKIP_MD_LINE_RE = re.compile(
    r"^(?:>\s*)?(?:inspected from|table of contents|\s*[-*]\s*\[|"
    r"!\[|badge|npm run |yarn |pnpm |```)",
    re.IGNORECASE,
)
_NOT_CODE_RE = re.compile(
    r"\b(?:"
    r"conversation|chats?|email|mail|message|people|photos?|weather|"
    r"sandwich|mom|dad|calendar|reminder"
    r")\b",
    re.IGNORECASE,
)
_HOME_FOLDER_RE = re.compile(
    r"\b(?:on my desktop|inside my desktop|in my documents|in downloads|"
    r"on my laptop|on my mac|on my computer|in my home folder)\b",
    re.IGNORECASE,
)
_HOW_RUN_RE = re.compile(
    r"\b(?:"
    r"how (?:do i |to |can i )?(?:run|start|launch|serve|dev|develop)(?:\s+it|\s+this|\s+that)?"
    r"|how (?:do i |to )get it running"
    r"|what(?:'s| is) the (?:dev |start |run )?command"
    r"|what command (?:do i |to )run"
    r"|npm run"
    r")\b",
    re.IGNORECASE,
)
_STACK_RE = re.compile(
    r"\b(?:"
    r"what(?:'s| is) (?:the |its )?stack"
    r"|what(?:'s| is) it (?:built|written|made) (?:with|in)"
    r"|which (?:framework|language)s?"
    r"|what (?:framework|language)s?"
    r")\b",
    re.IGNORECASE,
)
_STRUCTURE_RE = re.compile(
    r"\b(?:"
    r"how is (?:it |this |.{0,48}?)?(?:structured|organized|laid out|put together)"
    r"|what(?:'s| is) (?:the |its )?(?:structure|layout|shape|architecture)"
    r"|walk me through (?:the )?(?:structure|layout|architecture)"
    r")\b",
    re.IGNORECASE,
)
_GIT_RE = re.compile(
    r"\b(?:"
    r"git status"
    r"|what(?:'s| is) (?:dirty|changed|uncommitted)"
    r"|any (?:uncommitted|dirty) (?:files|changes)"
    r"|uncommitted changes"
    r"|what changed in (?:the |this |my )?(?:repo|project|workspace|code)"
    r")\b",
    re.IGNORECASE,
)
_DEEPER_RE = re.compile(
    r"\b(?:"
    r"go deeper|dig deeper|more detail|more details|"
    r"full (?:overview|rundown)|"
    r"tell me more about (?:it|this|that|the (?:project|repo|workspace|code))|"
    r"explain (?:it |this )?(?:more|further)|"
    r"walk me through (?:it|this)(?! (?:the )?(?:structure|layout|architecture))"
    r")\b",
    re.IGNORECASE,
)
_ANAPHORA_RE = re.compile(
    r"\b(?:it|this|that|those|the (?:project|repo|workspace|codebase|folder|code))\b",
    re.IGNORECASE,
)
_ALIAS_REJECT_RE = re.compile(
    r"\b(?:where|users?|platform|upload|monorepo|people|clothing|garments?|"
    r"that|which|their|from|with|for the)\b",
    re.IGNORECASE,
)
_SEARCH_RE = re.compile(
    r"\b(?:where(?:'s| is)|find|locate|look for)\s+(?:the |a |an |my |our )?"
    r"(?P<needle>.+?)"
    r"(?:"
    r"\s+in\s+(?:the |this |my |our )?(?:code|repo|project|workspace|codebase|it)\b"
    r"|\s+in\s+(?:the |my |our )?(?P<place>[\w-]+)\s+(?:repo|project|workspace|folder|codebase)\b"
    r"|$)",
    re.IGNORECASE,
)
_ALIAS_STOP = frozenset(
    {
        "project",
        "workspace",
        "code",
        "app",
        "next",
        "python",
        "react",
        "owner",
        "small",
        "helper",
        "tests",
        "interactive",
        "experience",
        "module",
        "package",
        "readme",
        "overview",
    }
)
_CARD_CACHE: dict[str, tuple[float, dict[str, Any]]] = {}


def looks_like_code_how_to_run(text: str | None) -> bool:
    return bool(_HOW_RUN_RE.search(text or ""))


def looks_like_code_stack(text: str | None) -> bool:
    return bool(_STACK_RE.search(text or ""))


def looks_like_code_structure(text: str | None) -> bool:
    return bool(_STRUCTURE_RE.search(text or ""))


def looks_like_code_git_health(text: str | None) -> bool:
    return bool(_GIT_RE.search(text or ""))


def looks_like_code_deeper(text: str | None) -> bool:
    return bool(_DEEPER_RE.search(text or ""))


def looks_like_code_search(text: str | None) -> bool:
    raw = (text or "").strip()
    if not raw or _NOT_CODE_RE.search(raw):
        return False
    hit = _SEARCH_RE.search(raw)
    if not hit:
        return False
    needle = _clean_search_needle(hit.group("needle") or "")
    return bool(needle)


def looks_like_code_literacy(text: str | None) -> bool:
    """Follow-ups about a named/sticky Code project: run, stack, where-is, git."""

    raw = (text or "").strip()
    if not raw or _NOT_CODE_RE.search(raw):
        return False
    if _HOME_FOLDER_RE.search(raw) and not _has_code_anchor(raw):
        return False
    from app.ev.code_locate import looks_like_code_info_ask, resolve_code_target

    located = resolve_code_target(raw)
    if looks_like_code_search(raw):
        if _search_has_code_place(raw) or _has_code_anchor(raw):
            return True
        if located is not None:
            return True
        if _sticky_owner_repo() is None:
            return False
        from app.ev.luna_code import live_code_job

        if live_code_job() is None:
            return False
        hit = _SEARCH_RE.search(raw)
        needle = _clean_search_needle(hit.group("needle") if hit else "")
        return _search_ok_on_sticky(needle)
    specific = (
        looks_like_code_how_to_run(raw)
        or looks_like_code_stack(raw)
        or looks_like_code_structure(raw)
        or looks_like_code_git_health(raw)
        or looks_like_code_deeper(raw)
    )
    if specific:
        if _has_code_anchor(raw):
            return True
        if _sticky_owner_repo() is None:
            return False
        if looks_like_code_deeper(raw) and not _ANAPHORA_RE.search(raw):
            from app.ev.luna_code import live_code_job

            return live_code_job() is not None
        return True
    return located is not None and looks_like_code_info_ask(raw)


def spoken_is_file_dump(text: str) -> bool:
    raw = (text or "").strip()
    if not raw:
        return True
    lowered = raw.lower()
    if "top level:" in lowered or "top-level files" in lowered:
        return True
    if re.search(r"\b(?:looked through|i read the)\b", lowered):
        return True
    if lowered.startswith("in ") and (raw.count(",") >= 2 or len(_FILE_DUMP_RE.findall(raw)) >= 2):
        return True
    if len(_FILE_DUMP_RE.findall(raw)) >= 3:
        return True
    has_purpose = bool(
        re.search(
            r"\b(?:is|it's|its|does|built|app|film|site|tool|companion|for|purpose|python|swift|react)\b",
            lowered,
        )
    )
    return (not has_purpose) and ("," in raw or "/" in raw)


def project_name_for_alias(text: str | None) -> str | None:
    """Map 'Sweet Potato' / 'birthday film' onto a Code folder name."""

    lowered = (text or "").lower()
    if not lowered or _NOT_CODE_RE.search(lowered):
        return None
    persisted = _persisted_alias_name(lowered)
    if persisted:
        return persisted
    from app.ev.code_sandbox import alias_project_name

    mapped = alias_project_name(text)
    if mapped:
        return mapped
    ranked: list[tuple[int, str]] = []
    for item in list_projects():
        name = str(item.get("name") or "")
        path = Path(str(item.get("path") or ""))
        if not _catalog_include(name, path):
            continue
        card = project_card(root=path)
        for alias in card.get("aliases") or []:
            token = str(alias or "").strip().lower()
            if not _alias_token_ok(token, name.lower()):
                continue
            if re.search(rf"\b{re.escape(token)}\b", lowered):
                ranked.append((len(token), name))
                break
    if not ranked:
        return None
    ranked.sort(reverse=True)
    return ranked[0][1]


def spoken_purpose_catalog() -> str:
    """Voice catalog: name plus a one-line purpose, not a file dump."""

    ranked: list[tuple[int, str, str]] = []
    for item in list_projects():
        name = str(item.get("name") or "")
        path = Path(str(item.get("path") or ""))
        if not _catalog_include(name, path):
            continue
        card = project_card(root=path)
        blurb = str(card.get("one_liner") or "").strip()
        row = f"{name} — {blurb}" if blurb else name
        ranked.append((_catalog_rank(name, path, card), name, row))
    ranked.sort(key=lambda item: (-item[0], item[1]))
    rows = [row for _score, _name, row in ranked[:8]]
    if not rows:
        return "I don't see any code projects in your Code folder yet."
    extra = f" And {len(ranked) - 8} more." if len(ranked) > 8 else ""
    spoken = "On this laptop: " + "; ".join(rows) + "." + extra
    return spoken[:700]


def literacy_job(goal: str) -> dict[str, Any]:
    """Deterministic spoken follow-up against the active jail. Never writes."""

    raw = (goal or "").strip()
    from app.ev.code_locate import locate_job, needle_names_target, resolve_code_target

    located = resolve_code_target(raw)
    if located is not None and located.kind == "ambiguous":
        return locate_job(located, raw)
    search_hit = looks_like_code_search(raw)
    needle = ""
    if search_hit:
        hit = _SEARCH_RE.search(raw)
        needle = _clean_search_needle(hit.group("needle") if hit else "")
    self_hit = bool(located and needle_names_target(needle, located)) if located is not None else False
    if located is not None and located.rel:
        if search_hit and needle and not self_hit:
            pass
        else:
            return locate_job(located, raw)
    card = project_card()
    if is_sandbox_workspace(workspace_root()):
        return card
    spoken = ""
    kind = "survey"
    if search_hit and not self_hit:
        spoken = _search_spoken(raw, card)
        kind = "search"
    elif looks_like_code_how_to_run(raw):
        spoken = str(card.get("run_spoken") or "")
        kind = "run"
    elif looks_like_code_git_health(raw):
        spoken = _git_health_spoken(card)
        kind = "git"
    elif looks_like_code_stack(raw):
        spoken = str(card.get("stack_spoken") or "")
        kind = "stack"
    elif looks_like_code_structure(raw):
        spoken = str(card.get("structure_spoken") or "")
        kind = "structure"
    elif looks_like_code_deeper(raw):
        spoken = str(card.get("deeper_spoken") or card.get("spoken") or "")
        kind = "deeper"
    else:
        spoken = str(card.get("spoken") or "")
        kind = "survey"
    spoken = (spoken or str(card.get("spoken") or "")).strip()
    if kind == "survey" and spoken_is_file_dump(spoken):
        spoken = str(card.get("spoken") or spoken)
    result = dict(card)
    result["spoken"] = spoken[:700]
    result["brain"] = kind
    result["goal"] = raw
    result["ok"] = True
    result["files_changed"] = []
    return result


def project_card(*, root: Path | None = None) -> dict[str, Any]:
    """Purpose-first card for one allowlisted project. Never writes source files."""

    if root is not None:
        target = root.expanduser()
        try:
            target = target.resolve()
        except OSError:
            target = root
        if target != workspace_root():
            token = set_active_project(target)
            try:
                return project_card()
            finally:
                reset_active_project(token)

    active = workspace_root()
    stamp = _card_stamp(active)
    cached = _CARD_CACHE.get(str(active))
    if cached and cached[0] == stamp:
        return dict(cached[1])

    folder = active.name or "this project"
    if is_sandbox_workspace(active):
        card = {
            "ok": False,
            "spoken": "That's Evie's private coding sandbox, not one of your Code folders.",
            "one_liner": "",
            "aliases": [],
            "stack": "",
            "run_spoken": "",
            "stack_spoken": "",
            "structure_spoken": "",
            "deeper_spoken": "",
            "files_changed": [],
            "runs": [],
            "brain": "survey",
            "degraded": True,
            "purpose_ok": False,
            "workspace": str(active),
            "title": folder,
            "folder": folder,
        }
        _CARD_CACHE[str(active)] = (stamp, card)
        return dict(card)

    entries: list[str] = []
    try:
        listing = list_dir(".")
        entries = [str(item) for item in (listing.get("entries") or []) if item]
    except CodeJailError:
        entries = []
    overview = _safe_read("OVERVIEW.md", limit=80)
    readme = _safe_read("README.md", limit=80)
    agents = _safe_read("AGENTS.md", limit=40)
    package = _safe_read("package.json", limit=120)
    pyproject = _safe_read("pyproject.toml", limit=80)
    layout = _safe_read("src/app/layout.tsx", limit=40) or _safe_read("app/layout.tsx", limit=40)
    pkg_name, pkg_desc, deps, scripts = _package_meta(package)
    py_name, py_desc = _pyproject_meta(pyproject)
    title, layout_desc = _layout_meta(layout)
    what = _markdown_section(overview, ("what it is", "overview", "about"))
    why = _markdown_section(overview, ("purpose",))
    if not what:
        what = _first_markdown_claim(overview)
    if not what and readme and not _STOCK_README_RE.search(readme):
        what = _markdown_section(readme, ("what it is", "overview", "about", "purpose")) or _first_markdown_claim(
            readme
        )
    if not what and agents and not _STOCK_README_RE.search(agents):
        what = _first_markdown_claim(agents)
    if not why:
        why = _markdown_section(readme, ("purpose",)) if readme and not _STOCK_README_RE.search(readme) else ""
    identity = title or pkg_name or py_name or folder
    claim = what or pkg_desc or py_desc or layout_desc or why or _python_module_clue(entries)
    stack = _stack_clause(deps, entries)
    sentences: list[str] = []
    if claim:
        lead = claim.rstrip(".")
        if lead[:1].isupper() and lead.startswith(("A ", "An ", "The ")):
            lead = lead[0].lower() + lead[1:]
        if identity and identity.lower() not in lead.lower():
            sentences.append(f"{identity} is {lead}")
        elif folder.lower() not in lead.lower():
            sentences.append(f"{folder} is {lead}")
        else:
            sentences.append(lead[0].upper() + lead[1:] if lead else lead)
    elif stack:
        sentences.append(f"{folder} is {stack}")
    else:
        where = folder
        sentences.append(f"I opened {where}, but I could not find a written purpose yet.")
    extra = _trim_purpose(why)
    joined = " ".join(sentences).lower()
    if extra and extra.lower() not in joined:
        sentences.append(extra[0].upper() + extra[1:] if extra else extra)
        joined = " ".join(sentences).lower()
    if (
        pkg_desc
        and pkg_desc.lower() not in joined
        and pkg_desc.rstrip(".").lower() != (claim or "").rstrip(".").lower()
    ):
        sentences.append(pkg_desc.rstrip("."))
        joined = " ".join(sentences).lower()
    if (
        stack
        and stack.lower() not in joined
        and not (
            ("next.js" in joined and "next" in stack.lower())
            or ("python" in joined and "python" in stack.lower())
            or ("swift" in joined and "swift" in stack.lower())
        )
    ):
        sentences.append(f"It is {stack}")
    spoken = ". ".join(part.rstrip(".") for part in sentences if part).strip()
    if spoken and not spoken.endswith("."):
        spoken += "."
    spoken = spoken[:700]
    purpose_ok = (
        bool(spoken)
        and "could not find a written purpose" not in spoken.lower()
        and not spoken_is_file_dump(spoken)
    )
    one_liner = _one_liner(identity, claim, stack, folder)
    run_spoken = _run_clause(folder, scripts, entries)
    stack_spoken = _stack_spoken(folder, stack, identity)
    structure_spoken = _structure_clause(folder, entries) or (
        f"{folder} keeps its source at the top level."
    )
    deeper = _deeper_spoken(spoken, structure_spoken, run_spoken, stack_spoken)
    aliases = _aliases_for(title=title, folder=folder, spoken=spoken, desc=pkg_desc or layout_desc)
    card = {
        "ok": True,
        "spoken": spoken,
        "one_liner": one_liner,
        "aliases": aliases,
        "stack": stack,
        "run_spoken": run_spoken,
        "stack_spoken": stack_spoken,
        "structure_spoken": structure_spoken,
        "deeper_spoken": deeper,
        "files_changed": [],
        "runs": [],
        "brain": "survey",
        "degraded": not purpose_ok,
        "purpose_ok": purpose_ok,
        "workspace": str(active),
        "title": identity,
        "folder": folder,
    }
    _CARD_CACHE[str(active)] = (stamp, card)
    _persist_card(card)
    return dict(card)


def _has_code_anchor(raw: str) -> bool:
    lowered = (raw or "").lower()
    if re.search(
        r"\b(?:repo|project|workspace|codebase|code folder|in the code|in this code)\b",
        lowered,
    ):
        return True
    if _persisted_alias_name(lowered):
        return True
    try:
        from app.ev.code_sandbox import alias_project_name

        if alias_project_name(raw):
            return True
    except Exception:  # noqa: BLE001 - map miss must not break literacy routing
        pass
    from app.ev.code_runtime import _mentions_named_project, catalog_project_names

    for name in catalog_project_names():
        if name in GENERIC_PROJECT_NAMES:
            continue
        if _mentions_named_project(lowered, name):
            return True
    return False


def _catalog_include(name: str, path: Path) -> bool:
    if name in GENERIC_PROJECT_NAMES or len(name) < 2:
        return False
    if name.endswith(".git"):
        return False
    return not is_sandbox_workspace(path)


def _catalog_rank(name: str, path: Path, card: dict[str, Any]) -> int:
    score = 0
    if card.get("purpose_ok"):
        score += 20
    if (path / "OVERVIEW.md").is_file():
        score += 15
    aliases = [str(item) for item in (card.get("aliases") or []) if len(str(item)) >= 5]
    score += min(12, len(aliases) * 3)
    title = str(card.get("title") or "").strip()
    folder = name.replace("-", " ").replace("_", " ")
    if title and title.lower() not in {name.lower(), folder.lower()}:
        score += 10
    if str(card.get("one_liner") or "").strip():
        score += 3
    sticky = _sticky_owner_repo()
    try:
        if sticky is not None and sticky.resolve() == path.resolve():
            score += 40
    except OSError:
        pass
    return score


def _search_ok_on_sticky(needle: str) -> bool:
    token = (needle or "").strip()
    if not token:
        return False
    if re.search(r"[a-z][A-Z]|[._/-]", token) or " " in token:
        return True
    return token[:1].islower()


def _search_has_code_place(raw: str) -> bool:
    return bool(
        re.search(
            r"\bin\s+(?:the |this |my |our )?(?:code|repo|project|workspace|codebase|it)\b",
            raw or "",
            re.IGNORECASE,
        )
        or re.search(
            r"\bin\s+(?:the |my |our )?[\w-]+\s+(?:repo|project|workspace|folder|codebase)\b",
            raw or "",
            re.IGNORECASE,
        )
    )


def _persisted_alias_name(lowered: str) -> str | None:
    try:
        from app.memory.paths import ensure_tree, read_json

        path = ensure_tree() / "code-jobs" / "project-cards.json"
        stored = read_json(path) if path.exists() else {}
    except OSError:
        return None
    if not isinstance(stored, dict):
        return None
    ranked: list[tuple[int, str]] = []
    for name, payload in stored.items():
        if not isinstance(payload, dict):
            continue
        for alias in payload.get("aliases") or []:
            token = str(alias or "").strip().lower()
            if not _alias_token_ok(token):
                continue
            if re.search(rf"\b{re.escape(token)}\b", lowered):
                ranked.append((len(token), str(name)))
                break
    if not ranked:
        return None
    ranked.sort(reverse=True)
    return ranked[0][1]


def _sticky_owner_repo() -> Path | None:
    from app.ev.code_runtime import session_sticky_project_path

    sticky = session_sticky_project_path()
    if sticky is None or is_sandbox_workspace(sticky):
        return None
    return sticky


def _clean_search_needle(raw: str) -> str:
    needle = (raw or "").strip().strip("?.!,")
    needle = re.sub(
        r"\s+in\s+(?:the |this |my |our )?(?:code|repo|project|workspace|codebase|it)\s*$",
        "",
        needle,
        flags=re.IGNORECASE,
    )
    needle = re.sub(
        r"\s+in\s+(?:the |my |our )?[\w-]+\s+(?:repo|project|workspace|folder|codebase)\s*$",
        "",
        needle,
        flags=re.IGNORECASE,
    )
    needle = re.sub(r"\s+", " ", needle).strip(" '\"")
    if needle.lower() in {"it", "this", "that", "code", "file", "the code"}:
        return ""
    return needle[:80]


def _search_spoken(goal: str, card: dict[str, Any]) -> str:
    hit = _SEARCH_RE.search(goal or "")
    needle = _clean_search_needle(hit.group("needle") if hit else "")
    folder = str(card.get("folder") or workspace_root().name)
    if not needle:
        return f"Tell me what to find in {folder}."
    try:
        from app.ev.code_sandbox import lookup_in_project

        mapped = lookup_in_project(needle, workspace_root())
    except Exception:  # noqa: BLE001 - map miss falls through to grep
        mapped = []
    if mapped:
        rel = str(mapped[0].get("rel") or mapped[0].get("name") or needle)
        extra = f" There are {len(mapped) - 1} more matches." if len(mapped) > 1 else ""
        return f"In {folder}, {needle} is in {rel}.{extra}"[:700]
    pattern = f"(?i){re.escape(needle)}"
    hits: list[dict[str, Any]] = []
    try:
        for glob in ("*.tsx", "*.ts", "*.jsx", "*.js", "*.py", "*.swift", "*.vue"):
            found = search_text(pattern, ".", glob=glob, max_hits=8)
            hits = [item for item in (found.get("hits") or []) if isinstance(item, dict)]
            if hits:
                break
        if not hits:
            found = search_text(pattern, ".", max_hits=8)
            hits = [item for item in (found.get("hits") or []) if isinstance(item, dict)]
    except CodeJailError:
        return f"I couldn't search {folder} for {needle}."
    if not hits:
        return f"I don't see {needle} in {folder}."

    def _hit_rank(item: dict[str, Any]) -> tuple[int, int]:
        path = str(item.get("path") or "").lower()
        if path.endswith((".tsx", ".ts", ".jsx", ".js", ".py", ".swift")):
            return (0, len(path))
        if "src/" in path or path.startswith("src/"):
            return (1, len(path))
        if path.endswith(".md") or "overview" in path or "readme" in path:
            return (3, len(path))
        return (2, len(path))

    hits.sort(key=_hit_rank)
    top = hits[0]
    path = str(top.get("path") or "").strip() or "the project"
    line = str(top.get("text") or "").strip()
    line = re.sub(r"\s+", " ", line)
    if len(line) > 120:
        line = line[:117].rsplit(" ", 1)[0] + "…"
    extra = f" There are {len(hits) - 1} more matches." if len(hits) > 1 else ""
    if line:
        return f"In {folder}, {needle} is in {path}: {line}{extra}"[:700]
    return f"In {folder}, {needle} is in {path}.{extra}"[:700]


def _git_health_spoken(card: dict[str, Any]) -> str:
    folder = str(card.get("folder") or workspace_root().name)
    try:
        ran = run_argv(["git", "status", "--short"])
    except CodeJailError:
        return f"I can't read git status in {folder}."
    text = str(ran.get("stdout") or "").strip()
    if not ran.get("ok") and ran.get("exit_code") not in {0, None}:
        return f"I couldn't read git status in {folder}."
    lines = [ln for ln in text.splitlines() if ln.strip()]
    if not lines:
        return f"{folder} is clean — no uncommitted changes."
    counts: dict[str, int] = {}
    names: list[str] = []
    for line in lines:
        rel = _git_relpath(line)
        if not rel:
            continue
        names.append(rel)
        parts = Path(rel).parts
        if len(parts) >= 2:
            top = parts[0]
            if top not in {".", ".."}:
                counts[top] = counts.get(top, 0) + 1
    if len(lines) <= 4:
        listed = ", ".join(names[:4])
        return f"{folder} has {len(lines)} uncommitted files: {listed}."[:700]
    if counts:
        top = max(counts, key=lambda name: counts[name])
        return f"{folder} has {len(lines)} uncommitted files, mostly under {top}."
    return f"{folder} has {len(lines)} uncommitted files."


def _git_relpath(line: str) -> str:
    raw = (line or "").rstrip("\n")
    rel = (
        raw[3:]
        if len(raw) >= 3 and raw[2] in {" ", "\t"}
        else re.sub(r"^..\s+", "", raw)
    )
    rel = rel.strip().strip('"')
    if " -> " in rel:
        rel = rel.split(" -> ", 1)[-1]
    return rel


def _one_liner(identity: str, claim: str, stack: str, folder: str) -> str:
    raw = (claim or stack or identity or folder).strip().rstrip(".")
    if identity and identity.lower() not in raw.lower() and not raw.lower().startswith(identity.lower()):
        raw = f"{identity}, {raw}" if raw else identity
    raw = re.sub(r"\s+", " ", raw)
    if len(raw) > 90:
        raw = raw[:90].rsplit(" ", 1)[0]
    return raw


def _run_clause(folder: str, scripts: dict[str, str], entries: list[str]) -> str:
    names = {Path(item.rstrip("/")).name.lower() for item in entries}
    if scripts.get("dev"):
        extra = " I cannot run npm myself."
        if scripts.get("test"):
            extra = " Tests are npm test — I cannot run npm from here."
        return f"From {folder}, start it with npm run dev.{extra}"
    if scripts.get("start"):
        return f"From {folder}, start it with npm start. I cannot run npm myself."
    if "pyproject.toml" in names or any(item.endswith(".py") for item in entries):
        if any(
            Path(item.rstrip("/")).name.startswith("test_") or Path(item.rstrip("/")).name.lower() == "tests"
            for item in entries
        ):
            return f"From {folder}, run the tests with pytest. I can run pytest in the jail."
        return f"From {folder}, run the Python files with python3. I can do that in the jail."
    if "package.json" in names:
        return f"I see a Node project in {folder}, but no start script I can read. I cannot run npm."
    return f"I don't see a start command in {folder} yet."


def _stack_spoken(folder: str, stack: str, identity: str) -> str:
    who = identity or folder
    if stack:
        return f"{who} is {stack}."
    return f"I don't have a clear stack reading for {folder} yet."


def _structure_clause(folder: str, entries: list[str]) -> str:
    names = {Path(item.rstrip("/")).name.lower() for item in entries}
    layers: list[str] = []
    if "src" in names:
        inner: list[str] = []
        try:
            nested = list_dir("src")
            inner = [Path(str(item).rstrip("/")).name.lower() for item in (nested.get("entries") or []) if item]
        except CodeJailError:
            inner = []
        if inner:
            shown = ", ".join(f"src/{name}" for name in inner[:4])
            layers.append(f"source under {shown}")
        else:
            layers.append("source under src")
    if "backend" in names:
        layers.append("a backend package")
    if "macos" in names or "ios" in names:
        layers.append("native Apple clients")
    if not layers and any(item.endswith(".py") for item in entries):
        layers.append("Python modules at the top level")
    if not layers:
        return ""
    if len(layers) == 1:
        return f"{folder} is organized as {layers[0]}."
    return f"{folder} is organized as {layers[0]}, plus {', '.join(layers[1:])}."


def _deeper_spoken(purpose: str, structure: str, run_spoken: str, stack_spoken: str) -> str:
    parts: list[str] = []
    for item in (purpose, structure, stack_spoken, run_spoken):
        text = (item or "").strip()
        if not text:
            continue
        if text.lower() in " ".join(parts).lower():
            continue
        parts.append(text.rstrip("."))
    spoken = ". ".join(parts)
    if spoken and not spoken.endswith("."):
        spoken += "."
    return spoken[:700]


def _alias_token_ok(token: str, folder: str = "") -> bool:
    raw = re.sub(r"\s+", " ", (token or "").strip().lower())
    if len(raw) < 5 or len(raw) > 40:
        return False
    if " " not in raw:
        return False
    if raw.count(" ") > 3:
        return False
    if raw in _ALIAS_STOP or raw == (folder or "").strip().lower():
        return False
    return not bool(_ALIAS_REJECT_RE.search(raw))


def _aliases_for(*, title: str, folder: str, spoken: str, desc: str) -> list[str]:
    aliases: list[str] = []
    lowered_folder = (folder or "").replace("-", " ").replace("_", " ").strip().lower()
    token = re.sub(r"\s+", " ", (title or "").strip().lower())
    if _alias_token_ok(token, lowered_folder):
        aliases.append(token)
    blob = f"{spoken} {desc}".lower()
    hit = re.search(r"\b([a-z][a-z]+(?: [a-z]+){0,2} film)\b", blob)
    if hit:
        phrase = hit.group(1).strip()
        if _alias_token_ok(phrase, lowered_folder) and phrase not in aliases:
            aliases.append(phrase)
    unique: list[str] = []
    for item in aliases:
        if item not in unique:
            unique.append(item)
    return unique[:8]


def _card_stamp(root: Path) -> float:
    stamp = 0.0
    for rel in (
        "OVERVIEW.md",
        "README.md",
        "package.json",
        "pyproject.toml",
        "src/app/layout.tsx",
        "app/layout.tsx",
    ):
        path = root / rel
        try:
            stamp = max(stamp, path.stat().st_mtime)
        except OSError:
            continue
    return stamp


def _persist_card(card: dict[str, Any]) -> None:
    folder = str(card.get("folder") or "").strip()
    if not folder or not card.get("purpose_ok"):
        return
    try:
        from app.memory.paths import atomic_write_json, ensure_tree, read_json

        path = ensure_tree() / "code-jobs" / "project-cards.json"
        stored = read_json(path) if path.exists() else {}
        if not isinstance(stored, dict):
            stored = {}
        stored[folder.lower()] = {
            "one_liner": card.get("one_liner"),
            "aliases": card.get("aliases") or [],
            "path": card.get("workspace"),
            "title": card.get("title"),
        }
        atomic_write_json(path, stored)
    except OSError:
        logger.debug("code_literacy.persist_failed folder=%s", folder)
    try:
        from app.ev.code_sandbox import note_aliases

        note_aliases(
            folder,
            [str(item) for item in (card.get("aliases") or [])],
            path=str(card.get("workspace") or ""),
        )
    except Exception:  # noqa: BLE001 - alias map must never break a survey
        logger.debug("code_literacy.alias_map_failed folder=%s", folder)


def _safe_read(rel: str, *, limit: int = 80) -> str:
    try:
        return str(read_file(rel, limit=limit).get("content") or "").strip()
    except CodeJailError:
        return ""


def _clean_md_line(line: str) -> str:
    raw = re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", line or "")
    raw = re.sub(r"^#+\s*", "", raw)
    raw = raw.strip(" >-*\t")
    raw = re.sub(r"[*_`]+", "", raw)
    return re.sub(r"\s+", " ", raw).strip()


def _markdown_section(text: str, headings: tuple[str, ...]) -> str:
    wanted = {item.lower() for item in headings}
    chunks = re.split(r"(?m)^#{1,3}\s+", text or "")
    for chunk in chunks[1:]:
        lines = chunk.splitlines()
        if not lines:
            continue
        heading = _clean_md_line(lines[0]).lower()
        if heading not in wanted and not any(heading.startswith(item) for item in wanted):
            continue
        body = "\n".join(lines[1:])
        return _first_markdown_claim(body)
    return ""


def _first_markdown_claim(text: str) -> str:
    parts: list[str] = []
    for raw in (text or "").splitlines():
        line = _clean_md_line(raw)
        if not line or _SKIP_MD_LINE_RE.search(line):
            if parts:
                break
            continue
        if line.lower() in {"what it is", "purpose", "overview", "about", "summary"}:
            continue
        if (
            not parts
            and len(line) <= 48
            and not line.endswith(".")
            and not re.search(r"\b(?:is|are|was|it's)\b", line.lower())
        ):
            continue
        parts.append(line)
        blob = " ".join(parts)
        if len(blob) >= 80 or blob.endswith("."):
            break
    blob = " ".join(parts).strip()
    if len(blob) > 280:
        cut = blob[:280]
        blob = cut.rsplit(" ", 1)[0] if " " in cut else cut
    return blob


def _package_meta(text: str) -> tuple[str, str, tuple[str, ...], dict[str, str]]:
    try:
        data = json.loads(text or "")
    except json.JSONDecodeError:
        return "", "", (), {}
    if not isinstance(data, dict):
        return "", "", (), {}
    name = str(data.get("name") or "").strip()
    desc = str(data.get("description") or "").strip()
    deps = data.get("dependencies") if isinstance(data.get("dependencies"), dict) else {}
    extra = data.get("devDependencies") if isinstance(data.get("devDependencies"), dict) else {}
    keys = tuple(sorted({*(deps or {}), *(extra or {})}))
    scripts_raw = data.get("scripts") if isinstance(data.get("scripts"), dict) else {}
    scripts = {
        str(key): str(value)
        for key, value in (scripts_raw or {}).items()
        if str(key).strip() and str(value).strip()
    }
    return name, desc, keys, scripts


def _layout_meta(text: str) -> tuple[str, str]:
    title = ""
    desc = ""
    hit = re.search(r"title:\s*[\"']([^\"']+)[\"']", text or "")
    if hit:
        title = hit.group(1).strip()
    hit = re.search(r"description:\s*[\"']([^\"']+)[\"']", text or "")
    if hit:
        desc = hit.group(1).strip()
    return title, desc


def _pyproject_meta(text: str) -> tuple[str, str]:
    name = ""
    desc = ""
    hit = re.search(r"(?m)^name\s*=\s*[\"']([^\"']+)[\"']", text or "")
    if hit:
        name = hit.group(1).strip()
    hit = re.search(r"(?m)^description\s*=\s*[\"']([^\"']+)[\"']", text or "")
    if hit:
        desc = hit.group(1).strip()
    return name, desc


def _stack_clause(deps: tuple[str, ...], entries: list[str]) -> str:
    names = {item.lower() for item in deps}
    names.update(Path(item.rstrip("/")).name.lower() for item in entries)
    if "next" in names and ("three" in names or "@react-three/fiber" in names):
        return "a Next.js and Three.js 3D app"
    if "next" in names:
        return "a Next.js app"
    if "react" in names and "react-native" not in names:
        return "a React app"
    if "django" in names or "fastapi" in names or "flask" in names:
        return "a Python web app"
    if any(item.endswith(".py") or item.rstrip("/").lower() in {"backend", "app"} for item in entries):
        if any("swift" in item.lower() or item.endswith(".swift") for item in entries):
            return "a Python and Swift companion"
        return "a Python project"
    if any(item.endswith(".swift") or "package.swift" in item.lower() for item in entries):
        return "a Swift project"
    if "cargo.toml" in names or any(item.endswith(".rs") for item in entries):
        return "a Rust project"
    return ""


def _python_module_clue(entries: list[str]) -> str:
    names = [Path(item.rstrip("/")).name for item in entries]
    py_files = [
        name
        for name in names
        if name.endswith(".py") and not name.startswith("test_") and name != "__init__.py"
    ]
    if not py_files:
        return ""
    src = _safe_read(py_files[0], limit=40)
    fn = re.search(r"^def (\w+)", src or "", re.M)
    if not fn:
        return ""
    helper = fn.group(1)
    article = "an" if helper[:1].lower() in "aeiou" else "a"
    tests = any(name.startswith("test_") or name.lower() == "tests" for name in names)
    if tests:
        return f"a small Python module with {article} {helper} helper and tests"
    return f"a small Python module with {article} {helper} helper"


def _trim_purpose(text: str) -> str:
    extra = (text or "").rstrip(".")
    if not extra:
        return ""
    if ":" in extra:
        head, tail = extra.split(":", 1)
        if len(tail.strip()) > 80:
            extra = head.strip()
    return extra
