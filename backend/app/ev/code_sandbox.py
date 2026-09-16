"""Evie's folder sandbox: a persisted name map so locate is instant.

Live os.walk of every Code tree on each utterance made Mini stall on
"locating the folder" — especially after the owner named a different tree.
This module is the working map: project names, aliases, inner folders/files,
and top-level Desktop/Documents/Downloads directories. Lookup never starts
Spark. The coding jail is unchanged; desk hits are spoken, not executed as code.
"""

from __future__ import annotations

import logging
import os
import re
from pathlib import Path
from typing import Any

from app.config import settings
from app.ev.code_runtime import (
    GENERIC_PROJECT_NAMES,
    SKIP_DIR_NAMES,
    is_sandbox_workspace,
    list_projects,
    projects_root,
)

logger = logging.getLogger("ev.code_sandbox")

_MAP: dict[str, Any] | None = None
_MAP_SCOPE = ""
_MAX_INDEX = 500
_MAX_DEPTH = 6
_DESK_DEPTH = 2
_MAX_DESK_FILES = 400
_VERSION = 2
_RANK_KEEP = 78.0
_RANK_GAP = 12.0

_NORM_RE = re.compile(r"[\s._-]+")


def folder_map() -> dict[str, Any]:
    """Return the in-memory map, building or refreshing as needed."""

    global _MAP, _MAP_SCOPE
    scope = _scope_key()
    if _MAP is not None and scope == _MAP_SCOPE and _MAP.get("version") == _VERSION:
        _refresh_stale_roots(_MAP)
        return _MAP
    loaded = _load_map(scope)
    if loaded is not None:
        _refresh_stale_roots(loaded)
        _MAP = loaded
        _MAP_SCOPE = scope
        return _MAP
    built = _build_map()
    _MAP = built
    _MAP_SCOPE = scope
    _persist_map(built)
    return built


def reset_folder_map() -> None:
    """Drop the in-process map. Tests call this after changing Code roots."""

    global _MAP, _MAP_SCOPE
    _MAP = None
    _MAP_SCOPE = ""


def lookup_folder_name(token: str, *, strict: bool = False) -> list[dict[str, Any]]:
    """Hits for one spoken name. Empty means the sandbox does not know it."""

    key = _norm(token)
    if not key or len(key) < 2:
        return []
    payload = folder_map()
    by_name = payload.get("by_name") if isinstance(payload.get("by_name"), dict) else {}
    hits = list(by_name.get(key) or [])
    if hits:
        return [item for item in hits if isinstance(item, dict)]
    ranked = _rank_name_hits(key, by_name)
    if not ranked:
        return []
    if strict:
        prefix = [
            row
            for score, row in ranked
            if score >= 90.0 and str(row.get("kind") or "") == "project"
        ]
        projects = {str(item.get("project") or "") for item in prefix}
        return prefix if len(projects) == 1 else []
    best = ranked[0][0]
    if best < _RANK_KEEP:
        return []
    kept = [row for score, row in ranked if best - score <= 2.0]
    projects = {str(item.get("project") or "") for item in kept}
    if len(projects) == 1:
        return kept
    second = ranked[1][0] if len(ranked) > 1 else 0.0
    if best - second >= _RANK_GAP:
        winner = str(ranked[0][1].get("project") or "")
        return [row for score, row in ranked if str(row.get("project") or "") == winner and best - score <= 2.0]
    return []


def alias_project_name(text: str) -> str | None:
    """Map a persisted alias ('Sweet Potato') onto a Code folder."""

    lowered = (text or "").lower()
    if not lowered:
        return None
    from app.ev.code_literacy import _alias_token_ok
    from app.ev.code_locate import negated_place_names

    denied = negated_place_names(text)
    payload = folder_map()
    aliases = payload.get("aliases") if isinstance(payload.get("aliases"), dict) else {}
    ranked: list[tuple[int, str]] = []
    for alias, name in aliases.items():
        token = str(alias or "").strip().lower()
        if not _alias_token_ok(token):
            continue
        if _norm(token) in denied or _norm(str(name)) in denied:
            continue
        if not re.search(rf"\b{re.escape(token)}\b", lowered):
            continue
        ranked.append((len(token), str(name)))
    if not ranked:
        return None
    ranked.sort(reverse=True)
    return ranked[0][1]


def note_aliases(folder: str, aliases: list[str], *, path: str = "") -> None:
    """Merge aliases from a project card without rebuilding the whole map."""

    payload = folder_map()
    stored = payload.setdefault("aliases", {})
    if not isinstance(stored, dict):
        stored = {}
        payload["aliases"] = stored
    name = (folder or "").strip().lower()
    if not name:
        return
    from app.ev.code_literacy import _alias_token_ok

    for alias in aliases:
        token = _norm(alias)
        raw = str(alias).strip().lower()
        if not _alias_token_ok(raw) or token == _norm(name):
            continue
        stored[str(alias).strip().lower()] = name
        _add_hit(
            payload,
            token,
            {
                "project": name,
                "rel": "",
                "kind": "project",
                "path": path,
                "root": path,
                "name": folder,
            },
        )
    _persist_map(payload)


def known_project_names() -> list[str]:
    payload = folder_map()
    names = payload.get("projects") if isinstance(payload.get("projects"), dict) else {}
    return [str(name) for name in names if str(name)]


def project_map_brief(root: Path | None, *, limit: int = 48) -> str:
    """Compact indexed tree for the selected jail. Empty if the map has no row."""

    if root is None:
        return ""
    try:
        want = str(root.expanduser().resolve())
    except OSError:
        return ""
    payload = folder_map()
    projects = payload.get("projects") if isinstance(payload.get("projects"), dict) else {}
    meta: dict[str, Any] | None = None
    for item in projects.values():
        if not isinstance(item, dict):
            continue
        if str(item.get("path") or "") == want and str(item.get("kind") or "") == "code":
            meta = item
            break
    if not isinstance(meta, dict):
        return ""
    folders = [str(item) for item in (meta.get("folders") or []) if item][:limit]
    files = [str(item) for item in (meta.get("files") or []) if item][: min(24, limit)]
    if not folders and not files:
        return ""
    parts: list[str] = []
    if folders:
        parts.append("folders: " + ", ".join(folders[:40]))
    if files:
        parts.append("files: " + ", ".join(files[:16]))
    return "\n".join(parts)[:1200]


def lookup_in_project(token: str, root: Path | None) -> list[dict[str, Any]]:
    """Map hits inside one allowlisted Code root. Never desk, never other trees."""

    if root is None:
        return []
    try:
        want = root.expanduser().resolve()
    except OSError:
        return []
    hits: list[dict[str, Any]] = []
    for row in lookup_folder_name(token):
        kind = str(row.get("kind") or "")
        if kind in {"desk", "project"}:
            continue
        try:
            hit_root = Path(str(row.get("root") or "")).expanduser().resolve()
        except OSError:
            continue
        if hit_root != want:
            continue
        hits.append(row)
    return hits


def _scope_key() -> str:
    return "|".join(
        [
            str(projects_root() or ""),
            str(getattr(settings, "code_workspace", "") or ""),
            str(getattr(settings, "code_projects_root", "") or ""),
            str(getattr(settings, "environment", "") or ""),
        ]
    )


def _build_map() -> dict[str, Any]:
    payload: dict[str, Any] = {
        "version": _VERSION,
        "scope": _scope_key(),
        "projects": {},
        "by_name": {},
        "aliases": _load_persisted_aliases(),
    }
    for item in list_projects():
        name = str(item.get("name") or "")
        path = Path(str(item.get("path") or ""))
        if name in GENERIC_PROJECT_NAMES or len(name) < 2:
            continue
        if name.endswith(".git"):
            continue
        if is_sandbox_workspace(path):
            continue
        _index_code_root(payload, name, path)
    if str(getattr(settings, "environment", "") or "").lower() != "test":
        for desk in _desk_roots():
            _index_desk_root(payload, desk)
    for alias, name in list((payload.get("aliases") or {}).items()):
        projects = payload.get("projects") or {}
        meta = projects.get(name) if isinstance(projects, dict) else None
        if not isinstance(meta, dict):
            continue
        _add_hit(
            payload,
            _norm(str(alias)),
            {
                "project": name,
                "rel": "",
                "kind": "project",
                "path": str(meta.get("path") or ""),
                "root": str(meta.get("path") or ""),
                "name": name,
            },
        )
    return payload


def _refresh_stale_roots(payload: dict[str, Any]) -> None:
    projects = payload.get("projects") if isinstance(payload.get("projects"), dict) else {}
    dirty = False
    live: dict[str, Path] = {}
    for item in list_projects():
        name = str(item.get("name") or "")
        path = Path(str(item.get("path") or ""))
        if name in GENERIC_PROJECT_NAMES or is_sandbox_workspace(path):
            continue
        if name.endswith(".git"):
            continue
        live[name] = path
        meta = projects.get(name) if isinstance(projects.get(name), dict) else {}
        stamp = _root_stamp(path)
        if int(meta.get("stamp") or 0) != stamp:
            _index_code_root(payload, name, path)
            dirty = True
    for name in list(projects.keys()):
        if name not in live and str(projects.get(name, {}).get("kind") or "") != "desk":
            _clear_project_hits(payload, name)
            projects.pop(name, None)
            dirty = True
    if dirty:
        _persist_map(payload)


def _index_code_root(payload: dict[str, Any], name: str, path: Path) -> None:
    try:
        resolved = path.expanduser().resolve()
    except OSError:
        return
    if not resolved.is_dir():
        return
    _clear_project_hits(payload, name)
    stamp = _root_stamp(resolved)
    payload.setdefault("projects", {})[name] = {
        "path": str(resolved),
        "stamp": stamp,
        "kind": "code",
        "folders": [],
        "files": [],
    }
    _add_hit(
        payload,
        _norm(name),
        {
            "project": name,
            "rel": "",
            "kind": "project",
            "path": str(resolved),
            "root": str(resolved),
            "name": name,
        },
    )
    flex = name.replace("-", " ").replace("_", " ")
    if _norm(flex) != _norm(name):
        _add_hit(
            payload,
            _norm(flex),
            {
                "project": name,
                "rel": "",
                "kind": "project",
                "path": str(resolved),
                "root": str(resolved),
                "name": name,
            },
        )
    walked = _walk_entries(resolved)
    payload.setdefault("projects", {})[name]["folders"] = [
        rel for rel, is_dir in walked if is_dir
    ][:80]
    payload.setdefault("projects", {})[name]["files"] = [
        rel for rel, is_dir in walked if not is_dir
    ][:40]
    for rel, is_dir in walked:
        base = Path(rel).name
        stem = Path(rel).stem
        row = {
            "project": name,
            "rel": rel,
            "kind": "folder" if is_dir else "file",
            "path": str(resolved / rel),
            "root": str(resolved),
            "name": stem or base,
        }
        _add_hit(payload, _norm(base), row)
        if _norm(stem) != _norm(base):
            _add_hit(payload, _norm(stem), row)


def _index_desk_root(payload: dict[str, Any], root: Path) -> None:
    try:
        resolved = root.expanduser().resolve()
    except OSError:
        return
    if not resolved.is_dir():
        return
    label = resolved.name.lower() or "desktop"
    prefix_len = len(resolved.parts)
    files_indexed = 0
    try:
        for dirpath, dirnames, filenames in os.walk(resolved):
            current = Path(dirpath)
            depth = len(current.parts) - prefix_len
            dirnames[:] = [
                name
                for name in dirnames
                if name not in SKIP_DIR_NAMES and not name.startswith(".")
            ]
            if depth > _DESK_DEPTH:
                dirnames.clear()
                continue
            if depth >= 1:
                rel = str(current.relative_to(resolved)).replace("\\", "/")
                _add_hit(
                    payload,
                    _norm(current.name),
                    {
                        "project": label,
                        "rel": rel,
                        "kind": "desk",
                        "path": str(current),
                        "root": str(current),
                        "name": current.name,
                    },
                )
            for filename in filenames:
                if filename.startswith(".") or files_indexed >= _MAX_DESK_FILES:
                    continue
                child = current / filename
                rel = str(child.relative_to(resolved)).replace("\\", "/")
                stem = Path(filename).stem
                row = {
                    "project": label,
                    "rel": rel,
                    "kind": "desk",
                    "path": str(child),
                    "root": str(resolved),
                    "name": stem or filename,
                }
                _add_hit(payload, _norm(filename), row)
                if _norm(stem) != _norm(filename):
                    _add_hit(payload, _norm(stem), row)
                files_indexed += 1
            if files_indexed >= _MAX_DESK_FILES:
                dirnames.clear()
    except OSError:
        logger.debug("code_sandbox.desk_index_failed root=%s", resolved)


def _walk_entries(root: Path) -> list[tuple[str, bool]]:
    entries: list[tuple[str, bool]] = []
    try:
        resolved = root.resolve()
    except OSError:
        return entries
    for dirpath, dirnames, filenames in os.walk(resolved):
        dirnames[:] = [
            name
            for name in dirnames
            if name not in SKIP_DIR_NAMES and not name.startswith(".")
        ]
        current = Path(dirpath)
        try:
            rel_dir = current.resolve().relative_to(resolved)
        except (OSError, ValueError):
            continue
        depth = len(rel_dir.parts) if str(rel_dir) != "." else 0
        if depth > _MAX_DEPTH:
            dirnames.clear()
            continue
        if str(rel_dir) != ".":
            entries.append((str(rel_dir).replace("\\", "/"), True))
        for filename in filenames:
            if filename.startswith(".") or len(entries) >= _MAX_INDEX:
                continue
            rel = str((rel_dir / filename) if str(rel_dir) != "." else Path(filename))
            entries.append((rel.replace("\\", "/"), False))
        if len(entries) >= _MAX_INDEX:
            break
    return entries


def _desk_roots() -> list[Path]:
    if str(getattr(settings, "laptop_files_root", "") or "").strip():
        return []
    home = Path.home()
    found: list[Path] = []
    for name in ("Desktop", "Documents", "Downloads"):
        path = home / name
        if path.is_dir():
            found.append(path)
    return found


def _clear_project_hits(payload: dict[str, Any], name: str) -> None:
    """Drop prior map rows for one Code tree before a refresh."""

    wanted = (name or "").strip()
    if not wanted:
        return
    by_name = payload.get("by_name")
    if not isinstance(by_name, dict):
        return
    for key in list(by_name.keys()):
        rows = by_name.get(key)
        if not isinstance(rows, list):
            by_name.pop(key, None)
            continue
        kept = [
            row
            for row in rows
            if not (
                isinstance(row, dict)
                and str(row.get("project") or "") == wanted
                and str(row.get("kind") or "") != "desk"
            )
        ]
        if kept:
            by_name[key] = kept
        else:
            by_name.pop(key, None)


def _add_hit(payload: dict[str, Any], key: str, row: dict[str, Any]) -> None:
    if not key:
        return
    by_name = payload.setdefault("by_name", {})
    if not isinstance(by_name, dict):
        by_name = {}
        payload["by_name"] = by_name
    rows = by_name.setdefault(key, [])
    if not isinstance(rows, list):
        rows = []
        by_name[key] = rows
    marker = (row.get("path"), row.get("rel"), row.get("kind"))
    for existing in rows:
        if (
            isinstance(existing, dict)
            and (existing.get("path"), existing.get("rel"), existing.get("kind")) == marker
        ):
            return
    rows.append(row)


def _root_stamp(path: Path) -> int:
    stamp = 0
    try:
        stamp = int(path.stat().st_mtime)
    except OSError:
        return 0
    for rel in ("OVERVIEW.md", "README.md", "package.json", "src"):
        child = path / rel
        try:
            stamp = max(stamp, int(child.stat().st_mtime))
        except OSError:
            continue
    return stamp


def _load_persisted_aliases() -> dict[str, str]:
    try:
        from app.memory.paths import ensure_tree, read_json

        path = ensure_tree() / "code-jobs" / "project-cards.json"
        stored = read_json(path) if path.exists() else {}
    except OSError:
        return {}
    if not isinstance(stored, dict):
        return {}
    aliases: dict[str, str] = {}
    from app.ev.code_literacy import _alias_token_ok

    for name, payload in stored.items():
        if not isinstance(payload, dict):
            continue
        for alias in payload.get("aliases") or []:
            token = str(alias or "").strip().lower()
            if _alias_token_ok(token):
                aliases[token] = str(name)
    return aliases


def _map_path() -> Path:
    from app.memory.paths import ensure_tree

    return ensure_tree() / "code-jobs" / "folder-map.json"


def _load_map(scope: str) -> dict[str, Any] | None:
    try:
        from app.memory.paths import read_json

        path = _map_path()
        if not path.exists():
            return None
        stored = read_json(path)
    except OSError:
        return None
    if not isinstance(stored, dict) or stored.get("version") != _VERSION:
        return None
    if stored.get("scope") != scope:
        return None
    return stored


def _persist_map(payload: dict[str, Any]) -> None:
    try:
        from app.memory.paths import atomic_write_json

        payload["scope"] = _scope_key()
        payload["version"] = _VERSION
        atomic_write_json(_map_path(), payload)
    except OSError:
        logger.debug("code_sandbox.persist_failed")


def _norm(value: str) -> str:
    return _NORM_RE.sub("", (value or "").lower())


def _trigrams(value: str) -> set[str]:
    token = f"  {_norm(value)} "
    if len(token) < 5:
        return set()
    return {token[index : index + 3] for index in range(len(token) - 2)}


def _name_score(query: str, key: str) -> float:
    q = _norm(query)
    k = _norm(key)
    if not q or not k:
        return 0.0
    if q == k:
        return 100.0
    shorter, longer = (q, k) if len(q) <= len(k) else (k, q)
    if len(shorter) >= 4 and longer.startswith(shorter):
        return max(90.0 - (len(longer) - len(shorter)) * 2.0, 78.0)
    if len(q) < 5 or len(k) < 5:
        return 0.0
    left, right = _trigrams(q), _trigrams(k)
    if not left or not right:
        return 0.0
    dice = (2.0 * len(left & right)) / (len(left) + len(right))
    if dice < 0.78:
        return 0.0
    return 50.0 + 50.0 * dice


def _rank_name_hits(
    query: str, by_name: dict[str, Any]
) -> list[tuple[float, dict[str, Any]]]:
    scored: list[tuple[float, dict[str, Any]]] = []
    for name, rows in by_name.items():
        score = _name_score(query, str(name))
        if score < _RANK_KEEP:
            continue
        for row in rows or []:
            if isinstance(row, dict):
                scored.append((score, row))
    scored.sort(key=lambda item: (-item[0], len(str(item[1].get("rel") or ""))))
    return scored[:24]
