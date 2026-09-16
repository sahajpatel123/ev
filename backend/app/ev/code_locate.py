"""Find a named Code project, inner folder, or file from owner phrasing.

Mini is the mouth. This module only reads allowlisted Code trees. It never
walks Desktop/home, never runs npm, and never answers a name it did not find.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from app.ev.code_runtime import (
    GENERIC_PROJECT_NAMES,
    CodeJailError,
    _mentions_named_project,
    is_sandbox_workspace,
    list_dir,
    list_projects,
    read_file,
    reset_active_project,
    set_active_project,
)

_STOP = frozenset(
    {
        "the",
        "a",
        "an",
        "my",
        "our",
        "this",
        "that",
        "it",
        "code",
        "file",
        "files",
        "folder",
        "folders",
        "directory",
        "project",
        "repo",
        "workspace",
        "codebase",
        "app",
        "src",
        "test",
        "tests",
        "docs",
        "lib",
        "bin",
        "dist",
        "build",
        "public",
        "assets",
        "components",
        "utils",
        "hooks",
        "pages",
        "info",
        "information",
        "overview",
        "details",
        "python",
        "javascript",
        "typescript",
        "swift",
        "react",
        "node",
        "next",
        "about",
        "please",
        "something",
        "anything",
    }
)
_GENERIC_INNER = frozenset(
    {
        "src",
        "app",
        "test",
        "tests",
        "docs",
        "lib",
        "bin",
        "dist",
        "build",
        "public",
        "assets",
        "components",
        "utils",
        "hooks",
        "pages",
        "source",
        "scripts",
        "backend",
        "frontend",
        "ios",
        "macos",
    }
)
_THE_PLACE_RE = re.compile(
    r"\b(?:the |my |our |this )(?P<name>[A-Za-z][\w-]*(?:[ _][A-Za-z][\w-]*){0,2})"
    r"(?:'s)?\s+(?P<kind>folders?|directories|directory|repos?|projects?|"
    r"workspaces?|codebases?|files?)\b",
    re.IGNORECASE,
)
_FILE_NAME_RE = re.compile(
    r"\b([A-Za-z][\w.-]*\.(?:tsx?|jsx?|py|swift|md|json|mjs|cjs|go|rs|toml))\b",
    re.IGNORECASE,
)
_CAMEL_RE = re.compile(r"\b([A-Z][a-z]+[A-Z][\w]*)\b")
_INFO_PREFIX_RE = re.compile(
    r"^(?:hey |ok(?:ay)? |evie |please |can you |could you |would you )*"
    r"(?:"
    r"tell me about|talk (?:to me )?about|explain|describe|summarize|"
    r"look (?:at|through|into)|take a look at|"
    r"(?:help me |i (?:want|need) to )?understand|"
    r"give me (?:some )?(?:the )?(?:info|information|details|an overview|a rundown)"
    r"(?: about| on)?"
    r"|info(?:rmation)? (?:about|on|regarding)|"
    r"details about|overview of|"
    r"what(?:'s| is)(?: in| inside)?"
    r"|where(?:'s| is)|find|show me|"
    r"reference|based on"
    r")"
    r"(?: the | my | our | this | a | an )?",
    re.IGNORECASE,
)
_KIND_TAIL_RE = re.compile(
    r"\s+(?:folders?|directories|directory|repos?|projects?|workspaces?|"
    r"codebases?|files?|code)\s*$",
    re.IGNORECASE,
)
_PERSON_RE = re.compile(r"^[A-Z][a-z]{2,20}$")


@dataclass(frozen=True)
class CodeTarget:
    project: str
    root: Path
    rel: str
    kind: str
    name: str
    options: tuple[str, ...] = ()


def looks_like_code_info_ask(text: str | None) -> bool:
    raw = (text or "").strip()
    if not raw:
        return False
    return bool(_INFO_PREFIX_RE.search(raw))


def looks_like_named_place_ask(text: str | None) -> bool:
    """Owner named a folder/repo/workspace/file place — known or not.

    Unknown names must still enter the code lane so we can miss instantly
    instead of Spark-hunting the default sandbox.
    """

    raw = (text or "").strip()
    if not raw:
        return False
    if re.search(
        r"\b(?:conversation|chats?|email|mail|message|people|photos?|weather|"
        r"sandwich|mom|dad|calendar|reminder|"
        r"on my desktop|inside my desktop|in my documents|in downloads|"
        r"on my laptop|from (?:my )?(?:laptop|mac|computer)|"
        r"on (?:this |my )?(?:mac|computer)|in my home folder"
        r")\b",
        raw,
        re.IGNORECASE,
    ):
        return False
    hit = _THE_PLACE_RE.search(raw)
    if not hit:
        return False
    kind = (hit.group("kind") or "").lower()
    headed = (raw[hit.start() : hit.start() + 4]).lower()
    if headed.startswith("my ") and kind.startswith(("file", "folder", "director")):
        from app.ev.code_runtime import (
            GENERIC_PROJECT_NAMES,
            _mentions_named_project,
            catalog_project_names,
        )

        lowered = raw.lower()
        named = any(
            _mentions_named_project(lowered, name)
            for name in catalog_project_names()
            if name not in GENERIC_PROJECT_NAMES
        )
        if not named and not re.search(
            r"\b(?:repo|project|workspace|codebase|code folder)\b",
            raw,
            re.IGNORECASE,
        ):
            return False
    return True


def resolve_code_target(text: str | None) -> CodeTarget | None:
    """Map owner phrasing onto one allowlisted Code folder or file."""

    raw = (text or "").strip()
    if not raw:
        return None
    catalog_name, catalog_root = _catalog_target(raw)
    place_names = {
        _norm(_strip_kind(hit.group("name") or ""))
        for hit in _THE_PLACE_RE.finditer(raw)
    }
    queries = extract_locate_queries(raw)
    inner_queries = [
        item
        for item in queries
        if catalog_name is None or _norm(item) != _norm(catalog_name)
    ]
    inner_queries = [
        item
        for item in inner_queries
        if not _skip_inner_token(
            item,
            scoped=catalog_root is not None,
            place_names=place_names,
        )
    ]
    if catalog_root is None:
        inner_queries = [
            item
            for item in inner_queries
            if _norm(item) in place_names
            or "." in item
            or bool(_CAMEL_RE.fullmatch(item))
        ]
    if inner_queries:
        hits: list[CodeTarget] = []
        token = inner_queries[0]
        for token in inner_queries:
            scoped = [catalog_root] if catalog_root is not None else None
            hits = find_named_entries(token, roots=scoped)
            if not hits and scoped:
                hits = find_named_entries(token, roots=None)
            if hits:
                break
        if hits:
            code_hits = [item for item in hits if item.kind != "desk"]
            if code_hits:
                hits = code_hits
        chosen = _choose_hit(hits, catalog_root=catalog_root, token=token)
        if chosen is not None:
            return chosen
        if hits:
            names = tuple(dict.fromkeys(item.project for item in hits))
            if len(names) > 1:
                return CodeTarget(
                    project=hits[0].project,
                    root=hits[0].root,
                    rel="",
                    kind="ambiguous",
                    name=token,
                    options=names,
                )
    if catalog_root is not None and catalog_name:
        return CodeTarget(
            project=catalog_name,
            root=catalog_root,
            rel="",
            kind="project",
            name=catalog_name,
        )
    return None


def extract_locate_queries(text: str | None) -> list[str]:
    raw = (text or "").strip()
    if not raw:
        return []
    found: list[str] = []

    def _add(token: str) -> None:
        cleaned = _strip_kind(token)
        if not cleaned or cleaned.lower() in _STOP:
            return
        if cleaned not in found:
            found.append(cleaned)

    for hit in _THE_PLACE_RE.finditer(raw):
        _add(hit.group("name") or "")
    for hit in _FILE_NAME_RE.finditer(raw):
        _add(hit.group(1) or "")
    for hit in _CAMEL_RE.finditer(raw):
        _add(hit.group(1) or "")
    subject = _info_subject(raw)
    if subject:
        _add(subject)
    return found


def find_named_entries(token: str, *, roots: list[Path] | None = None) -> list[CodeTarget]:
    from app.ev.code_sandbox import lookup_folder_name

    wanted = (token or "").strip()
    if not wanted:
        return []
    allowed = None
    if roots:
        allowed = {path.resolve() for path in roots if path is not None}
    hits: list[CodeTarget] = []
    for row in lookup_folder_name(wanted):
        kind = str(row.get("kind") or "file")
        if kind == "project":
            continue
        project = str(row.get("project") or "")
        rel = str(row.get("rel") or "")
        name = str(row.get("name") or Path(rel).name)
        root = Path(str(row.get("root") or row.get("path") or ""))
        if not project or not root:
            continue
        if kind == "desk":
            if allowed is not None:
                continue
            hits.append(
                CodeTarget(
                    project=project,
                    root=root,
                    rel=rel,
                    kind="desk",
                    name=name,
                )
            )
            continue
        try:
            resolved_root = root.resolve()
        except OSError:
            continue
        if allowed is not None and resolved_root not in allowed:
            continue
        hits.append(
            CodeTarget(
                project=project,
                root=resolved_root,
                rel=rel,
                kind=kind,
                name=name,
            )
        )
    return hits


def spoken_code_target(target: CodeTarget) -> str:
    if target.kind == "ambiguous":
        listed = ", ".join(target.options[:4])
        extra = " and more" if len(target.options) > 4 else ""
        return f"I found {target.name} in {listed}{extra}. Which project?"
    if target.kind == "desk":
        return _spoken_desk(target)
    if not target.rel:
        return ""
    token = set_active_project(target.root)
    try:
        if target.kind == "folder":
            return _spoken_folder(target)
        return _spoken_file(target)
    finally:
        reset_active_project(token)


def missing_folder_spoken(goal: str) -> str:
    """Honest miss — never a Spark hunt through the coding sandbox."""

    from app.ev.code_sandbox import known_project_names

    queries = extract_locate_queries(goal)
    name = queries[0] if queries else "that folder"
    names = [item for item in known_project_names() if item][:8]
    have = f" I have {', '.join(names)}." if names else ""
    return f"I don't see {name} in your Code folder.{have}"[:700]


def needle_names_target(needle: str, target: CodeTarget) -> bool:
    """True when a where/find needle is the project or the located file itself."""

    if target.kind == "ambiguous":
        return False
    token = _norm(_strip_kind(needle))
    if not token:
        return False
    names = {_norm(target.project), _norm(target.name)}
    if target.rel:
        names.update({_norm(Path(target.rel).name), _norm(Path(target.rel).stem)})
    return token in names


def target_is_self_locate(goal: str, target: CodeTarget) -> bool:
    """True when 'find the wish folder' means the project, not a grep."""

    if target.kind == "ambiguous":
        return False
    raw = (goal or "").strip()
    queries = extract_locate_queries(raw)
    if not queries:
        return not target.rel
    token = _norm(_strip_kind(queries[0]))
    names = {_norm(_strip_kind(item)) for item in queries}
    names.add(token)
    if not token:
        return False
    if _norm(target.project) in names or _norm(target.name) in names:
        return True
    return bool(
        target.rel
        and token in {_norm(Path(target.rel).name), _norm(Path(target.rel).stem)}
    )


def _catalog_target(text: str) -> tuple[str | None, Path | None]:
    catalog: list[tuple[str, Path]] = []
    for item in list_projects():
        name = str(item.get("name") or "")
        path = Path(str(item.get("path") or ""))
        if name in GENERIC_PROJECT_NAMES or len(name) < 2:
            continue
        if is_sandbox_workspace(path):
            continue
        catalog.append((name, path))
    lowered = text.lower()
    mentioned = [
        name
        for name, _path in catalog
        if _mentions_named_project(lowered, name)
    ]
    if mentioned:
        mentioned.sort(key=len, reverse=True)
        name = mentioned[0]
        path = next(path for item, path in catalog if item == name)
        return name, path
    try:
        from app.ev.code_sandbox import alias_project_name

        alias = alias_project_name(text)
    except Exception:  # noqa: BLE001 - alias miss must not break locate
        alias = None
    if alias and _alias_is_invoked(text, alias):
        for name, path in catalog:
            if name == alias:
                return name, path
    subject = _strip_kind(_info_subject(text) or "")
    if subject:
        want = _norm(subject)
        ranked = [
            (name, path)
            for name, path in catalog
            if _norm(name) == want
        ]
        if len(ranked) == 1:
            return ranked[0]
    return None, None


def _alias_is_invoked(text: str, alias: str) -> bool:
    """True when the owner used this title as the thing they named, not a topic word."""

    token = (alias or "").strip()
    if not token:
        return False
    subject = _strip_kind(_info_subject(text) or "")
    if subject and _norm(subject) == _norm(token):
        return True
    for hit in _THE_PLACE_RE.finditer(text or ""):
        if _norm(hit.group("name") or "") == _norm(token):
            return True
    return bool(
        re.search(
            rf"\b{re.escape(token)}\b.{{0,24}}\b(?:repo|project|folder|workspace|codebase)\b",
            text or "",
            re.IGNORECASE,
        )
    )


def _info_subject(text: str) -> str | None:
    raw = (text or "").strip().strip("?.!")
    hit = _INFO_PREFIX_RE.search(raw)
    if not hit:
        return None
    rest = raw[hit.end() :].strip()
    rest = _KIND_TAIL_RE.sub("", rest)
    rest = re.sub(r"\s+", " ", rest).strip(" .,'\"")
    if not rest or rest.lower() in _STOP or len(rest) > 48:
        return None
    return rest


def _strip_kind(token: str) -> str:
    cleaned = _KIND_TAIL_RE.sub("", (token or "").strip())
    cleaned = re.sub(r"^(?:the |my |our |this |a |an )", "", cleaned, flags=re.IGNORECASE)
    return cleaned.strip(" .,'\"")


def _skip_inner_token(
    token: str, *, scoped: bool, place_names: set[str] | None = None
) -> bool:
    raw = (token or "").strip()
    if not raw:
        return True
    lowered = raw.lower()
    if lowered.split()[0] in {"find", "tell", "look", "show", "give", "help", "where", "what"}:
        return True
    if (
        _PERSON_RE.fullmatch(raw)
        and "." not in raw
        and not (place_names and _norm(raw) in place_names)
    ):
        return True
    if lowered in _GENERIC_INNER and not scoped:
        return True
    if len(raw.split()) > 3 or len(raw) > 40:
        return True
    return len(raw) < 3


def _choose_hit(
    hits: list[CodeTarget], *, catalog_root: Path | None, token: str = ""
) -> CodeTarget | None:
    if not hits:
        return None
    scoped = hits
    if catalog_root is not None:
        matched = [item for item in hits if item.root == catalog_root.resolve()]
        if matched:
            scoped = matched
    projects = {item.project for item in scoped}
    if len(projects) > 1:
        return None
    want = (token or "").lower()
    return min(
        scoped,
        key=lambda item: (
            0 if item.kind == "folder" and Path(item.rel).name.lower() == want else 1,
            0 if item.kind == "file" and Path(item.rel).stem.lower() == want else 1,
            0 if item.rel.startswith("src/") else 1,
            len(item.rel),
        ),
    )


def _spoken_desk(target: CodeTarget) -> str:
    folder = target.root
    kids: list[str] = []
    try:
        kids = [child.name for child in folder.iterdir() if not child.name.startswith(".")][:6]
    except OSError:
        kids = []
    where = target.project or folder.parent.name or "your folders"
    kid_bit = f" It holds {', '.join(kids)}." if kids else ""
    return f"{target.name} is on your {where}.{kid_bit}"[:700]


def _spoken_folder(target: CodeTarget) -> str:
    folder = target.project
    rel = target.rel
    try:
        listing = list_dir(rel)
        entries = [str(item) for item in (listing.get("entries") or []) if item]
    except CodeJailError:
        entries = []
    kids = [item.rstrip("/") for item in entries[:6]]
    kid_bit = f" It holds {', '.join(kids)}." if kids else ""
    overview = ""
    for name in ("README.md", "OVERVIEW.md"):
        try:
            overview = str(read_file(f"{rel}/{name}", limit=30).get("content") or "").strip()
        except CodeJailError:
            overview = ""
        if overview:
            break
    claim = _first_claim(overview)
    if claim:
        return f"In {folder}, {target.name} is {rel}. {claim}{kid_bit}"[:700]
    return f"In {folder}, {target.name} is {rel}.{kid_bit}"[:700]


def _spoken_file(target: CodeTarget) -> str:
    folder = target.project
    rel = target.rel
    try:
        content = str(read_file(rel, limit=40).get("content") or "")
    except CodeJailError:
        content = ""
    hint = _file_hint(content, Path(rel).stem)
    where = f"In {folder}, {target.name} is {rel}"
    if hint:
        return f"{where}: {hint}"[:700]
    return f"{where}."[:700]


def _file_hint(content: str, stem: str) -> str:
    export = re.search(
        r"export (?:default )?(?:async )?(?:function|const|class) (\w+)",
        content or "",
    )
    if export:
        name = export.group(1)
        return f"it exports {name}."
    fn = re.search(r"^(?:async )?def (\w+)", content or "", re.M)
    if fn:
        return f"it defines {fn.group(1)}."
    cls = re.search(r"^class (\w+)", content or "", re.M)
    if cls:
        return f"it defines {cls.group(1)}."
    comment = re.search(r"^\s*(?:#|//|/\*)\s+(.+)$", content or "", re.M)
    if comment:
        line = re.sub(r"\s+", " ", comment.group(1)).strip(" /*")
        if 12 <= len(line) <= 140:
            return line.rstrip(".") + "."
    if stem:
        return f"that's the {stem} source."
    return ""


def _first_claim(text: str) -> str:
    for raw in (text or "").splitlines():
        line = re.sub(r"^#+\s*", "", raw).strip(" >-*")
        line = re.sub(r"[*_`]+", "", line)
        line = re.sub(r"\s+", " ", line).strip()
        if len(line) < 24:
            continue
        if line.lower().startswith(("inspected from", "table of contents")):
            continue
        return line[:220].rstrip(".") + "."
    return ""


def _norm(value: str) -> str:
    return re.sub(r"[\s._-]+", "", (value or "").lower())


def locate_job(target: CodeTarget, goal: str) -> dict[str, Any]:
    spoken = spoken_code_target(target)
    return {
        "ok": True,
        "spoken": spoken[:700],
        "one_liner": target.rel or target.project,
        "files_changed": [],
        "runs": [],
        "brain": "locate" if target.rel else "survey",
        "degraded": False,
        "purpose_ok": bool(spoken),
        "workspace": str(target.root),
        "title": target.name,
        "folder": target.project,
        "goal": goal,
        "rel": target.rel,
        "kind": target.kind,
    }
