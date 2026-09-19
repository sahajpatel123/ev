"""Central locate: one competitive vote across the whole Mac.

The folder sandbox (named Code + Desktop/Documents/Downloads map) and the
file index (IDF / prefix / edit-distance / trigram Dice) are sensors. This
module is the vote. No origin gets a prior — Code does not beat Desktop
because it is Code, Desktop does not beat Documents because it is Desktop.
A name either wins, ties (the owner picks), or misses.

Write/edit/run still jail to a software project. Find/info is this hub.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

_KEEP = 78.0
_GAP = 12.0
_WRITE_RE = re.compile(
    r"\b(?:write|create|make|build|implement|generate|scaffold|add|edit|"
    r"patch|refactor|fix|run)\b",
    re.IGNORECASE,
)
_FOLDERISH_RE = re.compile(
    r"\b(?:folders?|files?|director(?:y|ies)|find|where(?:'s| is)|locate|search for)\b",
    re.IGNORECASE,
)
_NORM_RE = re.compile(r"[\s._-]+")


@dataclass(frozen=True)
class HubHit:
    name: str
    path: Path
    origin: str
    kind: str
    score: float
    rel: str = ""
    project: str = ""


@dataclass(frozen=True)
class HubResult:
    status: str
    query: str
    hits: tuple[HubHit, ...] = ()

    @property
    def best(self) -> HubHit | None:
        return self.hits[0] if self.hits else None

    @property
    def is_software_unique(self) -> bool:
        return self.status == "hit" and software_lane(self.best)


def software_lane(hit: HubHit | None) -> bool:
    """True when the unique winner is software, wherever it lives."""

    if hit is None:
        return False
    return hit.kind == "project" or hit.origin == "code"


def query_from_text(text: str | None) -> str:
    raw = (text or "").strip()
    if not raw:
        return ""
    try:
        from app.ev.code_locate import extract_locate_queries, wanted_place_names

        wanted = wanted_place_names(raw)
        if wanted:
            return wanted[0]
        queries = extract_locate_queries(raw)
        if queries:
            return queries[0]
    except Exception:  # noqa: BLE001 - hub miss is better than a locate crash
        pass
    return ""


def locate_named(text: str | None) -> HubResult:
    raw = (text or "").strip()
    query = query_from_text(raw)
    return locate_query(query, utterance=raw)


def locate_query(query: str | None, *, utterance: str = "") -> HubResult:
    token = (query or "").strip()
    if not token or len(_norm(token)) < 2:
        return HubResult("miss", token or "", ())
    merged = _merge(_map_hits(token), _catalog_hits(token), _index_hits(token))
    hint = ""
    if utterance:
        try:
            from app.ev.code_locate import location_folder_hint, name_is_rejected

            hint = location_folder_hint(utterance) or ""
            merged = [hit for hit in merged if not name_is_rejected(utterance, hit.name)]
            merged = [hit for hit in merged if not name_is_rejected(utterance, hit.project)]
        except Exception:  # noqa: BLE001
            hint = ""
    if hint:
        hinted = [hit for hit in merged if hit.origin == hint]
        if hinted:
            merged = hinted
    noun = _spoken_kind(utterance)
    if noun in {"project", "repo", "workspace", "codebase"}:
        software = [hit for hit in merged if hit.kind == "project"]
        if software:
            merged = software
    elif noun == "file":
        files = [hit for hit in merged if hit.kind == "file"]
        if files:
            merged = files
    merged.sort(key=lambda hit: (-hit.score, _kind_rank(hit.kind), len(hit.rel), len(hit.name)))
    kept = [hit for hit in merged if hit.score >= _KEEP]
    if not kept:
        return HubResult("miss", token, ())
    winner = kept[0]
    if len(kept) == 1:
        return HubResult("hit", token, (winner,))
    rival = kept[1]
    if _same_place(winner, rival) or winner.score - rival.score >= _GAP:
        return HubResult("hit", token, (winner,))
    if winner.origin != rival.origin or _norm(winner.project) != _norm(rival.project):
        return HubResult("ambiguous", token, tuple(kept[:4]))
    return HubResult("hit", token, (winner,))


def folderish_ask(text: str | None) -> bool:
    """Find/where plus folder/file nouns — Mac-wide, not Spark."""

    return bool(_FOLDERISH_RE.search(text or ""))


def hub_owns_ask(text: str | None) -> bool:
    """Find/info that belongs to the Mac-wide vote, not Spark."""

    raw = (text or "").strip()
    if not raw or _WRITE_RE.search(raw):
        return False
    try:
        from app.ev.code_locate import looks_like_named_place_ask
    except Exception:  # noqa: BLE001
        return False
    if not looks_like_named_place_ask(raw):
        return False
    result = locate_named(raw)
    if result.is_software_unique:
        return False
    return folderish_ask(raw)


def spoken_result(result: HubResult) -> str:
    query = result.query or "that"
    if result.status == "miss":
        return f"I don't see {query} on this Mac."
    if result.status == "ambiguous":
        labels = [_place_label(hit) for hit in result.hits[:4]]
        listed = ", ".join(item for item in labels if item)
        extra = " and more" if len(result.hits) > 4 else ""
        return f"I found {query} in more than one place: {listed}{extra}. Which one?"
    hit = result.best
    if hit is None:
        return f"I don't see {query} on this Mac."
    where = _place_label(hit)
    if hit.kind == "project":
        return f"{hit.name} is a project {where}."
    if hit.kind == "file":
        return f"{hit.name} is {where}."
    kids = _child_names(hit.path)
    kid_bit = f" It holds {kids}." if kids else ""
    return f"{hit.name} is {where}.{kid_bit}"


def hub_job_payload(result: HubResult, goal: str) -> dict[str, Any]:
    spoken = spoken_result(result)
    hit = result.best
    if result.status == "miss":
        return {
            "ok": False,
            "spoken": spoken,
            "files_changed": [],
            "runs": [],
            "brain": "hub",
            "degraded": True,
            "partial": False,
            "error": "unknown_folder",
            "purpose_ok": False,
            "kind": "miss",
        }
    if result.status == "ambiguous":
        return {
            "ok": True,
            "spoken": spoken,
            "files_changed": [],
            "runs": [],
            "brain": "hub",
            "degraded": False,
            "partial": False,
            "kind": "ambiguous",
            "options": [_place_label(item) for item in result.hits[:4]],
            "workspace": str(hit.path) if hit is not None else "",
        }
    assert hit is not None
    return {
        "ok": True,
        "spoken": spoken,
        "one_liner": hit.rel or hit.name,
        "files_changed": [],
        "runs": [],
        "brain": "hub",
        "degraded": False,
        "partial": False,
        "purpose_ok": True,
        "workspace": str(hit.path if hit.kind != "file" else hit.path.parent),
        "title": hit.name,
        "folder": hit.project or hit.origin,
        "goal": goal,
        "rel": hit.rel,
        "kind": "desk" if hit.origin != "code" and hit.kind != "project" else hit.kind,
        "path": str(hit.path),
        "origin": hit.origin,
    }


def _map_hits(token: str) -> list[HubHit]:
    from app.ev.code_sandbox import ranked_name_hits

    hits: list[HubHit] = []
    for score, row in ranked_name_hits(token):
        path = _row_path(row)
        if path is None:
            continue
        hits.append(
            HubHit(
                name=str(row.get("name") or path.name),
                path=path,
                origin=_origin_of(path, str(row.get("kind") or "")),
                kind=_kind_for_row(row, path),
                score=float(score),
                rel=str(row.get("rel") or ""),
                project=str(row.get("project") or ""),
            )
        )
    return hits


def _catalog_hits(token: str) -> list[HubHit]:
    from app.ev.code_runtime import GENERIC_PROJECT_NAMES, list_projects
    from app.ev.code_sandbox import name_score

    hits: list[HubHit] = []
    for item in list_projects():
        name = str(item.get("name") or "")
        if not name or name in GENERIC_PROJECT_NAMES or len(name) < 2:
            continue
        path = Path(str(item.get("path") or ""))
        if not path:
            continue
        try:
            resolved = path.expanduser().resolve()
        except OSError:
            continue
        score = name_score(token, name)
        if score < _KEEP:
            continue
        hits.append(
            HubHit(
                name=name,
                path=resolved,
                origin=_origin_of(resolved, "project"),
                kind="project",
                score=score,
                rel="",
                project=name,
            )
        )
    return hits


def _index_hits(token: str) -> list[HubHit]:
    from app.ev.code_sandbox import name_score

    hits: list[HubHit] = []
    for path in _index_candidates(token):
        try:
            resolved = path.expanduser().resolve()
        except OSError:
            continue
        score = name_score(token, resolved.stem if resolved.is_file() else resolved.name)
        if score < _KEEP:
            continue
        kind = "file"
        if resolved.is_dir():
            kind = "project" if _is_project(resolved) else "folder"
        hits.append(
            HubHit(
                name=resolved.name,
                path=resolved,
                origin=_origin_of(resolved, kind),
                kind=kind,
                score=score,
                rel="",
                project=resolved.name if kind == "project" else "",
            )
        )
    return hits


def _index_candidates(token: str) -> list[Path]:
    from app.config import settings

    override = str(getattr(settings, "laptop_files_root", "") or "").strip()
    if not override and os.environ.get("PYTEST_CURRENT_TEST"):
        return []
    try:
        from app.ev.file_index import scored_search
    except Exception:  # noqa: BLE001
        return []
    found: list[Path] = []
    seen: set[Path] = set()
    try:
        folders = scored_search(token, want_folder=True, limit=24)
        files = scored_search(token, want_folder=False, limit=24)
    except Exception:  # noqa: BLE001
        return []
    for path in list(folders) + list(files):
        try:
            resolved = path.expanduser().resolve()
        except OSError:
            continue
        if resolved in seen:
            continue
        seen.add(resolved)
        found.append(resolved)
    return found


def _merge(*groups: list[HubHit]) -> list[HubHit]:
    best: dict[Path, HubHit] = {}
    for group in groups:
        for hit in group:
            try:
                key = hit.path.expanduser().resolve()
            except OSError:
                continue
            current = best.get(key)
            if current is None or hit.score > current.score:
                best[key] = HubHit(
                    name=hit.name,
                    path=key,
                    origin=hit.origin,
                    kind=hit.kind,
                    score=hit.score,
                    rel=hit.rel,
                    project=hit.project,
                )
    return list(best.values())


def _row_path(row: dict[str, Any]) -> Path | None:
    raw = str(row.get("path") or row.get("root") or "").strip()
    if not raw:
        return None
    try:
        return Path(raw).expanduser().resolve()
    except OSError:
        return None


def _kind_for_row(row: dict[str, Any], path: Path) -> str:
    kind = str(row.get("kind") or "")
    if kind == "project":
        return "project"
    try:
        is_dir = path.is_dir()
    except OSError:
        is_dir = False
    if kind == "desk":
        if is_dir and _is_project(path):
            return "project"
        return "folder" if is_dir else "file"
    if kind in {"folder", "file"}:
        return kind
    if is_dir:
        return "project" if _is_project(path) else "folder"
    return "file"


def _origin_of(path: Path, kind: str = "") -> str:
    parts = [part.lower() for part in path.parts]
    if "desktop" in parts:
        return "desktop"
    if "documents" in parts:
        return "documents"
    if "downloads" in parts:
        return "downloads"
    try:
        from app.ev.code_runtime import projects_root

        root = projects_root()
        if root is not None:
            resolved = root.expanduser().resolve()
            if path == resolved or resolved in path.parents:
                return "code"
    except Exception:  # noqa: BLE001
        pass
    if "code" in parts or kind == "project":
        return "code" if "code" in parts else "home"
    return "home"


def _is_project(path: Path) -> bool:
    try:
        from app.ev.code_runtime import _looks_like_project, is_sandbox_workspace

        if not path.exists() or not path.is_dir() or is_sandbox_workspace(path):
            return False
        return bool(_looks_like_project(path))
    except OSError:
        return False
    except Exception:  # noqa: BLE001
        return False


def _same_place(left: HubHit, right: HubHit) -> bool:
    if left.path == right.path:
        return True
    return (
        left.origin == right.origin
        and _norm(left.name) == _norm(right.name)
        and _norm(left.project) == _norm(right.project)
    )


def _kind_rank(kind: str) -> int:
    order = {"project": 0, "folder": 1, "desk": 1, "file": 2}
    return order.get(kind, 3)


def _place_label(hit: HubHit) -> str:
    origin = hit.origin
    if origin == "code":
        folder = hit.project or hit.path.name
        if hit.rel:
            return f"in {folder} at {hit.rel}"
        return f"in {folder}"
    if origin == "desktop":
        return "on your Desktop"
    if origin == "documents":
        return "in Documents"
    if origin == "downloads":
        return "in Downloads"
    try:
        parent = hit.path.parent.name
    except OSError:
        parent = ""
    if parent:
        return f"in {parent}"
    return "on this Mac"


def _child_names(path: Path) -> str:
    try:
        kids = [child.name for child in path.iterdir() if not child.name.startswith(".")][:6]
    except OSError:
        return ""
    return ", ".join(kids)


def _spoken_kind(text: str) -> str:
    raw = (text or "").strip()
    if not raw:
        return ""
    try:
        from app.ev.code_locate import _THE_PLACE_RE
    except Exception:  # noqa: BLE001
        return ""
    hit = _THE_PLACE_RE.search(raw)
    if hit is None:
        return ""
    kind = (hit.group("kind") or "").lower()
    if kind.startswith("file"):
        return "file"
    if kind.startswith(("folder", "director")):
        return "folder"
    if kind.startswith("repo"):
        return "repo"
    if kind.startswith("workspace"):
        return "workspace"
    if kind.startswith("codebase"):
        return "codebase"
    if kind.startswith("project"):
        return "project"
    return ""


def _norm(value: str) -> str:
    return _NORM_RE.sub("", (value or "").lower())
